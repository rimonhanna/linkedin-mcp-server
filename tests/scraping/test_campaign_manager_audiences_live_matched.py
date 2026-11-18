"""list_audiences and the upload read-back on LinkedIn's live matched shape.

Synthetic ids and names only; the row keys are those LinkedIn served on
2026-11-18.
"""

from __future__ import annotations

from .test_campaign_manager_audiences import (
    ACCOUNT,
    COMPANIES,
    MATCHED,
    NEW_ID,
    _audiences,
    _Browser,
    _dialog,
    _matched,
    _segment,
    _signed_url,
)
from .test_campaign_manager_entities_payload_live_matched import _row


async def test_list_audiences_reports_an_audience_still_matching(mock_page):
    browser = _Browser(mock_page)
    browser.loads[MATCHED] = [[_matched(_row(100000401, "Example list", "PROCESSING"))]]
    browser.tab_count = 0

    with browser.running():
        result = await _audiences(mock_page).list_audiences(ACCOUNT)

    [audience] = result["matched_audiences"]
    assert (audience["id"], audience["status"]) == (100000401, "PROCESSING")
    assert "PROCESSING" in result["sections"]["matched_audiences"]


async def test_upload_read_back_finds_the_processing_audience(mock_page):
    browser = _Browser(mock_page)
    browser.loads[MATCHED] = [
        [_matched()],
        [_matched(_row(int(NEW_ID), "Example list", "PROCESSING"))],
    ]
    sends = [_signed_url(), _segment("Example list")]

    with browser.running(), _dialog(mock_page, sends=sends):
        result = await _audiences(mock_page).create_company_list_audience(
            ACCOUNT, "Example list", COMPANIES, confirm_send=True
        )

    assert result["status"] == "audience_created", result["message"]
    assert result["created_id"] == NEW_ID
    assert result["applied"]["status"] == "PROCESSING"
