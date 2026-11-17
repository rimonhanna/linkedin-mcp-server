"""Campaign Manager write tools: a returned control_unavailable writes notes."""

from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP

from linkedin_mcp_server.scraping.campaign_manager_results import (
    campaign_action_result,
)
from linkedin_mcp_server.tools.campaign_manager_actions import (
    register_campaign_manager_action_tools,
)

_MODULE = "linkedin_mcp_server.tools.campaign_manager_actions"
ARGS = {
    "ad_account_id": "urn:li:sponsoredAccount:100000001",
    "campaign_id": "urn:li:sponsoredCampaign:200000002",
    "daily_budget": "12.5",
    "confirm_send": False,
}
URL = "https://www.linkedin.com/campaignmanager/accounts/100000001/campaigns"


async def _call(status: str) -> MagicMock:
    extractor = MagicMock()
    extractor.update_campaign = AsyncMock(
        return_value=campaign_action_result(
            "update_campaign",
            URL,
            status,
            "The control was not found.",
            retry_safe=True,
        )
    )
    mcp = FastMCP("test")
    register_campaign_manager_action_tools(mcp)
    with (
        patch(f"{_MODULE}.get_ready_extractor", AsyncMock(return_value=extractor)),
        patch(
            f"{_MODULE}.build_issue_diagnostics",
            return_value={"issue_template_path": "notes.md"},
        ) as notes,
    ):
        result = await mcp.call_tool("update_campaign", ARGS)
    assert cast(dict, result.structured_content)["status"] == status
    return notes


async def test_control_unavailable_writes_the_failure_notes():
    notes = await _call("control_unavailable")

    notes.assert_called_once()
    assert notes.call_args.kwargs == {"context": "update_campaign"}
    assert str(notes.call_args.args[0]) == "The control was not found."


@pytest.mark.parametrize("status", ["confirmation_required", "not_editable"])
async def test_other_statuses_write_no_notes(status):
    notes = await _call(status)

    notes.assert_not_called()
