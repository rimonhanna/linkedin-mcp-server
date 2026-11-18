"""The upload read-back links the create to its listed row by id, never name.

LinkedIn answers the ``campaignManagerDmpSegments`` create with the DMP
segment id in ``x-restli-id``; the listed row carries it as ``sourceId`` and
has its own ``segmentId``. Synthetic ids and names only.
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

SEGMENT_ID = 700000301


def _building(segment_id: int, source_id: int, name: str = "Example list") -> dict:
    return _row(
        segment_id,
        name,
        "BUILDING",
        sourceId=source_id,
        count=0,
        segmentType="BULK",
        listAudienceType="ORGANIZATION",
    )


async def _upload(mock_page, *after: dict) -> dict:
    browser = _Browser(mock_page)
    browser.loads[MATCHED] = [[_matched()], [_matched(*after)]]
    sends = [_signed_url(), _segment("Example list")]
    with browser.running(), _dialog(mock_page, sends=sends):
        return await _audiences(mock_page).create_company_list_audience(
            ACCOUNT, "Example list", COMPANIES, confirm_send=True
        )


async def test_create_id_is_matched_to_the_rows_source_id(mock_page):
    result = await _upload(mock_page, _building(SEGMENT_ID, int(NEW_ID)))

    assert result["status"] == "audience_created", (
        "the create's x-restli-id must be matched to the listed row's sourceId"
    )
    assert result["created_id"] == str(SEGMENT_ID), (
        "created_id must be the listed audience's segmentId"
    )
    assert result["applied"]["sourceId"] == int(NEW_ID)
    assert f"id {SEGMENT_ID}, list source id {NEW_ID}" in result["message"]
    assert "audienceCount" not in result["applied"]


async def test_same_name_with_another_source_id_is_not_the_upload(mock_page):
    result = await _upload(mock_page, _building(SEGMENT_ID, 700000999))

    assert result["status"] == "save_unverified", (
        "a row with the same name but another sourceId must not verify the upload"
    )


async def test_create_id_equal_to_another_rows_segment_id_is_not_the_upload(
    mock_page,
):
    result = await _upload(mock_page, _building(int(NEW_ID), 700000999))

    assert result["status"] == "save_unverified", (
        "the create's DMP id must not be read as a listed segmentId"
    )
