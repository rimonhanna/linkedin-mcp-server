"""One query shape per period, and coverage only from an unfiltered list (synthetic data).

The page can ask for one period by the level's pivot alone and with objective
type; reading both would count each entity twice, or sum rows projecting
different metrics into a row with none.
"""

from __future__ import annotations

from typing import Any

import pytest

from scraping.test_campaign_manager import (
    ACCOUNT,
    GROUP,
    _Browser,
    _campaigns,
    _run,
    _url,
)
from scraping.test_campaign_manager_reporting_queries import (
    CAMPAIGN_FIELDS,
    _campaign_list,
    _ids,
    _objective_row,
    _statistics,
)

FIRST = 100000501
BASE_FIELDS = "baseMetrics:(impressions,clicks,costInLocalCurrency),pivotValues"
CONVERSION_FIELDS = (
    "baseMetrics:(clicks),conversionMetrics:(externalWebsiteConversions),pivotValues"
)


def _campaign_row(cid: int, **metrics: Any) -> dict[str, Any]:
    return {
        "pivotValues": [{"id": f"urn:li:sponsoredCampaign:{cid}"}],
        "baseMetrics": metrics,
    }


@pytest.mark.parametrize("reverse", [False, True])
async def test_campaign_alone_and_with_objective_type_are_not_summed(
    mock_page, reverse
):
    alone = _statistics(
        f"campaignIds={_ids([FIRST])}",
        "List(CAMPAIGN)",
        CAMPAIGN_FIELDS,
        [_campaign_row(FIRST, impressions=100, clicks=4)],
    )
    with_objective = _statistics(
        f"campaignIds={_ids([FIRST])}",
        "List(CAMPAIGN,OBJECTIVE_TYPE)",
        CAMPAIGN_FIELDS,
        [_objective_row(FIRST, "urn:li:objectiveType:A", impressions=100, clicks=4)],
    )
    queries = [with_objective, alone] if reverse else [alone, with_objective]
    browser = _Browser(mock_page)
    browser.loads[_url("/campaigns")] = [_campaign_list([FIRST], 1), *queries]

    result = await _run(mock_page, browser, lambda r: r.get_campaign_analytics(ACCOUNT))

    (row,) = result["analytics"]["rows"]
    assert row["impressions"] == 100, "the two query shapes were summed"
    assert row["clicks"] == 4


async def test_the_shape_carrying_the_most_metrics_is_read_not_mixed(mock_page):
    browser = _Browser(mock_page)
    browser.loads[_url("/campaigns")] = [
        _campaign_list([FIRST], 1),
        _statistics(
            f"campaignIds={_ids([FIRST])}",
            "List(CAMPAIGN,OBJECTIVE_TYPE)",
            CONVERSION_FIELDS,
            [
                {
                    "pivotValues": [
                        {"id": f"urn:li:sponsoredCampaign:{FIRST}"},
                        {"id": "urn:li:objectiveType:A"},
                    ],
                    "baseMetrics": {"clicks": 4},
                    "conversionMetrics": {"externalWebsiteConversions": 2},
                }
            ],
        ),
        _statistics(
            f"campaignIds={_ids([FIRST])}",
            "List(CAMPAIGN)",
            BASE_FIELDS,
            [_campaign_row(FIRST, impressions=100, clicks=4, costInLocalCurrency=8)],
        ),
    ]

    result = await _run(mock_page, browser, lambda r: r.get_campaign_analytics(ACCOUNT))

    (row,) = result["analytics"]["rows"]
    assert row["impressions"] == 100, f"metrics lost by mixing shapes: {row}"
    assert row["costInLocalCurrency"] == 8
    assert "externalWebsiteConversions" not in row


async def test_partial_analytics_text_says_it_is_not_the_total(mock_page):
    ids = [FIRST, FIRST + 1]
    browser = _Browser(mock_page)
    browser.loads[_url("/campaigns")] = [
        _campaign_list(ids, 2),
        _statistics(
            f"campaignIds={_ids(ids[:1])}",
            "List(CAMPAIGN,OBJECTIVE_TYPE)",
            CAMPAIGN_FIELDS,
            [_objective_row(FIRST, "urn:li:objectiveType:A", impressions=3)],
        ),
    ]

    result = await _run(mock_page, browser, lambda r: r.get_campaign_analytics(ACCOUNT))

    assert result["section_errors"]["analytics"]["error_type"] == "page_limit"
    assert result["sections"]["analytics"].startswith(
        "partial, not the ad account's total"
    )


@pytest.mark.parametrize(
    "extra, filtered",
    [
        ("&search=(status:(values:List(ACTIVE,PAUSED)))", True),
        ("&fields=id,name,status", False),
    ],
)
async def test_a_status_filtered_list_proves_no_coverage(mock_page, extra, filtered):
    browser = _Browser(mock_page)
    browser.loads[_url("/campaigns")] = [
        _campaigns(
            [
                {
                    "id": FIRST,
                    "name": "Example ad set",
                    "status": "ACTIVE",
                    "campaignGroupId": GROUP,
                }
            ],
            extra=extra,
        ),
        _statistics(
            f"campaignIds={_ids([FIRST])}",
            "List(CAMPAIGN,OBJECTIVE_TYPE)",
            CAMPAIGN_FIELDS,
            [_objective_row(FIRST, "urn:li:objectiveType:A", impressions=3)],
        ),
    ]

    result = await _run(mock_page, browser, lambda r: r.get_campaign_analytics(ACCOUNT))

    errors = result.get("section_errors", {})
    if filtered:
        error = errors["analytics"]
        assert error["error_type"] == "page_limit"
        assert "filtered by status" in error["error_message"]
        assert result["sections"]["analytics"].startswith("partial")
    else:
        assert errors == {}
