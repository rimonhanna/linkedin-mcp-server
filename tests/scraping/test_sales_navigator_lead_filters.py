"""The ICP filters of Sales Navigator lead search (synthetic data)."""

from __future__ import annotations

from typing import Any

import pytest

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.scraping.contracts import FilterValidationError
from linkedin_mcp_server.scraping.sales_navigator_lead_filters import (
    ACCOUNT_LIST,
    FUNCTION,
    LEAD_LIST,
    RELATIONSHIP,
    YEARS_AT_CURRENT_COMPANY,
    YEARS_IN_CURRENT_POSITION,
    function_ids,
    tenure_ids,
)
from linkedin_mcp_server.scraping.sales_navigator_payload import (
    COMPANY_HEADCOUNT,
    SearchFilter,
    build_lead,
    build_search_query,
    missing_echoes,
    search_page_url,
)

from .test_sales_navigator import _boot, _Browser, _lead, _run, _search

ACCOUNT_LIST_ID = "100000401"
LEAD_LIST_ID = "100000402"
OTHER_LEAD_LIST_ID = "100000403"

ICP_FILTERS = [
    SearchFilter(RELATIONSHIP, ids=("F",)),
    SearchFilter(FUNCTION, ids=("8", "13")),
    SearchFilter(COMPANY_HEADCOUNT, ids=("C", "D")),
    SearchFilter(YEARS_IN_CURRENT_POSITION, ids=("1",)),
    SearchFilter(YEARS_AT_CURRENT_COMPANY, ids=("3", "5")),
    SearchFilter(ACCOUNT_LIST, ids=(ACCOUNT_LIST_ID,)),
    SearchFilter(LEAD_LIST, ids=(LEAD_LIST_ID,), excluded_ids=(OTHER_LEAD_LIST_ID,)),
]
ICP_ARGS: dict[str, Any] = {
    "network": ["F"],
    "function": ["engineering", "Information Technology"],
    "company_size": ["11-50", "D"],
    "years_in_current_role": ["less_than_1"],
    "years_at_current_company": ["3_to_5", "more_than_10"],
    "account_list": [ACCOUNT_LIST_ID],
    "lead_list": [LEAD_LIST_ID],
    "exclude_lead_list": [OTHER_LEAD_LIST_ID],
}


async def test_every_icp_filter_reaches_the_search_url(mock_page):
    browser = _Browser(mock_page)
    query = build_search_query(None, ICP_FILTERS)
    url = search_page_url("people", query, 1)
    browser.loads[url] = [
        *_boot(),
        _search(
            "salesApiLeadSearch", query, 0, [_lead("ACwAAA0001", "A", degree=1)], 1
        ),
    ]

    result = await _run(mock_page, browser, lambda r: r.search_leads(**ICP_ARGS))

    assert browser.navigated == [url]
    assert [lead["degree"] for lead in result["leads"]] == [1]


async def test_a_filter_the_page_dropped_is_named_and_nothing_reported(mock_page):
    browser = _Browser(mock_page)
    query = build_search_query(None, ICP_FILTERS)
    url = search_page_url("people", query, 1)
    # The page sent the search without the lead-list exclusion.
    sent = build_search_query(
        None,
        [*ICP_FILTERS[:-1], SearchFilter(LEAD_LIST, ids=(LEAD_LIST_ID,))],
    )
    browser.loads[url] = [*_boot(), _search("salesApiLeadSearch", sent, 0, [], 0)]

    with pytest.raises(LinkedInScraperException, match="ignored the lead_list"):
        await _run(mock_page, browser, lambda r: r.search_leads(**ICP_ARGS))


def test_lead_lists_are_sent_as_included_and_excluded_values():
    query = build_search_query(
        None,
        [SearchFilter(LEAD_LIST, ids=("1",), excluded_ids=("2",))],
    )

    assert query == (
        "(filters:List((type:LEAD_LIST,values:List("
        "(id:1,selectionType:INCLUDED),(id:2,selectionType:EXCLUDED)))))"
    )


def test_an_exclusion_sent_as_an_inclusion_is_not_an_echo():
    wanted = [SearchFilter(LEAD_LIST, excluded_ids=("2",))]
    sent = build_search_query(None, [SearchFilter(LEAD_LIST, ids=("2",))])

    assert missing_echoes(None, wanted, sent) == [LEAD_LIST]


@pytest.mark.parametrize(
    ("argument", "value", "message"),
    [
        ("network", ["1st"], "Invalid network"),
        ("function", ["astrology"], "Unknown function"),
        ("years_in_current_role", ["forever"], "years_in_current_role"),
        ("years_at_current_company", ["2_to_3"], "years_at_current_company"),
        ("account_list", ["my list"], "list_id"),
    ],
)
async def test_a_wrong_filter_value_is_refused_before_any_page_load(
    mock_page, argument, value, message
):
    browser = _Browser(mock_page)

    with pytest.raises(Exception, match=message):
        await _run(mock_page, browser, lambda r: r.search_leads(**{argument: value}))

    assert browser.navigated == []


def test_function_names_map_to_their_ids():
    assert function_ids(["Sales", "customer success & support", "19"]) == [
        "25",
        "26",
        "19",
    ]


def test_tenure_buckets_map_in_order():
    assert tenure_ids(
        ["less_than_1", "1_to_2", "3_to_5", "6_to_10", "more_than_10"], "x"
    ) == ["1", "2", "3", "4", "5"]


def test_an_unknown_function_id_is_refused():
    with pytest.raises(FilterValidationError):
        function_ids(["27"])


def test_a_lead_carries_the_identifiers_other_tools_take():
    lead = build_lead(_lead("ACwAAA0001", "A", degree=1))

    assert lead is not None
    assert (
        lead["salesProfileUrn"] == "urn:li:fs_salesProfile:(ACwAAA0001,NAME_SEARCH,abc)"
    )
    assert lead["profileUrl"] == "https://www.linkedin.com/in/ACwAAA0001"
    assert lead["memberUrn"] == "urn:li:member:100000011"
    assert lead["degree"] == 1
