"""Campaign Manager write tools: an error escaping a flow after a write was sent."""

import asyncio
from contextlib import asynccontextmanager
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.scraping import campaign_manager_write
from linkedin_mcp_server.scraping.campaign_manager_guard import WriteWindow
from linkedin_mcp_server.scraping.campaign_manager_write import CampaignWriteFlow
from linkedin_mcp_server.tools import campaign_manager_actions
from linkedin_mcp_server.tools.campaign_manager_actions import (
    register_campaign_manager_action_tools,
)

_MODULE = "linkedin_mcp_server.tools.campaign_manager_actions"
ACCOUNT_URN = "urn:li:sponsoredAccount:100000001"
CAMPAIGN_URN = "urn:li:sponsoredCampaign:200000002"
PAGE_URL = "https://www.linkedin.com/campaignmanager/accounts/100000001/campaigns"

VALID = {
    "update_campaign": {
        "ad_account_id": ACCOUNT_URN,
        "campaign_id": CAMPAIGN_URN,
        "daily_budget": "12.5",
    },
    "set_campaign_status": {
        "ad_account_id": ACCOUNT_URN,
        "campaign_id": CAMPAIGN_URN,
        "status": "PAUSED",
    },
    "update_campaign_targeting": {
        "ad_account_id": ACCOUNT_URN,
        "campaign_id": CAMPAIGN_URN,
        "remove": ["urn:li:title:200"],
    },
    "create_campaign_draft": {
        "ad_account_id": ACCOUNT_URN,
        "campaign_group_id": "urn:li:sponsoredCampaignGroup:300000003",
        "objective": "WEBSITE_VISIT",
        "name": "Example ad set",
    },
    "add_post_to_campaign": {
        "ad_account_id": ACCOUNT_URN,
        "campaign_id": CAMPAIGN_URN,
        "post_urn": "urn:li:share:400000004",
    },
    "create_company_list_audience": {
        "ad_account_id": ACCOUNT_URN,
        "name": "Example list",
        "companies": [{"name": "Example company"}],
    },
    "create_saved_audience": {
        "ad_account_id": ACCOUNT_URN,
        "campaign_id": CAMPAIGN_URN,
        "name": "Example audience",
    },
}
TOOLS = tuple(VALID)


@asynccontextmanager
async def _fake_window(page, *, admit):
    yield WriteWindow()


@pytest.fixture
def pacing():
    with (
        patch(f"{_MODULE}.refuse_action"),
        patch(f"{_MODULE}.record_action") as record,
        patch(f"{_MODULE}.JobStore"),
        patch.object(campaign_manager_write, "explicit_campaign_write", _fake_window),
    ):
        yield record


def _flow_raising(error: BaseException, *, admit: bool):
    """A flow step that opens the real write window, maybe admits, then raises."""
    page = MagicMock()
    page.url = PAGE_URL
    flow = CampaignWriteFlow(MagicMock(), MagicMock())

    async def run(*_args, **_kwargs):
        async with flow.apply(page, lambda _request: None) as window:
            if admit:
                window.admitted.append(MagicMock())
            raise error

    return run


def _extractor(error: BaseException, *, admit: bool) -> MagicMock:
    extractor = MagicMock()
    for name in TOOLS:
        setattr(
            extractor, name, AsyncMock(side_effect=_flow_raising(error, admit=admit))
        )
    return extractor


async def _call(name: str, extractor) -> dict:
    mcp = FastMCP("test")
    register_campaign_manager_action_tools(mcp)
    with patch(f"{_MODULE}.get_ready_extractor", AsyncMock(return_value=extractor)):
        result = await mcp.call_tool(name, {**VALID[name], "confirm_send": True})
    return cast(dict, result.structured_content)


class TestErrorAfterAnAdmittedWrite:
    @pytest.mark.parametrize("name", TOOLS)
    async def test_is_unverified_and_recorded_once(self, pacing, name):
        extractor = _extractor(PlaywrightTimeoutError("click timed out"), admit=True)
        result = await _call(name, extractor)
        assert result["status"] == "save_unverified"
        assert result["retry_safe"] is False
        assert result["url"] == PAGE_URL
        pacing.assert_called_once()
        assert pacing.call_args.args[1] == "campaign_writes"

    @pytest.mark.parametrize("name", TOOLS)
    async def test_before_any_admission_raises_and_records_nothing(self, pacing, name):
        extractor = _extractor(PlaywrightTimeoutError("click timed out"), admit=False)
        with pytest.raises(ToolError):
            await _call(name, extractor)
        pacing.assert_not_called()


class TestCancellation:
    async def _apply(self, *, admit: bool):
        run = _flow_raising(asyncio.CancelledError(), admit=admit)
        ctx = MagicMock()
        ctx.report_progress = AsyncMock()
        with pytest.raises(asyncio.CancelledError):
            await campaign_manager_actions._apply(
                ctx,
                "update_campaign",
                confirm_send=True,
                extractor=MagicMock(),
                message="m",
                call=lambda _ex: run(),
            )

    async def test_after_an_admitted_write_is_recorded_once_then_reraised(self, pacing):
        await self._apply(admit=True)
        pacing.assert_called_once()

    async def test_before_any_admission_records_nothing(self, pacing):
        await self._apply(admit=False)
        pacing.assert_not_called()


def test_ledger_is_scoped_to_its_context():
    with campaign_manager_write.tracking_writes() as ledger:
        assert campaign_manager_write._ledger.get() is ledger
    assert campaign_manager_write._ledger.get() is None
