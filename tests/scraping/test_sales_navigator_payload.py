"""Tests for Sales Navigator query building and payload parsing (synthetic data only)."""

from typing import get_args

from linkedin_mcp_server.scraping.sales_navigator_payload import (
    COMPANY_FILTER_ID,
    COMPANY_HEADCOUNT,
    COMPANY_HEADCOUNT_GROWTH,
    CURRENT_COMPANY,
    CURRENT_TITLE,
    INDUSTRY,
    REGION,
    SENIORITY_IDS,
    SENIORITY_LEVEL,
    WITHHELD_REASON,
    SearchFilter,
    SeniorityLevel,
    build_account,
    build_account_card,
    build_account_list,
    build_elements,
    build_lead,
    build_leads,
    build_listed_account,
    build_paging,
    build_search_query,
    build_withheld,
    is_withheld,
    missing_echoes,
    parse_sn_call,
    render_account,
    render_account_cards,
    render_account_lists,
    render_leads,
    render_listed_accounts,
    search_page_url,
    unrequested_echoes,
)

API = "https://www.linkedin.com/sales-api/"
COMPANY_URN = "urn:li:fs_salesCompany:100000001"
PROFILE_URN = "urn:li:fs_salesProfile:(ACwAAA100000002,NAME_SEARCH,tok1)"


def _lead_card(**extra) -> dict:
    card = {
        "entityUrn": PROFILE_URN,
        "objectUrn": "urn:li:member:100000002",
        "fullName": "Example Person",
        "firstName": "Example",
        "lastName": "Person",
        "geoRegion": "Example Region",
        "degree": 2,
        "premium": False,
        "openLink": True,
        "seniorityV2s": [{"id": 310, "displayName": "CXO"}],
        "currentPositions": [
            {
                "title": "Example Title",
                "companyName": "Example Company",
                "companyUrn": COMPANY_URN,
                "tenureAtPosition": {"numYears": 1},
                "current": True,
                "description": "not copied",
            }
        ],
        "blockThirdPartyDataSharing": False,
    }
    card.update(extra)
    return card


def test_query_encodes_keyword_structure_characters():
    query = build_search_query("cloud ai, saas: b2b (eu)'", [])
    assert query == (
        "(spellCorrectionEnabled:true,"
        "keywords:cloud%20ai%2C%20saas%3A%20b2b%20%28eu%29%27)"
    )


def test_query_with_id_text_range_and_company_filters():
    query = build_search_query(
        None,
        [
            SearchFilter(INDUSTRY, ids=("106",)),
            SearchFilter(REGION, ids=("102890719",)),
            SearchFilter(COMPANY_HEADCOUNT, ids=("B", "C")),
            SearchFilter(COMPANY_HEADCOUNT_GROWTH, minimum=5, maximum=10),
            SearchFilter(CURRENT_TITLE, ids=("35",), texts=("head of growth, emea",)),
            SearchFilter(CURRENT_COMPANY, ids=(COMPANY_FILTER_ID.format(100000003),)),
            SearchFilter(SENIORITY_LEVEL),
        ],
    )
    assert query == (
        "(filters:List("
        "(type:INDUSTRY,values:List((id:106,selectionType:INCLUDED))),"
        "(type:REGION,values:List((id:102890719,selectionType:INCLUDED))),"
        "(type:COMPANY_HEADCOUNT,values:List((id:B,selectionType:INCLUDED),"
        "(id:C,selectionType:INCLUDED))),"
        "(type:COMPANY_HEADCOUNT_GROWTH,rangeValue:(min:5,max:10)),"
        "(type:CURRENT_TITLE,values:List((id:35,selectionType:INCLUDED),"
        "(text:head%20of%20growth%2C%20emea,selectionType:INCLUDED))),"
        "(type:CURRENT_COMPANY,values:List((id:urn%3Ali%3Aorganization%3A100000003,"
        "selectionType:INCLUDED,parent:(id:0))))"
        "))"
    )


def test_open_range_and_empty_query():
    assert build_search_query(
        None, [SearchFilter(COMPANY_HEADCOUNT_GROWTH, minimum=-5)]
    ) == ("(filters:List((type:COMPANY_HEADCOUNT_GROWTH,rangeValue:(min:-5))))")
    assert build_search_query("", []) == "()"


def test_query_never_carries_recent_search_param():
    query = build_search_query(
        "x", [SearchFilter(SENIORITY_LEVEL, ids=tuple(SENIORITY_IDS.values()))]
    )
    assert "recentSearchParam" not in query
    assert "doLogHistory" not in query


def test_seniority_ids_are_numeric():
    assert SENIORITY_IDS["cxo"] == "310"
    assert SENIORITY_IDS["owner_partner"] == "320"
    assert SENIORITY_IDS["in_training"] == "100"
    assert len(SENIORITY_IDS) == 10
    assert all(value.isdigit() for value in SENIORITY_IDS.values())


def test_seniority_levels_are_the_mapped_keys():
    assert list(get_args(SeniorityLevel)) == list(SENIORITY_IDS)


def test_page_url_first_and_later_page():
    query = build_search_query("a b", [SearchFilter(INDUSTRY, ids=("106",))])
    first = search_page_url("company", query, 1)
    assert first == (
        "https://www.linkedin.com/sales/search/company?query="
        "(spellCorrectionEnabled%3Atrue,keywords%3Aa%2520b,"
        "filters%3AList((type%3AINDUSTRY,values%3AList((id%3A106,"
        "selectionType%3AINCLUDED)))))"
    )
    assert "&page=" not in first
    assert search_page_url("people", query, 3).endswith("&page=3")
    assert "/sales/search/people?query=" in search_page_url("people", query, 3)


def test_echo_check_passes_on_the_query_that_was_sent():
    filters = [
        SearchFilter(INDUSTRY, ids=("106",)),
        SearchFilter(COMPANY_HEADCOUNT_GROWTH, minimum=5),
        SearchFilter(CURRENT_TITLE, texts=("head of growth",)),
        SearchFilter(CURRENT_COMPANY, ids=(COMPANY_FILTER_ID.format(100000003),)),
    ]
    sent = build_search_query("cloud, ai", filters)
    assert missing_echoes("cloud, ai", filters, sent) == []


def test_echo_check_accepts_ui_style_labels_and_order():
    captured = (
        "(filters:List((type:SENIORITY_LEVEL,values:List((id:310,text:CXO,"
        "selectionType:INCLUDED)))),spellCorrectionEnabled:true,keywords:x%20y)"
    )
    assert (
        missing_echoes("x y", [SearchFilter(SENIORITY_LEVEL, ids=("310",))], captured)
        == []
    )


def test_echo_check_names_each_dropped_criterion():
    filters = [
        SearchFilter(INDUSTRY, ids=("106",)),
        SearchFilter(REGION, ids=("102890719",)),
        SearchFilter(COMPANY_HEADCOUNT_GROWTH, minimum=5, maximum=10),
    ]
    captured = (
        "(keywords:other,filters:List("
        "(type:INDUSTRY,values:List((id:106,selectionType:EXCLUDED))),"
        "(type:INDUSTRY,values:List((id:102890719,selectionType:INCLUDED))),"
        "(type:COMPANY_HEADCOUNT_GROWTH,rangeValue:(min:5))))"
    )
    assert missing_echoes("wanted", filters, captured) == [
        "keywords",
        INDUSTRY,
        REGION,
        COMPANY_HEADCOUNT_GROWTH,
    ]
    assert missing_echoes(None, filters, "garbage") == [
        INDUSTRY,
        REGION,
        COMPANY_HEADCOUNT_GROWTH,
    ]


def test_echo_check_names_each_unrequested_criterion():
    filters = [SearchFilter(INDUSTRY, ids=("106",))]
    sent = build_search_query(None, filters)
    assert unrequested_echoes(None, filters, sent) == []
    captured = (
        "(spellCorrectionEnabled:true,keywords:other,filters:List("
        "(type:INDUSTRY,values:List((id:106,selectionType:INCLUDED))),"
        "(type:REGION,values:List((id:102890719,selectionType:INCLUDED))),"
        "(type:REGION,values:List((id:102890720,selectionType:INCLUDED)))))"
    )
    assert missing_echoes(None, filters, captured) == []
    assert unrequested_echoes(None, filters, captured) == ["keywords", REGION]
    assert unrequested_echoes("other", filters, captured) == [REGION]


def test_parse_sn_call_keeps_restli_raw():
    call = parse_sn_call(
        API
        + "salesApiAccountSearch?q=searchQuery&query=(keywords:a%20b)&start=25&count=25",
        "get",
    )
    assert call is not None
    assert call.resource == "salesApiAccountSearch"
    assert call.finder == "searchQuery"
    assert call.method == "GET"
    assert call.entity_id is None
    assert call.params["query"] == "(keywords:a%20b)"
    assert call.params["start"] == "25"


def test_parse_sn_call_entity_and_other_urls():
    company = parse_sn_call(API + "salesApiCompanies/100000001?decoration=x", "GET")
    assert company is not None and company.entity_id == "100000001"
    lists = parse_sn_call(API + "salesApiLists/100000004", "GET")
    assert lists is not None and lists.entity_id == "100000004"
    assert parse_sn_call("https://www.linkedin.com/voyager/api/me", "GET") is None
    assert parse_sn_call(API, "GET") is None


def test_paging_capped_on_last_viewable_page():
    payload = {"paging": {"start": 975, "count": 25, "total": 1500}}
    assert build_paging(payload, 40, 25) == {
        "page": 40,
        "start": 975,
        "count": 25,
        "total": 1500,
        "resultCap": 1000,
        "capped": True,
        "more": False,
    }


def test_paging_more_and_bad_shapes():
    paging = build_paging({"paging": {"start": 0, "count": 25, "total": 30}}, 1, 25)
    assert paging is not None and paging["more"] is True and not paging["capped"]
    last = build_paging({"paging": {"start": 25, "count": 25, "total": 30}}, 2, 25)
    assert last is not None and last["more"] is False
    assert build_paging({"paging": {"start": 0, "count": 25}}, 1, 25) is None
    assert (
        build_paging({"paging": {"start": "0", "count": 25, "total": 1}}, 1, 25) is None
    )
    assert build_paging([], 1, 25) is None


def test_account_card():
    card = build_account_card(
        {
            "entityUrn": COMPANY_URN,
            "companyName": "Example Company",
            "industry": "Example Industry",
            "employeeCountRange": "1-10",
            "employeeDisplayCount": "7",
            "saved": False,
            "listCount": 0,
            "companyPictureDisplayImage": {"rootUrl": "x"},
        }
    )
    assert card == {
        "id": "100000001",
        "name": "Example Company",
        "industry": "Example Industry",
        "employeeCountRange": "1-10",
        "employeeDisplayCount": "7",
        "saved": False,
        "listCount": 0,
        "salesNavigatorUrl": "https://www.linkedin.com/sales/company/100000001",
    }
    assert build_account_card({"entityUrn": "urn:li:organization:1"}) is None
    assert build_account_card("x") is None


def test_lead_tuple_urn_parsed():
    lead = build_lead(_lead_card())
    assert lead is not None
    assert lead["id"] == "ACwAAA100000002"
    assert lead["memberUrn"] == "urn:li:member:100000002"
    assert lead["seniority"] == [{"id": "310", "name": "CXO"}]
    assert lead["currentPositions"] == [
        {
            "title": "Example Title",
            "companyName": "Example Company",
            "companyId": "100000001",
            "tenureAtPosition": {"numYears": 1},
            "current": True,
        }
    ]
    assert build_lead(_lead_card(entityUrn="urn:li:fs_salesProfile:(x,y)")) is None
    assert build_lead(_lead_card(entityUrn="urn:li:member:1")) is None


def test_withheld_lead_is_dropped_and_counted():
    withheld = _lead_card(
        blockThirdPartyDataSharing=True, fullName="Withheld Person Name"
    )
    assert is_withheld(withheld)
    assert not is_withheld(_lead_card())
    assert not is_withheld(_lead_card(blockThirdPartyDataSharing="true"))
    assert build_lead(withheld) is None
    result = build_leads([_lead_card(), withheld])
    assert result is not None
    leads, count = result
    assert count == 1
    assert [lead["fullName"] for lead in leads] == ["Example Person"]
    assert "Withheld Person Name" not in render_leads(leads)
    assert build_withheld(count) == {"count": 1, "reason": WITHHELD_REASON}
    assert build_withheld(0) is None


def test_leads_reject_a_malformed_card():
    assert build_leads([_lead_card(), {"fullName": "No Urn"}]) is None
    assert build_leads([]) == ([], 0)


def _decoration_a(**extra) -> dict:
    record = {
        "entityUrn": COMPANY_URN,
        "name": "Example Company",
        "description": "Example description",
        "industry": "Example Industry",
        "type": "Privately Held",
        "specialties": ["one", "two"],
        "website": "https://example.com",
        "yearFounded": 2001,
        "headquarters": {"country": "XX", "city": "Example City", "extra": 1},
        "revenueRange": {
            "estimatedMinRevenue": {"currencyCode": "USD", "amount": 1, "unit": "M"},
            "estimatedMaxRevenue": {"currencyCode": "USD", "amount": "2.5"},
        },
        "employeeGrowthPercentages": [
            {"timespan": "SIX_MONTHS", "percentage": 4},
            {"timespan": "YEAR", "percentage": "bad"},
        ],
        "employees": [{"entityUrn": PROFILE_URN}],
        "employeesResolutionResults": {PROFILE_URN: {"fullName": "Example Person"}},
        "pictures": [{"rootUrl": "x"}],
        "flagshipCompanyUrl": "https://www.linkedin.com/company/example/",
        "account": {"saved": True, "listCount": 2, "noteCount": 1},
    }
    record.update(extra)
    return record


def test_account_merges_both_decorations():
    decoration_b = {
        "entityUrn": COMPANY_URN,
        "employeeCount": 42,
        "employeeCountRange": "11-50",
        "employeeDisplayCount": "42",
        "decisionMakersDisplayCount": "3",
    }
    account = build_account([_decoration_a(), decoration_b], "100000001")
    assert account is not None
    assert account["name"] == "Example Company"
    assert account["employeeCount"] == 42
    assert account["employeeCountRange"] == "11-50"
    assert account["headquarters"] == {"country": "XX", "city": "Example City"}
    assert account["revenueRange"] == {
        "min": {"amount": "1", "currencyCode": "USD", "unit": "M"},
        "max": {"amount": "2.5", "currencyCode": "USD"},
    }
    assert account["employeeGrowth"] == [{"timespan": "SIX_MONTHS", "percentage": 4}]
    assert account["saved"] is True and account["listCount"] == 2
    assert account["salesNavigatorUrl"].endswith("/sales/company/100000001")
    for key in ("employees", "employeesResolutionResults", "pictures"):
        assert key not in account
    assert "Example Person" not in str(account)


def test_account_id_mismatch_rejected():
    assert build_account([_decoration_a()], "100000009") is None
    other = {"entityUrn": "urn:li:fs_salesCompany:100000009", "employeeCount": 1}
    assert build_account([_decoration_a(), other], "100000001") is None
    assert build_account([{"name": "No Id"}], "100000001") is None
    assert build_account([], "100000001") is None


def test_account_list_and_rows():
    account_list = build_account_list(
        {
            "id": 100000004,
            "name": "Example list",
            "listType": "ACCOUNT",
            "listSource": "MANUAL",
            "entityCount": 30,
            "role": "OWNER",
            "lastModifiedAt": 1767225600000,
            "creator": {"name": "not copied"},
        }
    )
    assert account_list == {
        "id": "100000004",
        "name": "Example list",
        "listType": "ACCOUNT",
        "listSource": "MANUAL",
        "entityCount": 30,
        "role": "OWNER",
        "lastModifiedAt": "2026-01-01T00:00:00Z",
    }
    assert build_account_list({"name": "No Id"}) is None

    row = build_listed_account(
        {
            "companyInfo": {
                "entityUrn": COMPANY_URN,
                "name": "Example Company",
                "industry": "Example Industry",
                "employeeCount": 9,
                "buyerIntentLevel": "LOW",
                "account": {"starred": False, "saved": True},
            },
            "recommendedLead": {"fullName": "Example Person"},
        }
    )
    assert row == {
        "id": "100000001",
        "name": "Example Company",
        "industry": "Example Industry",
        "employeeCount": 9,
        "buyerIntentLevel": "LOW",
        "saved": True,
    }
    assert build_listed_account({"recommendedLead": {}}) is None


def test_build_elements_empty_and_malformed():
    assert build_elements({"elements": []}, build_account_card) == []
    assert (
        build_elements({"elements": [{"entityUrn": "x"}]}, build_account_card) is None
    )
    assert build_elements({}, build_account_card) is None


def test_renderers_one_line_per_entity():
    card = build_account_card(
        {"entityUrn": COMPANY_URN, "companyName": "Example Company"}
    )
    assert card is not None
    assert render_account_cards([card, card]).count("\n") == 1
    assert render_account_cards([card]) == "Example Company (id 100000001)"
    lead = build_lead(_lead_card())
    assert lead is not None
    assert render_leads([lead]) == (
        "Example Person (id ACwAAA100000002): Example Title, Example Company, "
        "Example Region"
    )
    account = build_account([_decoration_a()], "100000001")
    assert account is not None
    assert "\n" not in render_account(account)
    lists = build_account_list(
        {"id": 100000004, "name": "Example list", "entityCount": 3}
    )
    assert lists is not None
    assert render_account_lists([lists]) == "Example list (id 100000004): entityCount 3"
    row = build_listed_account({"companyInfo": {"entityUrn": COMPANY_URN}})
    assert row is not None
    assert render_listed_accounts([row]) == "id 100000001"
    assert render_leads([]) == ""
