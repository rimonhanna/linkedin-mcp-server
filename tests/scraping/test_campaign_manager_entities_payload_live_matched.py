"""Matched audiences in the shape LinkedIn served them live (synthetic values).

Every key of a ``campaignManagerMatchedAudiences?q=account`` row seen on
2026-11-18 is present; ids, names and enum values are made up.
"""

from __future__ import annotations

from typing import Any

from linkedin_mcp_server.scraping.campaign_manager_entities_payload import (
    matched_audiences,
    render_audiences,
)


def _row(segment_id: int, name: str, status: str, **extra: Any) -> dict[str, Any]:
    return {
        "activeCampaigns": [],
        "allowedToCopy": True,
        "allowedToCreateLookalike": False,
        "allowedToDelete": True,
        "allowedToEdit": True,
        "allowedToViewMatchedCompanies": False,
        "companyInputDataAvailable": True,
        "companyMatchesNotAvailableReason": "EXAMPLE_REASON",
        "lastModifiedAt": 1700000000000,
        "listAudienceType": "ACCOUNT_LIST",
        "name": name,
        "ownership": "ACCOUNT",
        "segmentId": segment_id,
        "segmentType": "COMPANY",
        "sourceId": 900000001,
        "sourcePlatform": "LIST_UPLOAD",
        "status": status,
        **extra,
    }


def _page(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"elements": list(rows), "paging": {"count": 25, "links": [], "start": 0}}


def test_live_row_is_read_by_its_segment_id():
    audiences = matched_audiences(
        _page(_row(700000101, "Example ready list", "READY", count=1500))
    )
    assert audiences == [
        {
            "kind": "matched_audience",
            "id": 700000101,
            "segmentUrn": "urn:li:adSegment:700000101",
            "name": "Example ready list",
            "segmentType": "COMPANY",
            "listType": "ACCOUNT_LIST",
            "source": "LIST_UPLOAD",
            "status": "READY",
            "sourceId": 900000001,
            "audienceCount": 1500,
        }
    ]


def test_audience_still_matching_is_reported_with_its_status():
    audiences = matched_audiences(
        _page(_row(700000102, "Example new list", "PROCESSING"))
    )
    assert audiences is not None, "a row still matching must not fail the list"
    [audience] = audiences
    assert audience["id"] == 700000102
    assert audience["status"] == "PROCESSING"
    assert "audienceCount" not in audience
    assert render_audiences(audiences) == (
        "Example new list (id 700000102): COMPANY, ACCOUNT_LIST, LIST_UPLOAD, "
        "PROCESSING, urn:li:adSegment:700000102"
    )


def test_row_without_a_segment_id_is_unrecognised():
    row = _row(700000103, "Example", "READY")
    del row["segmentId"]
    assert matched_audiences(_page(row)) is None
