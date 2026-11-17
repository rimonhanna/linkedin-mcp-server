"""Campaign Manager reads in the envelope each resource is answered in (synthetic data).

LinkedIn answers some resources normalized (``{data, included}``, for the
``application/vnd.linkedin.normalized+json+2.1`` Accept) and others plain
(``{elements, paging}``), resource by resource. Every read takes either.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from linkedin_mcp_server.core.exceptions import LinkedInScraperException

from scraping.test_campaign_manager import (
    ACCOUNT,
    CAMPAIGN,
    CURRENT,
    GROUP,
    _analytics,
    _Browser,
    _campaign_record,
    _response,
    _row,
    _run,
    _statistics_body,
    _url,
)

ACCOUNTS_URL = "https://www.linkedin.com/campaignmanager/accounts"
PAGING = {"count": 10, "links": [], "start": 0}


def _normalized(elements: list[Any], *, total: int | None = None, included=()):
    paging = {**PAGING, "total": total} if total is not None else PAGING
    return {
        "data": {
            "$type": "com.example.Collection",
            "elements": elements,
            "entityUrn": "urn:li:collection:example",
            "paging": paging,
        },
        "included": list(included),
    }


def _account(account_id: int, name: str) -> dict[str, Any]:
    return {
        "$type": "com.example.Account",
        "id": account_id,
        "name": name,
        "status": "ACTIVE",
        "type": "BUSINESS",
        "currencyCode": "EUR",
    }


def _normalized_accounts(total: int = 1) -> SimpleNamespace:
    return _response(
        "campaignManagerReportingAccounts?q=findAll",
        _normalized([_account(int(ACCOUNT), "Example account")], total=total),
    )


def _normalized_boost() -> SimpleNamespace:
    boost = {
        "$type": "com.example.Account",
        "id": 100000009,
        "name": "Example boost account",
        "type": "PERSONAL",
        "status": "ACTIVE",
        "reference": "urn:li:member:100000010",
    }
    return _response(
        "campaignManagerAccounts?q=memberBoostAccounts", _normalized([boost])
    )


async def test_list_ad_accounts_reads_the_normalized_account_list(mock_page):
    browser = _Browser(mock_page)
    browser.loads[ACCOUNTS_URL] = [_normalized_accounts(total=2), _normalized_boost()]

    result = await _run(mock_page, browser, lambda r: r.list_ad_accounts())

    assert [a["id"] for a in result["ad_accounts"]] == [int(ACCOUNT), 100000009]
    assert result["ad_accounts"][1]["boost"] is True
    # paging.total sits under data in this envelope.
    assert result["section_errors"]["ad_accounts"]["error_type"] == "page_limit"


async def test_list_ad_accounts_resolves_account_refs_through_included(mock_page):
    urn = f"urn:li:sponsoredAccount:{ACCOUNT}"
    entity = {**_account(int(ACCOUNT), "Example account"), "entityUrn": urn}
    browser = _Browser(mock_page)
    browser.loads[ACCOUNTS_URL] = [
        _response(
            "campaignManagerReportingAccounts?q=findAll",
            _normalized([urn], total=1, included=[entity]),
        ),
        _normalized_boost(),
    ]

    result = await _run(mock_page, browser, lambda r: r.list_ad_accounts())

    assert result["ad_accounts"][0]["name"] == "Example account"


async def test_list_ad_accounts_refuses_an_unresolved_account_ref(mock_page):
    browser = _Browser(mock_page)
    browser.loads[ACCOUNTS_URL] = [
        _response(
            "campaignManagerReportingAccounts?q=findAll",
            _normalized([f"urn:li:sponsoredAccount:{ACCOUNT}"], total=1),
        ),
        _normalized_boost(),
    ]
    with pytest.raises(LinkedInScraperException, match="ad account list"):
        await _run(mock_page, browser, lambda r: r.list_ad_accounts())


async def test_list_campaigns_reads_normalized_reporting_lists(mock_page):
    browser = _Browser(mock_page)
    url = _url("/campaigns")
    group = {"id": GROUP, "name": "Example group", "status": "ACTIVE"}
    campaign = {
        "id": int(CAMPAIGN),
        "name": "Example ad set",
        "status": "ACTIVE",
        "campaignGroupId": GROUP,
    }
    browser.loads[url] = [
        _response(
            f"campaignManagerReportingCampaignGroups?q=accountId&accountId={ACCOUNT}",
            _normalized([group], total=1),
        ),
        _response(
            "campaignManagerReportingCampaigns?q=accountAndCampaignGroups"
            f"&accountId={ACCOUNT}",
            _normalized([campaign], total=3),
        ),
    ]

    result = await _run(mock_page, browser, lambda r: r.list_campaigns(ACCOUNT))

    assert [g["id"] for g in result["campaign_groups"]] == [GROUP]
    assert [c["id"] for c in result["campaigns"]] == [int(CAMPAIGN)]
    assert result["section_errors"]["campaigns"]["error_type"] == "page_limit"


def _normalized_group() -> SimpleNamespace:
    # The shape campaignManagerCampaignGroups/{id} answers with.
    return _response(
        f"campaignManagerCampaignGroups/{GROUP}",
        {
            "data": {
                "$type": "com.example.CampaignGroup",
                "id": GROUP,
                "accountId": int(ACCOUNT),
                "name": "Example group",
                "status": "ACTIVE",
                "entityUrn": f"urn:li:sponsoredCampaignGroup:{GROUP}",
            },
            "included": [],
        },
    )


def _plain_creatives() -> SimpleNamespace:
    # The shape campaignManagerCreatives?q=campaignIds answers with.
    return _response(
        f"campaignManagerCreatives?q=campaignIds&campaignIds=List({CAMPAIGN})",
        {
            "elements": [
                {
                    "id": "urn:li:sponsoredCreative:100000401",
                    "campaign": f"urn:li:sponsoredCampaign:{CAMPAIGN}",
                    "status": "ACTIVE",
                }
            ],
            "paging": PAGING,
        },
    )


def _campaign_data() -> dict[str, Any]:
    payload = _campaign_record().json.return_value
    return dict(payload["data"])


@pytest.mark.parametrize(
    "envelope",
    [
        pytest.param(lambda data: {"data": data, "included": []}, id="normalized"),
        pytest.param(lambda data: data, id="plain"),
        pytest.param(
            lambda data: {
                "data": f"urn:li:sponsoredCampaign:{CAMPAIGN}",
                "included": [
                    {**data, "entityUrn": f"urn:li:sponsoredCampaign:{CAMPAIGN}"}
                ],
            },
            id="urn-ref",
        ),
    ],
)
async def test_get_campaign_reads_the_record_group_and_creatives(mock_page, envelope):
    browser = _Browser(mock_page)
    url = _url(f"/campaigns/{CAMPAIGN}/details")
    record = _response(
        f"campaignManagerCampaigns/{CAMPAIGN}", envelope(_campaign_data())
    )
    browser.loads[url] = [record, _normalized_group(), _plain_creatives()]

    result = await _run(mock_page, browser, lambda r: r.get_campaign(ACCOUNT, CAMPAIGN))

    assert result["campaign"]["id"] == int(CAMPAIGN)
    assert result["campaign_groups"][0]["id"] == GROUP
    assert result["creatives"][0]["id"] == 100000401
    assert "section_errors" not in result


async def test_get_campaign_with_a_digits_id_refuses_instead_of_timing_out(mock_page):
    # The wait matches the id either way; the builder still wants a number.
    browser = _Browser(mock_page)
    url = _url(f"/campaigns/{CAMPAIGN}/details")
    data = {**_campaign_data(), "id": CAMPAIGN}
    record = _response(f"campaignManagerCampaigns/{CAMPAIGN}", {"data": data})
    browser.loads[url] = [record]

    with pytest.raises(LinkedInScraperException, match="shape this tool does not"):
        await _run(mock_page, browser, lambda r: r.get_campaign(ACCOUNT, CAMPAIGN))


async def test_get_campaign_waits_past_a_record_of_another_campaign(mock_page):
    browser = _Browser(mock_page)
    url = _url(f"/campaigns/{CAMPAIGN}/details")
    data = {**_campaign_data(), "id": int(CAMPAIGN) + 1}
    browser.loads[url] = [
        _response(f"campaignManagerCampaigns/{CAMPAIGN}", {"data": data})
    ]
    with pytest.raises(LinkedInScraperException, match="did not return"):
        await _run(mock_page, browser, lambda r: r.get_campaign(ACCOUNT, CAMPAIGN))


async def test_get_campaign_drops_a_normalized_group_of_another_account(mock_page):
    browser = _Browser(mock_page)
    url = _url(f"/campaigns/{CAMPAIGN}/details")
    wrong = _normalized_group()
    wrong.json.return_value["data"]["accountId"] = 100000002
    browser.loads[url] = [_campaign_record(), wrong, _plain_creatives()]

    result = await _run(mock_page, browser, lambda r: r.get_campaign(ACCOUNT, CAMPAIGN))

    assert "campaign_groups" not in result
    assert result["creatives"][0]["id"] == 100000401


async def test_get_campaign_waits_past_a_collection_on_the_campaign_path(mock_page):
    browser = _Browser(mock_page)
    url = _url(f"/campaigns/{CAMPAIGN}/details")
    sub = _response(
        f"campaignManagerCampaigns/{CAMPAIGN}",
        _normalized([{"id": int(CAMPAIGN)}]),
    )
    browser.loads[url] = [sub]
    with pytest.raises(LinkedInScraperException, match="did not return"):
        await _run(mock_page, browser, lambda r: r.get_campaign(ACCOUNT, CAMPAIGN))


async def test_analytics_matches_a_query_whose_projection_is_in_the_body(mock_page):
    browser = _Browser(mock_page)
    url = _url("/campaigns")
    body = (
        _statistics_body(CURRENT)
        + "&fields=baseMetrics:(costInLocalCurrency,impressions,clicks),pivotValues"
    )
    response = _analytics(body, [_row(f"urn:li:sponsoredCampaign:{CAMPAIGN}", 4)])
    response.url = response.request.url = (
        "https://www.linkedin.com/campaign-manager-api/campaignManagerAdAnalytics"
    )
    browser.loads[url] = [response]

    result = await _run(mock_page, browser, lambda r: r.get_campaign_analytics(ACCOUNT))

    assert result["analytics"]["rows"][0]["impressions"] == 4


async def test_analytics_reads_a_normalized_reply(mock_page):
    browser = _Browser(mock_page)
    url = _url("/campaigns")
    response = _analytics(_statistics_body(CURRENT), [])
    response.json.return_value = _normalized(
        [_row(f"urn:li:sponsoredCampaign:{CAMPAIGN}", 6)]
    )
    browser.loads[url] = [response]

    result = await _run(mock_page, browser, lambda r: r.get_campaign_analytics(ACCOUNT))

    assert result["analytics"]["rows"][0]["impressions"] == 6


async def test_analytics_after_a_route_hop_names_the_unread_query(mock_page):
    browser = _Browser(mock_page)
    url = _url("/campaigns")
    browser.redirects.add(url)
    path = f"/campaignmanager/accounts/{ACCOUNT}/campaigns"
    daily = _statistics_body(CURRENT).replace(
        "timeGranularity=ALL", "timeGranularity=DAILY"
    )
    browser.routes[path] = [_analytics(daily, [])]

    with pytest.raises(LinkedInScraperException) as raised:
        await _run(mock_page, browser, lambda r: r.get_campaign_analytics(ACCOUNT))

    message = str(raised.value)
    assert "none was the query this tool reads" in message
    assert "by CAMPAIGN (alone or with objective type)" in message
    assert "setup page" not in message


async def test_analytics_after_a_route_hop_names_a_refused_query(mock_page):
    browser = _Browser(mock_page)
    url = _url("/campaigns")
    browser.redirects.add(url)
    path = f"/campaignmanager/accounts/{ACCOUNT}/campaigns"
    browser.routes[path] = [_analytics(_statistics_body(CURRENT), [], status=400)]

    with pytest.raises(
        LinkedInScraperException, match=r"refused every one \(HTTP 400\)"
    ):
        await _run(mock_page, browser, lambda r: r.get_campaign_analytics(ACCOUNT))


async def test_analytics_without_any_query_after_a_hop_is_a_setup_redirect(mock_page):
    browser = _Browser(mock_page)
    url = _url("/campaigns")
    browser.redirects.add(url)

    result = await _run(mock_page, browser, lambda r: r.get_campaign_analytics(ACCOUNT))

    assert result["section_errors"]["analytics"]["error_type"] == (
        "account_setup_redirect"
    )
