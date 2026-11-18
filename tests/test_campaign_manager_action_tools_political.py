"""The write tools pass the caller's political-ad declaration through unchanged."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP

from linkedin_mcp_server.scraping.campaign_manager_results import (
    STATUSES,
    SUCCEEDED,
)
from linkedin_mcp_server.tools.campaign_manager_actions import (
    register_campaign_manager_action_tools,
)

_MODULE = "linkedin_mcp_server.tools.campaign_manager_actions"
VALID = {
    "update_campaign": {
        "ad_account_id": "100000001",
        "campaign_id": "200000002",
        "daily_budget": "12.5",
        "confirm_send": True,
    },
    "update_campaign_targeting": {
        "ad_account_id": "100000001",
        "campaign_id": "200000002",
        "remove": ["urn:li:title:200"],
        "confirm_send": True,
    },
    "create_campaign_draft": {
        "ad_account_id": "100000001",
        "campaign_group_id": "300000003",
        "objective": "WEBSITE_VISIT",
        "name": "Example ad set",
        "confirm_send": True,
    },
}
DECLARING = tuple(VALID)


def _mcp() -> FastMCP:
    mcp = FastMCP("test")
    register_campaign_manager_action_tools(mcp)
    return mcp


def _extractor() -> MagicMock:
    mock = MagicMock()
    result = {"url": "u", "status": "updated", "message": "m", "retry_safe": False}
    for name in DECLARING:
        setattr(mock, name, AsyncMock(return_value=result))
    return mock


@pytest.fixture
def pacing():
    with (
        patch(f"{_MODULE}.refuse_action"),
        patch(f"{_MODULE}.record_action"),
        patch(f"{_MODULE}.JobStore"),
    ):
        yield


async def _call(name: str, args: dict, extractor: MagicMock) -> None:
    with patch(f"{_MODULE}.get_ready_extractor", AsyncMock(return_value=extractor)):
        await _mcp().call_tool(name, args)


@pytest.mark.parametrize("name", DECLARING)
@pytest.mark.parametrize("declared", [True, False])
async def test_the_declaration_reaches_the_editor(pacing, name, declared):
    extractor = _extractor()

    await _call(name, {**VALID[name], "political_ad": declared}, extractor)

    assert getattr(extractor, name).await_args.kwargs["political_ad"] is declared


@pytest.mark.parametrize("name", DECLARING)
async def test_no_declaration_is_passed_as_none(pacing, name):
    extractor = _extractor()

    await _call(name, VALID[name], extractor)

    assert getattr(extractor, name).await_args.kwargs["political_ad"] is None


@pytest.mark.parametrize("name", DECLARING)
async def test_docstring_names_it_a_declaration_the_user_makes(name):
    tool = await _mcp().get_tool(name)
    assert tool is not None
    description = " ".join((tool.description or "").split())
    assert "legal declaration the user makes" in description
    assert "never defaults it" in description
    assert "planned.politicalAdQuestion" in description
    argument = tool.parameters["properties"]["political_ad"]["description"]
    assert "never guess or default it" in " ".join(argument.split())


@pytest.mark.parametrize("name", DECLARING)
def test_declaration_required_is_a_stop_not_a_success(name):
    assert "declaration_required" in STATUSES[name]
    assert "declaration_required" not in SUCCEEDED[name]
