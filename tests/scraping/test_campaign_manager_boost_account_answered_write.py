"""A boost refusal at close never replaces an answered write (synthetic data).

The capture raises a boost account's refusal again when it closes, for a
caller that took it for a miss. Once LinkedIn answered a write the capture
recorded, that write's result stands instead.
"""

from __future__ import annotations

from typing import Any

import pytest

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.scraping.campaign_manager import BOOST_WRITE, boost_refusal
from linkedin_mcp_server.scraping.campaign_manager_api import (
    CampaignManagerCapture,
    CmResponse,
    CmWriteResponse,
)
from linkedin_mcp_server.scraping.campaign_manager_payload import parse_cm_call

from scraping.test_campaign_manager_boost_account_tools import CUTOFF, MEMBER

CM = "https://www.linkedin.com/campaign-manager-api/"
ACCOUNT = "100000001"


def _boost_record() -> CmResponse:
    call = parse_cm_call(f"{CM}campaignManagerAccounts/{ACCOUNT}", "GET", {}, None)
    assert call is not None
    payload = {
        "data": {
            "id": int(ACCOUNT),
            "type": "PERSONAL",
            "reference": MEMBER,
            "createdAt": CUTOFF,
        },
        "included": [],
    }
    return CmResponse(call, 200, payload)


def _answered_write() -> CmWriteResponse:
    call = parse_cm_call(f"{CM}campaignManagerCampaigns/100000301", "POST", {}, "{}")
    assert call is not None
    return CmWriteResponse(
        call=call,
        method="POST",
        restli_method="PARTIAL_UPDATE",
        body={},
        status=204,
        payload=None,
        created_id=None,
    )


async def _close_with(page: Any, *, wrote: bool) -> str:
    refuse = boost_refusal(ACCOUNT, BOOST_WRITE)
    async with CampaignManagerCapture(page, refuse=refuse) as capture:
        # Seen only now, after the tool's waits: nothing raised it yet.
        capture.responses.append(_boost_record())
        if wrote:
            capture.writes.append(_answered_write())
    return "write result"


async def test_an_answered_write_keeps_its_result_over_the_boost_refusal(mock_page):
    assert await _close_with(mock_page, wrote=True) == "write result"


async def test_with_no_write_the_capture_raises_the_boost_refusal_at_close(mock_page):
    with pytest.raises(InvalidReferenceError, match="nothing was changed"):
        await _close_with(mock_page, wrote=False)
