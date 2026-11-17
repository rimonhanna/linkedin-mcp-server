"""Tests for Campaign Manager read workflows over a fake page (synthetic data)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs

import pytest

from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
    LinkedInScraperException,
)
from linkedin_mcp_server.scraping import campaign_manager as reader_module
from linkedin_mcp_server.scraping import campaign_manager_guard as guard_module
from linkedin_mcp_server.scraping.campaign_manager import CampaignManagerReader
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

CM = "https://www.linkedin.com/campaign-manager-api/"
ACCOUNT = "100000001"
CAMPAIGN = "100000301"
GROUP = 100000201
TUNNEL = {
    "x-http-method-override": "GET",
    "content-type": "application/x-www-form-urlencoded",
}
CURRENT = "(start:(day:1,month:10,year:2026),end:(day:31,month:10,year:2026))"
PREVIOUS = "(start:(day:1,month:9,year:2026),end:(day:30,month:9,year:2026))"
REPORTING_FIELDS = (
    "fields=baseMetrics:(costInLocalCurrency,impressions,clicks),pivotValues"
)


def _response(
    path: str,
    payload: Any = None,
    *,
    method: str = "GET",
    body: str | None = None,
    status: int = 200,
) -> SimpleNamespace:
    headers = TUNNEL if method == "POST" else {}
    request = SimpleNamespace(
        method=method,
        url=CM + path,
        headers=headers,
        post_data=body,
        all_headers=AsyncMock(return_value={**headers, "csrf-token": "token"}),
    )
    return SimpleNamespace(
        url=CM + path,
        status=status,
        request=request,
        json=AsyncMock(return_value=payload),
    )


def _emit(page: Any, responses: list[SimpleNamespace]) -> None:
    for response in responses:
        for callback in list(page.listeners.get("request", [])):
            callback(response.request)
        for callback in list(page.listeners.get("response", [])):
            callback(response)


def _account_record() -> SimpleNamespace:
    return _response(
        f"campaignManagerAccounts/{ACCOUNT}",
        {
            "data": {
                "id": int(ACCOUNT),
                "name": "Example account",
                "status": "ACTIVE",
                "type": "BUSINESS",
                "currencyCode": "EUR",
            },
            "included": [],
        },
    )


def _groups(*ids: int) -> SimpleNamespace:
    return _response(
        f"campaignManagerReportingCampaignGroups?q=accountId&accountId={ACCOUNT}",
        {
            "elements": [
                {"id": gid, "name": "Example group", "status": "ACTIVE"} for gid in ids
            ],
            "paging": {"total": len(ids)},
        },
    )


def _campaigns(elements: list[dict[str, Any]], extra: str = "") -> SimpleNamespace:
    return _response(
        "campaignManagerReportingCampaigns?q=accountAndCampaignGroups"
        f"&accountId={ACCOUNT}{extra}",
        {"elements": elements, "paging": {"total": len(elements)}},
    )


def _analytics(body: str, rows: list[dict[str, Any]], status: int = 200):
    return _response(
        f"campaignManagerAdAnalytics?{REPORTING_FIELDS}",
        {"elements": rows, "paging": {}},
        method="POST",
        body=body,
        status=status,
    )


def _statistics_body(date_range: str, pivot: str = "CAMPAIGN") -> str:
    return (
        f"q=statistics&pivots=List({pivot})&timeGranularity=ALL"
        f"&dateRange={date_range}&accountIds=List({ACCOUNT})"
    )


def _row(urn: str, impressions: int) -> dict[str, Any]:
    return {
        "pivotValues": [{"id": urn, "displayText": "Example"}],
        "baseMetrics": {"impressions": impressions, "clicks": 1},
    }


class _Browser:
    """Script what each page view and in-app route change makes the page emit."""

    def __init__(self, page: Any) -> None:
        self.page = page
        self.loads: dict[str, list[SimpleNamespace]] = {}
        self.redirects: set[str] = set()
        self.routes: dict[str, list[SimpleNamespace]] = {}
        self.replies: list[dict[str, Any]] = []
        self.charged: list[str] = []
        self.navigated: list[str] = []
        self.replayed: list[dict[str, Any]] = []
        self.after_navigate: list[Any] = []

    async def navigate(self, url: str) -> None:
        self.navigated.append(url)
        self.page.url = (
            f"https://www.linkedin.com/campaignmanager/accounts/{ACCOUNT}"
            "/campaigns/new/setup?isNewAdvertiser=true"
            if url in self.redirects
            else url
        )
        _emit(self.page, self.loads.get(url, []))
        for request in self.after_navigate:
            for callback in list(self.page.listeners.get("request", [])):
                callback(request)

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if "linkedin-mcp:cm-route" in script:
            self.page.url = "https://www.linkedin.com" + arg
            _emit(self.page, self.routes.get(arg, []))
            return True
        if "linkedin-mcp:cm-analytics-replay" in script:
            self.replayed.append(arg)
            return self.replies.pop(0)
        raise AssertionError(script)

    @contextmanager
    def running(self) -> Iterator[None]:
        self.page.evaluate = AsyncMock(side_effect=self.evaluate)
        with (
            patch.object(
                PageNavigator, "_navigate_to_page", AsyncMock(side_effect=self.navigate)
            ),
            patch.object(
                PageNavigator,
                "_charge_navigation",
                MagicMock(side_effect=self.charged.append),
            ),
            patch.object(ScrapingSession, "check_rate_limit", new_callable=AsyncMock),
            patch.object(ScrapingSession, "dismiss_modal", new_callable=AsyncMock),
            patch.object(reader_module, "_READY_TIMEOUT", 0.2),
            patch.object(reader_module, "_OPTIONAL_WAIT", 0.1),
            patch.object(reader_module, "_BOOST_ON_LIST_WAIT", 0.1),
        ):
            yield


def _reader(page: Any) -> CampaignManagerReader:
    session = ScrapingSession(page)
    return CampaignManagerReader(session, PageNavigator(session))


def _url(suffix: str) -> str:
    return f"https://www.linkedin.com/campaignmanager/accounts/{ACCOUNT}{suffix}"


async def _run(
    page: Any, browser: _Browser, call: Callable[[CampaignManagerReader], Any]
) -> Any:
    with browser.running():
        return await call(_reader(page))


# --- list_ad_accounts -------------------------------------------------------

ACCOUNTS_URL = "https://www.linkedin.com/campaignmanager/accounts"


def _account_list(total: int = 1) -> SimpleNamespace:
    return _response(
        "campaignManagerReportingAccounts?q=findAll",
        {
            "elements": [
                {
                    "id": int(ACCOUNT),
                    "name": "Example account",
                    "status": "ACTIVE",
                    "type": "BUSINESS",
                    "currencyCode": "EUR",
                }
            ],
            "paging": {"total": total},
        },
    )


def _boost_accounts() -> SimpleNamespace:
    return _response(
        "campaignManagerAccounts?q=memberBoostAccounts",
        {
            "data": {
                "elements": [
                    {
                        "id": 100000009,
                        "name": "Example boost account",
                        "type": "PERSONAL",
                        "status": "ACTIVE",
                        "reference": "urn:li:member:100000010",
                    }
                ]
            },
            "included": [],
        },
    )


async def test_list_ad_accounts_reads_boost_account_on_the_same_page(mock_page):
    browser = _Browser(mock_page)
    browser.loads[ACCOUNTS_URL] = [_account_list(), _boost_accounts()]

    result = await _run(mock_page, browser, lambda r: r.list_ad_accounts())

    assert result["url"] == ACCOUNTS_URL
    assert [a["id"] for a in result["ad_accounts"]] == [int(ACCOUNT), 100000009]
    boost = result["ad_accounts"][1]
    assert boost["boost"] is True and "organizationUrn" not in boost
    assert result["ad_accounts"][0]["uiLabel"] == "Ad account"
    assert "Example account" in result["sections"]["ad_accounts"]
    assert "section_errors" not in result
    assert browser.charged == []
    assert guard_module._GUARDS[mock_page].read_mode is True


async def test_list_ad_accounts_routes_to_boost_manager_when_list_lacks_it(mock_page):
    browser = _Browser(mock_page)
    browser.loads[ACCOUNTS_URL] = [_account_list(total=3)]
    browser.routes[reader_module._BOOST_ACCOUNTS_PATH] = [_boost_accounts()]

    result = await _run(mock_page, browser, lambda r: r.list_ad_accounts())

    assert browser.charged == [
        "https://www.linkedin.com" + reader_module._BOOST_ACCOUNTS_PATH
    ]
    assert len(result["ad_accounts"]) == 2
    assert result["section_errors"]["ad_accounts"]["error_type"] == "page_limit"


async def test_list_ad_accounts_reports_missing_boost_account(mock_page):
    browser = _Browser(mock_page)
    browser.loads[ACCOUNTS_URL] = [_account_list()]

    result = await _run(mock_page, browser, lambda r: r.list_ad_accounts())

    assert [a["id"] for a in result["ad_accounts"]] == [int(ACCOUNT)]
    assert result["section_errors"]["boost_accounts"]["error_type"] == "unavailable"


# --- list_campaigns -----------------------------------------------------------


def _campaign_elements() -> list[SimpleNamespace]:
    return [
        _campaigns([{"id": 1, "name": "x"}], extra="&count=0"),
        _campaigns(
            [{"id": int(CAMPAIGN), "name": "Example ad set", "status": "ACTIVE"}],
            extra="&fields=id,name,status",
        ),
        _campaigns(
            [
                {
                    "id": int(CAMPAIGN),
                    "campaignGroupId": GROUP,
                    "objectiveType": "WEBSITE_VISIT",
                    "dailyBudget": {"amount": "10.00", "currencyCode": "EUR"},
                }
            ]
        ),
    ]


async def test_list_campaigns_merges_projections_and_verifies_links(mock_page):
    browser = _Browser(mock_page)
    url = _url("/campaigns")
    browser.loads[url] = [_account_record(), _groups(GROUP), *_campaign_elements()]

    result = await _run(mock_page, browser, lambda r: r.list_campaigns(ACCOUNT))

    assert result["url"] == url
    assert browser.charged == []
    (campaign,) = result["campaigns"]
    assert campaign["name"] == "Example ad set"
    assert campaign["campaignGroupId"] == GROUP
    assert campaign["dailyBudget"] == {"amount": "10.00", "currencyCode": "EUR"}
    assert campaign["uiLabel"] == "Ad set"
    assert [g["id"] for g in result["campaign_groups"]] == [GROUP]
    assert result["ad_accounts"][0]["currencyCode"] == "EUR"
    assert "section_errors" not in result


async def test_list_campaigns_accepts_url_form_of_account(mock_page):
    browser = _Browser(mock_page)
    url = _url("/campaigns")
    browser.loads[url] = [_groups(GROUP), *_campaign_elements()]

    result = await _run(
        mock_page, browser, lambda r: r.list_campaigns(_url("/overview"))
    )
    assert browser.navigated == [url]
    assert result["campaigns"][0]["id"] == int(CAMPAIGN)


async def test_list_campaigns_refuses_campaign_outside_the_groups(mock_page):
    browser = _Browser(mock_page)
    url = _url("/campaigns")
    browser.loads[url] = [
        _groups(GROUP),
        _campaigns([{"id": int(CAMPAIGN), "campaignGroupId": 100000999}]),
    ]
    with pytest.raises(LinkedInScraperException, match="outside"):
        await _run(mock_page, browser, lambda r: r.list_campaigns(ACCOUNT))


async def test_list_campaigns_refuses_campaign_of_another_account(mock_page):
    browser = _Browser(mock_page)
    url = _url("/campaigns")
    browser.loads[url] = [
        _groups(GROUP),
        _campaigns([{"id": int(CAMPAIGN), "accountId": 100000002}]),
    ]
    with pytest.raises(LinkedInScraperException, match="another ad account"):
        await _run(mock_page, browser, lambda r: r.list_campaigns(ACCOUNT))


async def test_setup_redirect_takes_exactly_one_route_hop(mock_page):
    browser = _Browser(mock_page)
    url = _url("/campaigns")
    path = f"/campaignmanager/accounts/{ACCOUNT}/campaigns"
    browser.redirects.add(url)
    browser.loads[url] = [_account_record(), _groups(GROUP)]
    browser.routes[path] = _campaign_elements()

    result = await _run(mock_page, browser, lambda r: r.list_campaigns(ACCOUNT))

    assert browser.charged == ["https://www.linkedin.com" + path]
    route_calls = [
        call
        for call in mock_page.evaluate.await_args_list
        if "linkedin-mcp:cm-route" in call.args[0]
    ]
    assert len(route_calls) == 1
    assert result["url"] == url
    assert result["campaigns"][0]["id"] == int(CAMPAIGN)


async def test_setup_redirect_without_payload_returns_honest_partial(mock_page):
    browser = _Browser(mock_page)
    url = _url("/campaigns")
    browser.redirects.add(url)
    browser.loads[url] = [_account_record(), _groups(GROUP)]

    result = await _run(mock_page, browser, lambda r: r.list_campaigns(ACCOUNT))

    assert len(browser.charged) == 1
    assert result["section_errors"]["campaigns"]["error_type"] == (
        "account_setup_redirect"
    )
    assert "campaigns" not in result
    assert [g["id"] for g in result["campaign_groups"]] == [GROUP]
    assert result["ad_accounts"][0]["id"] == int(ACCOUNT)


async def test_aborted_app_write_does_not_stall_the_read(mock_page):
    # The guard aborts billingConfiguration?action=findOrCreate: the page then
    # sees a request and no response, which nothing waits for.
    browser = _Browser(mock_page)
    url = _url("/campaigns")
    write = _response(
        f"campaignManagerAccounts/{ACCOUNT}/billingConfiguration?action=findOrCreate",
        method="POST",
        body="{}",
    )
    browser.loads[url] = [_groups(GROUP), *_campaign_elements()]
    browser.after_navigate.append(write.request)
    result = await _run(mock_page, browser, lambda r: r.list_campaigns(ACCOUNT))
    assert result["campaigns"][0]["id"] == int(CAMPAIGN)


# --- get_campaign -------------------------------------------------------------


def _campaign_record(account: int = int(ACCOUNT)) -> SimpleNamespace:
    return _response(
        f"campaignManagerCampaigns/{CAMPAIGN}",
        {
            "data": {
                "id": int(CAMPAIGN),
                "accountId": account,
                "campaignGroupId": GROUP,
                "name": "Example ad set",
                "status": "DRAFT",
                "targetingCriteria": {
                    "include": {
                        "and": [
                            {
                                "or": [
                                    {
                                        "facet": {
                                            "urn": "urn:li:adTargetingFacet:locations",
                                            "name": "Locations",
                                        },
                                        "segments": [
                                            {"urn": "urn:li:geo:100", "name": "Example"}
                                        ],
                                    }
                                ]
                            }
                        ]
                    },
                    "exclude": {"or": []},
                },
            },
            "included": [],
        },
    )


def _group_record() -> SimpleNamespace:
    return _response(
        f"campaignManagerCampaignGroups/{GROUP}",
        {"id": GROUP, "accountId": int(ACCOUNT), "name": "Example group"},
    )


def _creatives() -> SimpleNamespace:
    urn = "urn:li:sponsoredCreative:100000401"
    return _response(
        f"campaignManagerCreatives?q=campaignIds&campaignIds=List({CAMPAIGN})",
        {
            "data": {"elements": [urn]},
            "included": [
                {
                    "entityUrn": urn,
                    "campaign": f"urn:li:sponsoredCampaign:{CAMPAIGN}",
                    "status": "ACTIVE",
                }
            ],
        },
    )


async def test_get_campaign_reads_record_group_and_creatives(mock_page):
    browser = _Browser(mock_page)
    url = _url(f"/campaigns/{CAMPAIGN}/details")
    browser.loads[url] = [_campaign_record(), _group_record(), _creatives()]

    result = await _run(mock_page, browser, lambda r: r.get_campaign(ACCOUNT, CAMPAIGN))

    assert result["url"] == url
    targeting = result["campaign"]["targeting"]
    assert targeting["include"][0]["anyOf"][0]["segments"][0]["name"] == "Example"
    assert result["campaign_groups"][0]["id"] == GROUP
    assert result["creatives"][0]["id"] == 100000401
    assert "section_errors" not in result
    assert "Example ad set" in result["sections"]["campaign"]


async def test_get_campaign_reports_missing_creatives_and_keeps_the_rest(mock_page):
    browser = _Browser(mock_page)
    url = _url(f"/campaigns/{CAMPAIGN}/details")
    browser.loads[url] = [_campaign_record(), _group_record()]

    result = await _run(mock_page, browser, lambda r: r.get_campaign(ACCOUNT, CAMPAIGN))

    assert result["section_errors"]["creatives"]["error_type"] == "unavailable"
    assert "creatives" not in result
    assert result["campaign"]["id"] == int(CAMPAIGN)
    assert result["campaign_groups"][0]["id"] == GROUP


async def test_get_campaign_refuses_account_mismatch(mock_page):
    browser = _Browser(mock_page)
    url = _url(f"/campaigns/{CAMPAIGN}/details")
    browser.loads[url] = [_campaign_record(account=100000002)]
    with pytest.raises(InvalidReferenceError, match="does not belong"):
        await _run(mock_page, browser, lambda r: r.get_campaign(ACCOUNT, CAMPAIGN))


async def test_get_campaign_drops_a_group_that_is_not_the_campaigns(mock_page):
    browser = _Browser(mock_page)
    url = _url(f"/campaigns/{CAMPAIGN}/details")
    wrong = _response(
        f"campaignManagerCampaignGroups/{GROUP}",
        {"id": GROUP, "accountId": 100000002, "name": "Example group"},
    )
    browser.loads[url] = [_campaign_record(), wrong, _creatives()]

    result = await _run(mock_page, browser, lambda r: r.get_campaign(ACCOUNT, CAMPAIGN))
    assert "campaign_groups" not in result
    assert result["section_errors"]["campaign_groups"]["error_type"] == "unavailable"


# --- get_campaign_analytics ---------------------------------------------------


def _level_wave(rows: list[dict[str, Any]]) -> list[SimpleNamespace]:
    return [
        _account_record(),
        _analytics(_statistics_body(PREVIOUS), [_row("urn:li:sponsoredCampaign:9", 1)]),
        _analytics(_statistics_body(CURRENT), rows),
    ]


async def test_analytics_uses_the_page_period_without_a_replay(mock_page):
    browser = _Browser(mock_page)
    url = _url("/campaigns")
    browser.loads[url] = _level_wave([_row(f"urn:li:sponsoredCampaign:{CAMPAIGN}", 50)])

    result = await _run(mock_page, browser, lambda r: r.get_campaign_analytics(ACCOUNT))

    analytics = result["analytics"]
    assert analytics["window"] == {
        "start": "2026-10-01",
        "end": "2026-10-31",
        "source": "page",
    }
    assert analytics["currencyCode"] == "EUR"
    assert analytics["rows"][0]["impressions"] == 50
    assert analytics["rows"][0]["entity"]["id"] == int(CAMPAIGN)
    assert browser.replayed == []


async def test_analytics_empty_period_gives_no_rows(mock_page):
    browser = _Browser(mock_page)
    url = _url("/campaign-groups")
    browser.loads[url] = [
        _analytics(_statistics_body(CURRENT, pivot="CAMPAIGN_GROUP"), [])
    ]

    result = await _run(
        mock_page,
        browser,
        lambda r: r.get_campaign_analytics(ACCOUNT, level="campaign_group"),
    )
    assert result["analytics"]["rows"] == []
    assert result["analytics"]["level"] == "campaign_group"


async def test_analytics_replays_the_page_query_for_a_requested_period(mock_page):
    browser = _Browser(mock_page)
    url = _url("/campaigns")
    browser.loads[url] = _level_wave([])
    browser.replies = [
        {
            "status": 200,
            "payload": {"elements": [_row(f"urn:li:sponsoredCampaign:{CAMPAIGN}", 7)]},
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
    assert body["accountIds"] == [f"List({ACCOUNT})"]
    assert result["analytics"]["window"]["source"] == "requested"
    assert result["analytics"]["rows"][0]["impressions"] == 7


async def test_analytics_refuses_a_reply_with_the_wrong_urn_type(mock_page):
    browser = _Browser(mock_page)
    url = _url("/campaigns")
    browser.loads[url] = _level_wave([_row("urn:li:sponsoredCreative:1", 5)])
    with pytest.raises(LinkedInScraperException, match="analytics"):
        await _run(mock_page, browser, lambda r: r.get_campaign_analytics(ACCOUNT))


async def test_analytics_ignores_a_query_for_another_account(mock_page):
    browser = _Browser(mock_page)
    url = _url("/campaigns")
    browser.loads[url] = [
        _analytics(
            _statistics_body(CURRENT).replace(
                f"accountIds=List({ACCOUNT})", "accountIds=List(100000002)"
            ),
            [],
        )
    ]
    with pytest.raises(LinkedInScraperException, match="did not return"):
        await _run(mock_page, browser, lambda r: r.get_campaign_analytics(ACCOUNT))


async def test_analytics_demographics_passive_replayed_and_refused(mock_page):
    browser = _Browser(mock_page)
    url = _url("/campaigns")
    demographics_url = _url("/demographics")
    browser.loads[url] = _level_wave([])

    def demographic(pivot: str) -> str:
        return (
            f"q=analytics&pivot={pivot}&timeGranularity=ALL&dateRange={CURRENT}"
            f"&accountIds=List({ACCOUNT})"
        )

    browser.loads[demographics_url] = [
        _analytics(demographic("MEMBER_JOB_FUNCTION"), [_row("urn:li:function:1", 30)])
    ]
    browser.replies = [
        {"status": 200, "payload": {"elements": [_row("urn:li:seniority:3", 20)]}},
        {"status": 400, "payload": None},
    ]

    result = await _run(
        mock_page,
        browser,
        lambda r: r.get_campaign_analytics(
            ACCOUNT,
            demographics=["MEMBER_JOB_FUNCTION", "MEMBER_SENIORITY", "MEMBER_INDUSTRY"],
        ),
    )

    assert browser.navigated == [url, demographics_url]
    assert len(browser.replayed) == 2
    assert parse_qs(browser.replayed[0]["body"])["pivot"] == ["MEMBER_SENIORITY"]
    demographics = result["analytics"]["demographics"]
    assert demographics["MEMBER_JOB_FUNCTION"][0]["impressions"] == 30
    assert demographics["MEMBER_SENIORITY"][0]["impressions"] == 20
    assert "MEMBER_INDUSTRY" not in demographics
    error = result["section_errors"]["demographics_MEMBER_INDUSTRY"]
    assert "HTTP 400" in error["error_message"]


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"level": "account"}, "level"),
        ({"demographics": ["MEMBER_SECRET"]}, "Unknown demographics"),
        ({"start_date": date(2026, 8, 2)}, "both"),
        ({"start_date": date(2026, 8, 2), "end_date": date(2026, 8, 1)}, "after"),
        ({"start_date": date(2026, 8, 1), "end_date": date(2099, 1, 1)}, "future"),
        ({"start_date": date(2024, 1, 1), "end_date": date(2026, 1, 1)}, "366"),
    ],
)
async def test_analytics_input_checks_happen_before_the_browser(
    mock_page, kwargs, match
):
    browser = _Browser(mock_page)
    with pytest.raises(InvalidReferenceError, match=match):
        await _run(
            mock_page, browser, lambda r: r.get_campaign_analytics(ACCOUNT, **kwargs)
        )
    assert browser.navigated == []
