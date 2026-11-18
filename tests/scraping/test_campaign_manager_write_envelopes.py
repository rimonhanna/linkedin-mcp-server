"""The write flow's campaign read takes either envelope (synthetic data)."""

from __future__ import annotations

import pytest

from linkedin_mcp_server.core.exceptions import LinkedInScraperException

from scraping.test_campaign_manager_write import (
    CAMPAIGN,
    DETAILS,
    _Browser,
    _read,
    _record,
    _response,
)


async def test_read_campaign_reads_a_plain_record(mock_page):
    browser = _Browser(mock_page)
    plain = _record()["data"]
    browser.loads[DETAILS] = [
        [_response(f"campaignManagerCampaigns/{CAMPAIGN}", plain)]
    ]

    assert await _read(mock_page, browser) == plain


async def test_read_campaign_resolves_a_record_ref_through_included(mock_page):
    browser = _Browser(mock_page)
    urn = f"urn:li:sponsoredCampaign:{CAMPAIGN}"
    entity = {**_record()["data"], "entityUrn": urn}
    payload = {"data": urn, "included": [entity]}
    browser.loads[DETAILS] = [
        [_response(f"campaignManagerCampaigns/{CAMPAIGN}", payload)]
    ]

    assert await _read(mock_page, browser) == entity


async def test_read_campaign_waits_past_another_campaigns_record(mock_page):
    browser = _Browser(mock_page)
    other = {**_record()["data"], "id": int(CAMPAIGN) + 1}
    browser.loads[DETAILS] = [
        [_response(f"campaignManagerCampaigns/{CAMPAIGN}", other)]
    ]

    with pytest.raises(LinkedInScraperException, match="did not return"):
        await _read(mock_page, browser)
