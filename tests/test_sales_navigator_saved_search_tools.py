"""save_sales_navigator_search at the tool layer: what is refused before the browser."""

from datetime import datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import FastMCP
from fastmcp.tools import FunctionTool

from linkedin_mcp_server.config.loaders import EnvironmentKeys
from linkedin_mcp_server.pacing import SALES_LIST_WRITES, JobStore, Schedule
from linkedin_mcp_server.tools import sales_navigator_lists as list_tools
from linkedin_mcp_server.tools.sales_navigator_saved_search import (
    register_sales_navigator_saved_search_tools,
)

TOOL = "save_sales_navigator_search"
RESULT = {"url": "https://www.linkedin.com/sales/search/people", "status": "dry_run"}
WED_8PM = datetime(2026, 8, 5, 20, 0).astimezone()
WED_10AM = datetime(2026, 8, 5, 10, 0).astimezone()


def _extractor() -> MagicMock:
    mock = MagicMock()
    mock.save_sales_navigator_search = AsyncMock(return_value=RESULT)
    return mock


async def _fn():
    mcp = FastMCP("test")
    register_sales_navigator_saved_search_tools(mcp)
    tool = await mcp.get_tool(TOOL)
    assert tool is not None
    return cast(FunctionTool, tool).fn


@pytest.fixture
def clock(tmp_path, monkeypatch):
    """The cap's ledger in a temporary home, and a settable clock."""
    store = JobStore(tmp_path / "jobs")
    monkeypatch.setattr(list_tools, "JobStore", lambda *a, **k: store)
    monkeypatch.delenv(EnvironmentKeys.WORKING_HOURS_DISABLED, raising=False)
    now: dict[str, datetime] = {"at": WED_10AM}

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            return now["at"]

    monkeypatch.setattr(list_tools, "datetime", _Clock)
    return now


async def test_the_tool_claims_a_write_that_destroys_nothing():
    mcp = FastMCP("test")
    register_sales_navigator_saved_search_tools(mcp)
    tool = await mcp.get_tool(TOOL)
    assert tool is not None and tool.annotations is not None
    assert tool.annotations.destructiveHint is False
    assert not tool.annotations.readOnlyHint
    assert tool.tags == {"sales_navigator", "actions"}


async def test_a_dry_run_passes_outside_working_hours_and_owes_nothing(
    mock_context, clock
):
    clock["at"] = WED_8PM
    extractor = _extractor()
    fn = await _fn()

    result = await fn(
        kind="leads",
        name=" Example UK leads ",
        confirm_send=False,
        ctx=mock_context,
        title=["Head of Sales"],
        company_hq_location=["101165590"],
        company_size=["201-500", "501-1000"],
        industry=["software development", "96"],
        extractor=extractor,
    )

    assert result == RESULT
    call = extractor.save_sales_navigator_search.await_args
    assert call.args == ("leads", "Example UK leads")
    assert call.kwargs == {
        "criteria": {
            "title": ["Head of Sales"],
            "company_hq_location": ["101165590"],
            "company_size": ["E", "F"],
            "industry": ["4", "96"],
        },
        "confirm_send": False,
    }


async def test_a_save_outside_working_hours_is_refused_before_the_browser(
    mock_context, clock
):
    clock["at"] = WED_8PM
    # The account's schedule, as a real home holds it (the suite's is open).
    list_tools.load_account_budget(
        list_tools.JobStore(), WED_8PM, schedule=Schedule.business_hours()
    )
    extractor = _extractor()
    fn = await _fn()

    with pytest.raises(Exception, match="outside working hours"):
        await fn(
            kind="leads",
            name="Example",
            confirm_send=True,
            ctx=mock_context,
            title=["CTO"],
            extractor=extractor,
        )
    extractor.save_sales_navigator_search.assert_not_awaited()


async def test_a_save_without_room_for_both_requests_is_refused(
    mock_context, clock, monkeypatch
):
    monkeypatch.setenv(EnvironmentKeys.SALES_LIST_WRITES_MAX, "2")
    store = list_tools.JobStore()
    budget = list_tools.load_account_budget(store, WED_10AM)
    budget.ledger.record_kind(SALES_LIST_WRITES, WED_10AM)
    store.save(budget)
    extractor = _extractor()
    fn = await _fn()

    with pytest.raises(Exception) as excinfo:
        await fn(
            kind="leads",
            name="Example",
            confirm_send=True,
            ctx=mock_context,
            title=["CTO"],
            extractor=extractor,
        )
    assert "sales_list_writes" in str(excinfo.value)
    extractor.save_sales_navigator_search.assert_not_awaited()


async def test_a_save_with_room_reaches_the_browser(mock_context, clock):
    extractor = _extractor()
    fn = await _fn()

    await fn(
        kind="accounts",
        name="Example",
        confirm_send=True,
        ctx=mock_context,
        hq_location=["101165590"],
        size=["E"],
        extractor=extractor,
    )

    assert extractor.save_sales_navigator_search.await_args.kwargs == {
        "criteria": {"hq_location": ["101165590"], "size": ["E"]},
        "confirm_send": True,
    }


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (
            {"kind": "leads", "name": "Example", "hq_location": ["UK"]},
            "hq_location is a filter of the accounts search",
        ),
        (
            {"kind": "accounts", "name": "Example", "title": ["CTO"]},
            "title is a filter of the leads search",
        ),
        ({"kind": "leads", "name": "Example"}, "Pass keywords or at least one filter."),
        ({"kind": "leads", "name": "", "title": ["CTO"]}, "name must not be empty."),
        (
            {"kind": "leads", "name": "Example", "industry": ["Not an industry"]},
            "Unknown industry",
        ),
    ],
)
async def test_invalid_input_is_refused_before_the_browser(
    mock_context, clock, args: dict[str, Any], message
):
    clock["at"] = WED_8PM
    extractor = _extractor()
    fn = await _fn()

    result = await fn(confirm_send=True, ctx=mock_context, extractor=extractor, **args)

    assert result["status"] == "invalid_request"
    assert message in result["message"]
    assert result["retry_safe"] is True
    extractor.save_sales_navigator_search.assert_not_awaited()


@pytest.mark.parametrize(
    ("used", "extra", "refused"),
    [
        # The search page, the create, the rename and the read-back.
        (1, {}, False),
        (2, {}, True),
        # A location name costs its lookup on top.
        (0, {"company_hq_location": ["United Kingdom"]}, False),
        (1, {"company_hq_location": ["United Kingdom"]}, True),
    ],
)
async def test_a_save_without_hourly_room_for_every_action_is_refused(
    mock_context, clock, monkeypatch, used, extra, refused
):
    monkeypatch.setenv(EnvironmentKeys.HOURLY_ACTIONS_MAX, "5")
    store = list_tools.JobStore()
    budget = list_tools.load_account_budget(store, WED_10AM)
    for _ in range(used):
        budget.ledger.record(WED_10AM)
    store.save(budget)
    extractor = _extractor()
    fn = await _fn()

    async def save():
        return await fn(
            kind="leads",
            name="Example",
            confirm_send=True,
            ctx=mock_context,
            title=["Head of Sales"],
            extractor=extractor,
            **extra,
        )

    if refused:
        with pytest.raises(Exception, match="1 h"):
            await save()
        extractor.save_sales_navigator_search.assert_not_awaited()
    else:
        await save()
        extractor.save_sales_navigator_search.assert_awaited_once()
