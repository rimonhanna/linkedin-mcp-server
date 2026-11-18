"""Analytics for the entities the reporting table covers, naming the rest (synthetic data).

The table asks only for the campaigns its default status filter shows. A
draft never ran, so it need not be covered; any other status left out is
reported in ``uncovered`` and as a ``page_limit``.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import parse_qs

from scraping.test_campaign_manager import (
    ACCOUNT,
    CURRENT,
    GROUP,
    _Browser,
    _campaigns,
    _response,
    _run,
    _url,
)
from scraping.test_campaign_manager_reporting_queries import (
    CAMPAIGN_FIELDS,
    GROUP_FIELDS,
    _group_cost,
    _ids,
    _objective_row,
    _statistics,
)

FIRST = 100000401
IDS = [FIRST + offset for offset in range(17)]
DRAFT = IDS[15]
REMOVED = IDS[16]
SHOWN = IDS[:15]
PIVOTS = "List(CAMPAIGN,OBJECTIVE_TYPE)"


def _status(cid: int) -> str:
    if cid == DRAFT:
        return "DRAFT"
    if cid == REMOVED:
        return "REMOVED"
    return "ACTIVE" if cid % 2 else "PAUSED"


def _list() -> Any:
    return _campaigns(
        [
            {
                "id": cid,
                "name": f"Example ad set {cid}",
                "status": _status(cid),
                "campaignGroupId": GROUP,
            }
            for cid in IDS
        ]
    )


def _table(ids: list[int]) -> Any:
    return _statistics(
        f"campaignIds={_ids(ids)}",
        PIVOTS,
        CAMPAIGN_FIELDS,
        [_objective_row(cid, "urn:li:objectiveType:A", impressions=3) for cid in ids],
    )


async def test_rows_for_the_shown_campaigns_and_the_removed_one_named(mock_page):
    browser = _Browser(mock_page)
    browser.loads[_url("/campaigns")] = [_list(), _table(SHOWN)]

    result = await _run(mock_page, browser, lambda r: r.get_campaign_analytics(ACCOUNT))

    assert sorted(row["entity"]["id"] for row in result["analytics"]["rows"]) == SHOWN
    assert result["uncovered"] == [
        {
            "kind": "campaign",
            "id": REMOVED,
            "name": f"Example ad set {REMOVED}",
            "status": "REMOVED",
        }
    ]
    error = result["section_errors"]["analytics"]
    assert error["error_type"] == "page_limit"
    message = error["error_message"]
    assert (
        f"1 of its 16 campaigns that can have delivery was not covered: {REMOVED}."
        in message
    )
    assert str(DRAFT) not in message
    assert "not the ad account's totals" in message
    assert browser.replayed == []


async def test_ids_covering_every_campaign_but_the_draft_are_complete(mock_page):
    browser = _Browser(mock_page)
    browser.loads[_url("/campaigns")] = [_list(), _table([*SHOWN, REMOVED])]

    result = await _run(mock_page, browser, lambda r: r.get_campaign_analytics(ACCOUNT))

    assert len(result["analytics"]["rows"]) == 16
    assert "uncovered" not in result
    assert "section_errors" not in result


async def test_a_requested_period_replays_the_partial_query_with_the_same_gap(
    mock_page,
):
    browser = _Browser(mock_page)
    browser.loads[_url("/campaigns")] = [_list(), _table(SHOWN)]
    browser.replies = [
        {
            "status": 200,
            "payload": {
                "elements": [
                    _objective_row(FIRST, "urn:li:objectiveType:A", impressions=11)
                ]
            },
        }
    ]

    result = await _run(
        mock_page,
        browser,
        lambda r: r.get_campaign_analytics(
            ACCOUNT, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31)
        ),
    )

    (sent,) = browser.replayed
    body = parse_qs(sent["body"])
    assert body["dateRange"] == [
        "(start:(day:1,month:8,year:2026),end:(day:31,month:8,year:2026))"
    ]
    assert body["campaignIds"] == [_ids(SHOWN)]
    assert body["pivots"] == [PIVOTS]
    assert body["fields"] == [CAMPAIGN_FIELDS]
    (row,) = result["analytics"]["rows"]
    assert row["impressions"] == 11
    assert [entity["id"] for entity in result["uncovered"]] == [REMOVED]
    assert result["section_errors"]["analytics"]["error_type"] == "page_limit"


async def test_group_level_skips_a_draft_group_and_names_an_archived_one(mock_page):
    groups = [GROUP, GROUP + 1, GROUP + 2]
    statuses = {GROUP: "ACTIVE", GROUP + 1: "DRAFT", GROUP + 2: "ARCHIVED"}
    browser = _Browser(mock_page)
    browser.loads[_url("/campaign-groups")] = [
        _response(
            f"campaignManagerReportingCampaignGroups?q=accountId&accountId={ACCOUNT}",
            {
                "elements": [
                    {"id": gid, "name": "Example group", "status": statuses[gid]}
                    for gid in groups
                ],
                "paging": {"total": len(groups)},
            },
        ),
        _statistics(
            f"campaignGroupIds={_ids([GROUP])}",
            "List(CAMPAIGN_GROUP)",
            GROUP_FIELDS,
            [_group_cost(GROUP, 2.5)],
            date_range=CURRENT,
        ),
    ]

    result = await _run(
        mock_page,
        browser,
        lambda r: r.get_campaign_analytics(ACCOUNT, level="campaign_group"),
    )

    (row,) = result["analytics"]["rows"]
    assert row["entity"]["id"] == GROUP
    assert [entity["id"] for entity in result["uncovered"]] == [GROUP + 2]
    message = result["section_errors"]["analytics"]["error_message"]
    assert "1 of its 2 campaign groups" in message
