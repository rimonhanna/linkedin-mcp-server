"""A BUILDING matched audience has no member count yet (synthetic values)."""

from __future__ import annotations

from linkedin_mcp_server.scraping.campaign_manager_entities_payload import (
    matched_audiences,
    render_audiences,
)

from .test_campaign_manager_entities_payload_live_matched import _page, _row


def _building() -> dict:
    return _row(
        700000401,
        "Example new list",
        "BUILDING",
        sourceId=700000402,
        count=0,
        segmentType="BULK",
        listAudienceType="ORGANIZATION",
    )


def test_building_row_has_no_count_and_keeps_its_source_id():
    audiences = matched_audiences(_page(_building()))
    assert audiences is not None
    [audience] = audiences
    assert "audienceCount" not in audience, (
        "a BUILDING row's count of 0 must be reported as absent"
    )
    assert (audience["id"], audience["sourceId"]) == (700000401, 700000402)


def test_building_row_summary_says_not_matched_yet():
    audiences = matched_audiences(_page(_building()))
    assert audiences is not None
    text = render_audiences(audiences)
    assert "0 members" not in text, "a BUILDING row must not read as 0 members"
    assert text == (
        "Example new list (id 700000401): BULK, ORGANIZATION, LIST_UPLOAD, "
        "BUILDING, not matched yet, urn:li:adSegment:700000401"
    )


def test_ready_row_keeps_its_count_of_zero():
    row = _row(700000403, "Example ready list", "READY", count=0)
    audiences = matched_audiences(_page(row))
    assert audiences is not None
    assert audiences[0].get("audienceCount") == 0
