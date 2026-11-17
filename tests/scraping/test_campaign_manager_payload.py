"""Tests for Campaign Manager payload parsing (synthetic ids and names only)."""

from datetime import date

from linkedin_mcp_server.scraping.campaign_manager_payload import (
    build_ad_account,
    build_boost_account,
    build_analytics_rows,
    build_campaign,
    build_campaign_group,
    build_creative,
    build_targeting,
    format_restli_date_range,
    merge_by_id,
    normalized_elements,
    normalized_record,
    parse_cm_call,
    plain_elements,
    render_accounts,
    render_analytics,
    render_campaign,
    render_campaigns,
    restli_date_range,
    restli_list,
)

BASE = "https://www.linkedin.com/campaign-manager-api/"


def _facet(urn: str, name: str, segments: list[tuple[str, str]]) -> dict:
    return {
        "facet": {"urn": urn, "name": name},
        "segments": [{"urn": u, "name": n} for u, n in segments],
    }


def test_parse_plain_get_call():
    call = parse_cm_call(
        BASE + "campaignManagerReportingAccounts?q=findAll", "GET", {}, None
    )
    assert call is not None
    assert call.resource == "campaignManagerReportingAccounts"
    assert call.finder == "findAll"
    assert call.entity_id is None


def test_parse_entity_id_and_non_cm_url():
    call = parse_cm_call(BASE + "campaignManagerCampaigns/100000002", "GET", {}, None)
    assert call is not None and call.entity_id == "100000002"
    assert (
        parse_cm_call("https://www.linkedin.com/voyager/api/me", "GET", {}, None)
        is None
    )


def test_parse_tunneled_body_needs_override_header():
    body = (
        "q=statistics&pivots=List(CAMPAIGN)&timeGranularity=ALL"
        "&dateRange=(start:(day:1,month:2,year:2026),end:(day:3,month:4,year:2026))"
        "&campaignIds=List(100000002,100000003)"
    )
    url = (
        BASE + "campaignManagerAdAnalytics?fields=baseMetrics:(impressions),pivotValues"
    )
    call = parse_cm_call(url, "POST", {"X-Http-Method-Override": "get"}, body)
    assert call is not None
    assert call.finder == "statistics"
    assert call.params["pivots"] == "List(CAMPAIGN)"
    assert restli_list(call.params["campaignIds"]) == ["100000002", "100000003"]
    assert call.params["fields"] == "baseMetrics:(impressions),pivotValues"
    plain = parse_cm_call(url, "POST", {}, body)
    assert plain is not None and "q" not in plain.params


def test_date_range_round_trip():
    text = format_restli_date_range(date(2026, 2, 1), date(2026, 4, 30))
    assert text == ("(start:(day:1,month:2,year:2026),end:(day:30,month:4,year:2026))")
    assert restli_date_range(text) == (date(2026, 2, 1), date(2026, 4, 30))
    assert restli_date_range("garbage") is None
    assert (
        restli_date_range(
            "(start:(day:31,month:2,year:2026),end:(day:1,month:3,year:2026))"
        )
        is None
    )


def test_restli_list_nested_and_empty():
    assert restli_list("List(a,(b,c),d)") == ["a", "(b,c)", "d"]
    assert restli_list("List()") == []
    assert restli_list("nope") == []


def test_plain_envelope():
    assert plain_elements({"elements": [{"id": 1}], "paging": {}}) == [{"id": 1}]
    assert plain_elements({"elements": []}) == []
    assert plain_elements({"elements": [1]}) is None
    assert plain_elements([]) is None
    assert plain_elements({"data": {}}) is None


def test_normalized_envelopes():
    assert normalized_record({"data": {"id": 1}, "included": []}) == {"id": 1}
    assert normalized_record({"data": []}) is None
    refs = {
        "data": {"elements": ["urn:li:c:1", {"id": 2}]},
        "included": [{"entityUrn": "urn:li:c:1", "id": 1}],
    }
    assert normalized_elements(refs) == [
        {"entityUrn": "urn:li:c:1", "id": 1},
        {"id": 2},
    ]
    assert normalized_elements({"data": {"elements": []}, "included": []}) == []
    assert normalized_elements({"data": {"elements": ["urn:li:c:9"]}}) is None
    assert normalized_elements({"data": {}}) is None
    assert normalized_elements("x") is None


def test_build_ad_account():
    account = build_ad_account(
        {
            "id": 100000001,
            "name": "Example account",
            "status": "ACTIVE",
            "type": "BUSINESS",
            "currencyCode": "EUR",
            "servingStatuses": ["RUNNABLE"],
            "currentUserRole": "ACCOUNT_BILLING_ADMIN",
            "testAccount": False,
            "organizationReference": "urn:li:organization:100000009",
        }
    )
    assert account is not None
    assert account["kind"] == "ad_account"
    assert account["uiLabel"] == "Ad account"
    assert account["organizationUrn"] == "urn:li:organization:100000009"
    assert account["testAccount"] is False
    assert build_ad_account({"name": "no id"}) is None
    assert build_ad_account("x") is None


def test_build_boost_account_drops_member_reference():
    account = build_boost_account(
        {
            "id": 100000009,
            "name": "Example boost account",
            "type": "PERSONAL",
            "status": "ACTIVE",
            "reference": "urn:li:member:100000010",
        }
    )
    assert account is not None
    assert account["boost"] is True
    assert account["type"] == "PERSONAL"
    assert "organizationUrn" not in account
    assert build_boost_account({"name": "no id"}) is None


def test_build_campaign_group_with_iso_and_ms():
    group = build_campaign_group(
        {
            "id": 100000010,
            "name": "Example group",
            "status": "ACTIVE",
            "startsAt": 1767225600000,
            "defaultCampaignGroup": True,
            "buyingType": "AUCTION",
            "totalBudget": {"amount": "1500.00", "currencyCode": "EUR"},
        }
    )
    assert group is not None
    assert group["startsAt"] == 1767225600000
    assert group["startsAtIso"] == "2026-01-01T00:00:00Z"
    assert group["isDefault"] is True
    assert "endsAt" not in group


def test_money_stays_a_string():
    campaign = build_campaign(
        {
            "id": 100000002,
            "dailyBudget": {"amount": "50.10", "currencyCode": "EUR"},
            "unitCost": {"amount": 5.5, "currencyCode": "EUR"},
        }
    )
    assert campaign is not None
    assert campaign["dailyBudget"] == {"amount": "50.10", "currencyCode": "EUR"}
    assert "unitCost" not in campaign


def test_build_campaign_reporting_and_normalized_shapes():
    reporting = build_campaign(
        {
            "id": 100000002,
            "campaignGroupId": 100000010,
            "name": "Example set",
            "status": "ACTIVE",
            "startsAt": 1767225600000,
            "endsAt": 1769904000000,
            "totalBudget": {"amount": "900", "currencyCode": "EUR"},
        }
    )
    assert reporting is not None
    assert reporting["runSchedule"]["end"] == 1769904000000
    full = build_campaign(
        {
            "id": 100000002,
            "accountId": 100000001,
            "runSchedule": {"start": 1767225600000},
            "locale": {"country": "NL", "language": "en"},
            "audienceExpansionEnabled": False,
            "targetingCriteria": {
                "include": {"and": [{"or": [_facet("urn:f:geo", "Locations", [])]}]},
                "exclude": {"or": []},
            },
        }
    )
    assert full is not None
    assert full["accountId"] == 100000001
    assert "end" not in full["runSchedule"]
    assert full["locale"] == {"country": "NL", "language": "en"}
    assert full["audienceExpansionEnabled"] is False
    assert full["targeting"]["include"][0]["anyOf"][0]["urn"] == "urn:f:geo"
    assert build_campaign({"id": "x"}) is None


def test_targeting_and_or_exclude():
    targeting = build_targeting(
        {
            "include": {
                "and": [
                    {
                        "or": [
                            _facet(
                                "urn:f:geo",
                                "Locations",
                                [("urn:geo:1", "Example land")],
                            ),
                            _facet(
                                "urn:f:industry",
                                "Industries",
                                [("urn:ind:1", "Example")],
                            ),
                        ]
                    },
                    {
                        "or": [
                            _facet(
                                "urn:f:title", "Titles", [("urn:t:1", "Example title")]
                            )
                        ]
                    },
                ]
            },
            "exclude": {
                "or": [
                    _facet("urn:f:company", "Companies", [("urn:c:1", "Example Co")])
                ]
            },
        }
    )
    assert targeting is not None
    assert [len(group["anyOf"]) for group in targeting["include"]] == [2, 1]
    assert targeting["include"][0]["anyOf"][0]["segments"] == [
        {"urn": "urn:geo:1", "name": "Example land"}
    ]
    assert targeting["exclude"][0]["urn"] == "urn:f:company"
    assert build_targeting({"include": {"and": []}, "exclude": {"or": []}}) == {
        "include": [],
        "exclude": [],
    }
    assert build_targeting("x") is None
    assert build_targeting({}) is None


def test_build_creative_defensive():
    creative = build_creative(
        {
            "id": "urn:li:sponsoredCreative:100000020",
            "campaignId": 100000002,
            "name": 100000020,
            "statusV2": {"status": "ACTIVE"},
            "type": "SPONSORED_VIDEO",
            "destinationUrl": "https://example.com/",
        }
    )
    assert creative is not None
    assert creative["id"] == 100000020
    assert creative["name"] == "100000020"
    assert creative["status"] == "ACTIVE"
    assert build_creative({"name": "x"}) is None


def _analytics_payload(*ids: str) -> dict:
    return {
        "elements": [
            {
                "pivotValues": [{"id": urn, "displayText": "Example"}],
                "baseMetrics": {
                    "impressions": 100,
                    "clicks": 4,
                    "costInLocalCurrency": 12.5,
                },
                "objectiveMetrics": {"keyResults": 4},
            }
            for urn in ids
        ]
    }


def test_analytics_rows_omit_missing_metrics():
    rows = build_analytics_rows(
        _analytics_payload("urn:li:sponsoredCampaign:100000002"), "CAMPAIGN"
    )
    assert rows is not None
    row = rows[0]
    assert row["entity"] == {
        "kind": "campaign",
        "id": 100000002,
        "urn": "urn:li:sponsoredCampaign:100000002",
        "name": "Example",
    }
    assert row["impressions"] == 100 and row["costInLocalCurrency"] == 12.5
    assert "costPerClick" not in row and "costPerKeyResult" not in row


def test_analytics_row_with_wrong_urn_type_rejected():
    assert (
        build_analytics_rows(
            _analytics_payload(
                "urn:li:sponsoredCampaign:100000002",
                "urn:li:sponsoredCreative:100000020",
            ),
            "CAMPAIGN",
        )
        is None
    )
    assert build_analytics_rows({"elements": [{"pivotValues": []}]}, "CAMPAIGN") is None
    assert build_analytics_rows("x", "CAMPAIGN") is None


def test_analytics_demographic_pivot_and_empty():
    rows = build_analytics_rows(
        _analytics_payload("urn:li:function:7"), "MEMBER_JOB_FUNCTION"
    )
    assert rows is not None and rows[0]["entity"]["kind"] == "MEMBER_JOB_FUNCTION"
    assert (
        build_analytics_rows(_analytics_payload("urn:li:geo:7"), "MEMBER_JOB_FUNCTION")
        is None
    )
    assert build_analytics_rows({"elements": []}, "CAMPAIGN") == []


def test_merge_keeps_fullest_record():
    merged = merge_by_id(
        [
            {"id": 1, "name": "Example set", "status": "ACTIVE", "adFormats": ["A"]},
            {"id": 2, "name": "Other"},
            {"id": 1, "name": "", "status": "PAUSED", "adFormats": [], "flag": False},
        ]
    )
    assert [record["id"] for record in merged] == [1, 2]
    assert merged[0] == {
        "id": 1,
        "name": "Example set",
        "status": "PAUSED",
        "adFormats": ["A"],
        "flag": False,
    }


def test_renderers_one_line_per_entity():
    account = build_ad_account(
        {"id": 100000001, "name": "Example account", "status": "ACTIVE"}
    )
    group = build_campaign_group(
        {"id": 100000010, "name": "Example group", "status": "ACTIVE"}
    )
    campaign = build_campaign(
        {
            "id": 100000002,
            "name": "Example set",
            "status": "ACTIVE",
            "dailyBudget": {"amount": "50.10", "currencyCode": "EUR"},
        }
    )
    creative = build_creative(
        {"id": 100000020, "name": "Example ad", "status": "ACTIVE"}
    )
    assert account and group and campaign and creative
    assert render_accounts([account]) == "Example account (id 100000001): ACTIVE"
    listing = render_campaigns([group], [campaign]).splitlines()
    assert len(listing) == 2 and "daily 50.10 EUR" in listing[1]
    assert len(render_campaign(campaign, group, [creative]).splitlines()) == 3
    assert len(render_campaign(campaign, None, []).splitlines()) == 1
    rows = build_analytics_rows(
        _analytics_payload("urn:li:sponsoredCampaign:100000002"), "CAMPAIGN"
    )
    assert rows is not None
    text = render_analytics(
        rows, {"start": "2026-01-01", "end": "2026-01-31", "source": "page"}, "EUR"
    )
    lines = text.splitlines()
    assert lines[0] == "window 2026-01-01 to 2026-01-31 (page)"
    assert "costInLocalCurrency 12.5 EUR" in lines[1]
