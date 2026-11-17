"""The reporting table's own analytics queries, as the live page sends them (synthetic data).

The page scopes its ``q=statistics`` queries by id lists, not ``accountIds``:
campaign groups by ``campaignGroupIds`` with only the cost projected, campaigns
by chunked ``campaignIds`` pivoted by campaign and objective type. A personal
boost account's reporting is only on the boost manager, as a plain GET.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs

import pytest

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.scraping import campaign_manager_api as api_module
from linkedin_mcp_server.scraping.campaign_manager_api import analytics_call_matches
from linkedin_mcp_server.scraping.campaign_manager_payload import (
    build_analytics_rows,
    parse_cm_call,
)

from scraping.test_campaign_manager import (
    ACCOUNT,
    CM,
    CURRENT,
    GROUP,
    _account_record,
    _Browser,
    _campaigns,
    _group_list,
    _response,
    _run,
    _url,
)

BOOST_PATH = "/campaignmanager/boost/boostaccounts/"
FIRST = 100000301
GROUP_FIELDS = "baseMetrics:(costInLocalCurrency),pivotValues"
CAMPAIGN_FIELDS = (
    "objectiveMetrics:(keyResults,costPerKeyResult),"
    "baseMetrics:(costInLocalCurrency,impressions,clicks,clickThroughRate,"
    "costPerThousandImpressions,costPerClick),"
    "conversionMetrics:(externalWebsiteConversions,costPerConversion),pivotValues"
)


def _ids(ids: list[int]) -> str:
    return "List(" + ",".join(str(item) for item in ids) + ")"


def _statistics(
    scope: str,
    pivots: str,
    fields: str,
    rows: list[dict[str, Any]],
    *,
    granularity: str = "ALL",
    date_range: str = CURRENT,
) -> SimpleNamespace:
    """A tunneled POST whose whole Rest.li query, projection too, is the body."""
    return _response(
        "campaignManagerAdAnalytics",
        {"elements": rows, "paging": {"count": 10, "links": [], "start": 0}},
        method="POST",
        body=(
            f"q=statistics&pivots={pivots}&timeGranularity={granularity}"
            f"&dateRange={date_range}&{scope}&fields={fields}"
        ),
    )


def _campaign_list(ids: list[int], total: int) -> SimpleNamespace:
    response = _campaigns(
        [{"id": cid, "name": "Example ad set", "campaignGroupId": GROUP} for cid in ids]
    )
    response.json.return_value["paging"]["total"] = total
    return response


def _objective_row(cid: int, objective: str, **metrics: Any) -> dict[str, Any]:
    return {
        "pivotValues": [
            {"id": f"urn:li:sponsoredCampaign:{cid}", "displayText": "Example"},
            {"id": objective, "displayText": "Example objective"},
        ],
        "baseMetrics": metrics,
    }


def _group_cost(gid: int, cost: float) -> dict[str, Any]:
    return {
        "pivotValues": [{"id": f"urn:li:sponsoredCampaignGroup:{gid}"}],
        "baseMetrics": {"costInLocalCurrency": cost},
    }


def test_level_query_accepts_objective_type_and_any_base_metric():
    def call(pivots: str, fields: str, granularity: str = "ALL"):
        return parse_cm_call(
            f"{CM}campaignManagerAdAnalytics?q=statistics&pivots={pivots}"
            f"&timeGranularity={granularity}&dateRange={CURRENT}&fields={fields}",
            "GET",
            {},
            None,
        )

    def matches(pivots: str, fields: str, pivot: str, granularity: str = "ALL"):
        made = call(pivots, fields, granularity)
        assert made is not None
        return analytics_call_matches(made, q="statistics", pivot=pivot)

    assert matches("List(CAMPAIGN_GROUP)", GROUP_FIELDS, "CAMPAIGN_GROUP")
    assert matches("List(CAMPAIGN,OBJECTIVE_TYPE)", CAMPAIGN_FIELDS, "CAMPAIGN")
    assert not matches("List(ACCOUNT)", CAMPAIGN_FIELDS, "CAMPAIGN")
    assert not matches("List(CAMPAIGN,ACCOUNT)", CAMPAIGN_FIELDS, "CAMPAIGN")
    assert not matches(
        "List(CAMPAIGN,OBJECTIVE_TYPE)", CAMPAIGN_FIELDS, "CAMPAIGN", "DAILY"
    )
    # The sparkline projects only key results.
    assert not matches(
        "List(CAMPAIGN,OBJECTIVE_TYPE)",
        "dateRange,objectiveMetrics:(keyResults),pivotValues",
        "CAMPAIGN",
    )


def test_rows_of_one_entity_sum_and_ratios_are_recomputed_not_averaged():
    rows = build_analytics_rows(
        {
            "elements": [
                _objective_row(
                    FIRST,
                    "urn:li:objectiveType:A",
                    impressions=1000,
                    clicks=10,
                    costInLocalCurrency=20.0,
                    clickThroughRate=0.01,
                    costPerClick=2.0,
                    costPerThousandImpressions=20.0,
                ),
                _objective_row(
                    FIRST,
                    "urn:li:objectiveType:B",
                    impressions=3000,
                    clicks=90,
                    costInLocalCurrency=60.0,
                    clickThroughRate=0.03,
                    costPerClick=0.6667,
                    costPerThousandImpressions=20.0,
                ),
            ]
        },
        "CAMPAIGN",
    )

    assert rows is not None
    (row,) = rows
    assert row["entity"]["id"] == FIRST
    assert row["impressions"] == 4000
    assert row["clicks"] == 100
    assert row["costInLocalCurrency"] == 80.0
    assert row["clickThroughRate"] == pytest.approx(100 / 4000)
    assert row["costPerClick"] == pytest.approx(0.8)
    assert row["costPerThousandImpressions"] == pytest.approx(20.0)
    assert "keyResults" not in row
    assert "costPerKeyResult" not in row


def test_a_metric_missing_from_one_row_of_an_entity_is_absent_not_partial():
    rows = build_analytics_rows(
        {
            "elements": [
                _objective_row(
                    FIRST, "urn:li:objectiveType:A", impressions=5, clicks=1
                ),
                _objective_row(FIRST, "urn:li:objectiveType:B", impressions=7),
            ]
        },
        "CAMPAIGN",
    )

    assert rows is not None
    (row,) = rows
    assert row["impressions"] == 12
    assert "clicks" not in row
    assert "clickThroughRate" not in row


async def test_group_level_reads_the_cost_only_id_scoped_query(mock_page):
    groups = [GROUP, GROUP + 1]
    browser = _Browser(mock_page)
    url = _url("/campaign-groups")
    browser.loads[url] = [
        _account_record(),
        _group_list(groups, 2),
        _statistics(
            f"campaignGroupIds={_ids(groups)}",
            "List(CAMPAIGN_GROUP)",
            GROUP_FIELDS,
            [_group_cost(GROUP, 12.5)],
        ),
    ]

    result = await _run(
        mock_page,
        browser,
        lambda r: r.get_campaign_analytics(ACCOUNT, level="campaign_group"),
    )

    (row,) = result["analytics"]["rows"]
    assert row["entity"]["id"] == GROUP
    assert row["costInLocalCurrency"] == 12.5
    assert "impressions" not in row
    assert "section_errors" not in result
    assert browser.replayed == []


async def test_campaign_level_merges_chunks_by_campaign_and_objective_type(mock_page):
    ids = [FIRST, FIRST + 1, FIRST + 2]
    browser = _Browser(mock_page)
    url = _url("/campaigns")
    browser.loads[url] = [
        _account_record(),
        _campaign_list(ids, 3),
        # Not the table's rows: an account total and a daily series.
        _statistics(
            f"campaignIds={_ids(ids)}",
            "List(ACCOUNT)",
            CAMPAIGN_FIELDS,
            [],
        ),
        _statistics(
            f"campaignIds={_ids(ids)}",
            "List(CAMPAIGN,OBJECTIVE_TYPE)",
            "dateRange,objectiveMetrics:(keyResults),pivotValues",
            [],
            granularity="DAILY",
        ),
        _statistics(
            f"campaignIds={_ids(ids[:2])}",
            "List(CAMPAIGN,OBJECTIVE_TYPE)",
            CAMPAIGN_FIELDS,
            [
                _objective_row(
                    FIRST, "urn:li:objectiveType:A", impressions=10, clicks=1
                ),
                _objective_row(
                    FIRST, "urn:li:objectiveType:B", impressions=30, clicks=3
                ),
            ],
        ),
        _statistics(
            f"campaignIds={_ids(ids[2:])}",
            "List(CAMPAIGN,OBJECTIVE_TYPE)",
            CAMPAIGN_FIELDS,
            [_objective_row(FIRST + 2, "urn:li:objectiveType:A", impressions=5)],
        ),
    ]

    result = await _run(mock_page, browser, lambda r: r.get_campaign_analytics(ACCOUNT))

    rows = {row["entity"]["id"]: row for row in result["analytics"]["rows"]}
    assert set(rows) == set(ids) - {FIRST + 1}
    assert rows[FIRST]["impressions"] == 40
    assert rows[FIRST]["clickThroughRate"] == pytest.approx(4 / 40)
    assert rows[FIRST + 2]["impressions"] == 5
    assert "section_errors" not in result


async def test_campaign_ids_short_of_the_account_report_a_page_limit(mock_page):
    ids = [FIRST, FIRST + 1, FIRST + 2]
    browser = _Browser(mock_page)
    url = _url("/campaigns")
    browser.loads[url] = [
        _campaign_list(ids, 3),
        _statistics(
            f"campaignIds={_ids(ids[:2])}",
            "List(CAMPAIGN,OBJECTIVE_TYPE)",
            CAMPAIGN_FIELDS,
            [],
        ),
    ]

    result = await _run(mock_page, browser, lambda r: r.get_campaign_analytics(ACCOUNT))

    assert result["analytics"]["rows"] == []
    error = result["section_errors"]["analytics"]
    assert error["error_type"] == "page_limit"
    assert "reported 3 campaigns" in error["error_message"]


async def test_an_id_list_covering_the_account_is_replayed_for_a_period(mock_page):
    ids = [FIRST, FIRST + 1]
    browser = _Browser(mock_page)
    url = _url("/campaigns")
    browser.loads[url] = [
        _campaign_list(ids, 2),
        _statistics(
            f"campaignIds={_ids(ids)}",
            "List(CAMPAIGN,OBJECTIVE_TYPE)",
            CAMPAIGN_FIELDS,
            [],
        ),
    ]
    browser.replies = [
        {
            "status": 200,
            "payload": {
                "elements": [
                    _objective_row(FIRST, "urn:li:objectiveType:A", impressions=7)
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
    assert sent["method"] == "POST"
    body = parse_qs(sent["body"])
    assert body["dateRange"] == [
        "(start:(day:1,month:8,year:2026),end:(day:31,month:8,year:2026))"
    ]
    assert body["campaignIds"] == [_ids(ids)]
    assert body["pivots"] == ["List(CAMPAIGN,OBJECTIVE_TYPE)"]
    assert body["fields"] == [CAMPAIGN_FIELDS]
    assert result["analytics"]["rows"][0]["impressions"] == 7
    assert "section_errors" not in result


def _boost_record() -> SimpleNamespace:
    return _response(
        f"campaignManagerAccounts/{ACCOUNT}",
        {
            "data": {
                "id": int(ACCOUNT),
                "type": "PERSONAL",
                "status": "ACTIVE",
                "currencyCode": "EUR",
            },
            "included": [],
        },
    )


def _boost_statistics(rows: list[dict[str, Any]]) -> SimpleNamespace:
    return _response(
        "campaignManagerAdAnalytics?q=statistics&pivots=List(CAMPAIGN)"
        f"&timeGranularity=ALL&dateRange={CURRENT}&accountIds=List({ACCOUNT})"
        "&fields=baseMetrics:(impressions,clicks,costInLocalCurrency),"
        "objectiveMetrics:(keyResults),pivotValues",
        {"elements": rows, "paging": {"count": 10, "links": [], "start": 0}},
    )


async def test_boost_account_is_read_from_the_boost_manager(mock_page):
    browser = _Browser(mock_page)
    url = _url("/campaigns")
    browser.loads[url] = [_boost_record()]
    browser.routes[BOOST_PATH] = [
        _boost_statistics(
            [
                {
                    "pivotValues": [{"id": f"urn:li:sponsoredCampaign:{FIRST}"}],
                    "baseMetrics": {"impressions": 9, "clicks": 2},
                    "objectiveMetrics": {"keyResults": 3},
                }
            ]
        )
    ]

    with patch.object(api_module, "note_throttle_signal") as signal:
        result = await _run(
            mock_page,
            browser,
            lambda r: r.get_campaign_analytics(
                ACCOUNT, demographics=["MEMBER_JOB_FUNCTION"]
            ),
        )

    signal.assert_not_called()
    assert browser.charged[-1].endswith(BOOST_PATH)
    assert browser.navigated == [url]
    assert result["url"].endswith(BOOST_PATH)
    (row,) = result["analytics"]["rows"]
    assert row["keyResults"] == 3
    assert result["analytics"]["currencyCode"] == "EUR"
    error = result["section_errors"]["demographics_MEMBER_JOB_FUNCTION"]
    assert "boost account" in error["error_message"]


async def test_boost_account_without_reporting_names_the_boost_account(mock_page):
    browser = _Browser(mock_page)
    browser.loads[_url("/campaigns")] = [_boost_record()]

    with pytest.raises(LinkedInScraperException, match="personal boost account"):
        await _run(mock_page, browser, lambda r: r.get_campaign_analytics(ACCOUNT))

    assert [path for path in browser.charged if path.endswith(BOOST_PATH)] != []


def _demographic(scope: str, rows: list[dict[str, Any]]) -> SimpleNamespace:
    return _response(
        "campaignManagerAdAnalytics",
        {"elements": rows, "paging": {}},
        method="POST",
        body=(
            "q=analytics&pivot=MEMBER_JOB_FUNCTION&timeGranularity=ALL"
            f"&dateRange={CURRENT}&{scope}&fields={GROUP_FIELDS}"
        ),
    )


@pytest.mark.parametrize("total, read", [(2, True), (3, False)])
async def test_demographics_need_ids_covering_the_account(mock_page, total, read):
    ids = [FIRST, FIRST + 1]
    browser = _Browser(mock_page)
    browser.loads[_url("/campaigns")] = [
        _campaign_list(ids, total),
        _statistics(
            f"campaignIds={_ids(ids)}",
            "List(CAMPAIGN,OBJECTIVE_TYPE)",
            CAMPAIGN_FIELDS,
            [],
        ),
    ]
    browser.loads[_url("/demographics")] = [
        _demographic(
            f"campaignIds={_ids(ids)}",
            [
                {
                    "pivotValues": [{"id": "urn:li:function:1"}],
                    "baseMetrics": {"costInLocalCurrency": 4.0},
                }
            ],
        )
    ]

    result = await _run(
        mock_page,
        browser,
        lambda r: r.get_campaign_analytics(
            ACCOUNT, demographics=["MEMBER_JOB_FUNCTION"]
        ),
    )

    assert browser.replayed == []
    demographics = result["analytics"].get("demographics", {})
    if read:
        assert demographics["MEMBER_JOB_FUNCTION"][0]["costInLocalCurrency"] == 4.0
    else:
        assert "MEMBER_JOB_FUNCTION" not in demographics
        error = result["section_errors"]["demographics_MEMBER_JOB_FUNCTION"]
        assert "whole ad account" in error["error_message"]
