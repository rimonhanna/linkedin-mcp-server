"""invite_to_follow_page outside working hours, through the real refusal.

A confirmed send is refused before the browser with wording that names this
server's schedule; a dry run still reaches the browser to plan.
"""

from datetime import date, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import FunctionTool

from linkedin_mcp_server import pacing
from linkedin_mcp_server.config.loaders import EnvironmentKeys
from linkedin_mcp_server.exceptions import ActionLimitError
from linkedin_mcp_server.pacing import ACCOUNT_BUDGET_JOB, Job, Schedule
from linkedin_mcp_server.tools import company_page
from linkedin_mcp_server.tools.company_page import register_company_page_tools

# Synthetic: shaped like a profile id, names no one.
PROFILE = "ACoAAExampleFakeProfileId0000"
WED_8PM = datetime(2026, 8, 5, 20, 0).astimezone()


@pytest.fixture
def off_hours(monkeypatch):
    monkeypatch.delenv(EnvironmentKeys.WORKING_HOURS_DISABLED, raising=False)
    job = Job(
        name=ACCOUNT_BUDGET_JOB,
        started_on=date(2020, 1, 1),
        schedule=Schedule.business_hours(),
    )
    monkeypatch.setattr(pacing, "load_account_budget", lambda _store, _now: job)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            return WED_8PM

    monkeypatch.setattr(company_page, "datetime", _Clock)


async def _invite(mock_context, confirm: bool) -> MagicMock:
    mcp = FastMCP("test")
    register_company_page_tools(mcp)
    fn = cast(FunctionTool, await mcp.get_tool("invite_to_follow_page")).fn
    extractor = MagicMock()
    extractor.invite_to_follow_page = AsyncMock(
        return_value={"status": "dry_run", "people": []}
    )
    await fn("100000701", [PROFILE], confirm, mock_context, extractor=extractor)
    return extractor


async def test_an_off_hours_send_is_refused_as_the_schedule_before_the_browser(
    off_hours, mock_context, monkeypatch
):
    recorded: list[str] = []
    monkeypatch.setattr(
        pacing, "record_action", lambda _s, kind, _n: recorded.append(kind)
    )
    with pytest.raises(ToolError) as excinfo:
        await _invite(mock_context, confirm=True)

    message = str(excinfo.value)
    assert message.startswith("page invites are not sent outside working hours")
    assert "this server's schedule, not a LinkedIn limit" in message
    assert "page invites resume at " in message
    assert "limit_exceeded" not in message
    assert recorded == []


async def test_an_off_hours_dry_run_still_reaches_the_browser(off_hours, mock_context):
    extractor = await _invite(mock_context, confirm=False)
    extractor.invite_to_follow_page.assert_awaited_once()


def test_the_schedule_and_a_cap_read_differently():
    when = datetime(2026, 8, 6, 9, 0)
    schedule = ActionLimitError(
        "page_invites", limit=0, window="working hours", resume_at=when
    )
    cap = ActionLimitError("page_invites", limit=20, window="24 h", resume_at=when)

    assert schedule.schedule_only and not cap.schedule_only
    assert schedule.error_type == cap.error_type == "limit_exceeded"
    assert str(schedule) == (
        "page invites are not sent outside working hours (this server's "
        "schedule, not a LinkedIn limit); page invites resume at 2026-08-06T09:00."
    )
    assert str(cap) == (
        "limit_exceeded: page_invites limit of 20 per 24 h reached. "
        "Resume at 2026-08-06T09:00."
    )
