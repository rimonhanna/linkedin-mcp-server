"""A list id is looked up among the member's lists before a search (synthetic data)."""

from __future__ import annotations

import pytest

from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
    LinkedInScraperException,
)
from linkedin_mcp_server.scraping.identifiers import SALES_LEAD_LISTS_URL
from linkedin_mcp_server.scraping.sales_navigator_lead_filters import LEAD_LIST
from linkedin_mcp_server.scraping.sales_navigator_payload import (
    SearchFilter,
    build_search_query,
    search_page_url,
)

from .test_sales_navigator import _boot, _Browser, _lead, _run, _search
from .test_sales_navigator_lead_lists import (
    LEAD_LIST_ID,
    QUERY,
    _lead_lists,
    _own_lists,
    _page_url,
)

UNKNOWN_ID = "7000000000000000001"


async def test_an_unknown_lead_list_is_refused_before_any_search(mock_page):
    browser = _Browser(mock_page)
    hub = _own_lists(browser, LEAD_LIST_ID)
    browser.loads[_page_url(1)] = [
        *_boot(),
        _search("salesApiLeadSearch", QUERY, 0, [], 0),
    ]

    with pytest.raises(InvalidReferenceError, match=f"{UNKNOWN_ID} is not one of"):
        await _run(mock_page, browser, lambda r: r.get_lead_list(UNKNOWN_ID))

    assert browser.navigated == [hub]


@pytest.mark.parametrize(
    ("search", "list_type"),
    [
        (lambda r: r.search_leads(lead_list=[UNKNOWN_ID]), "LEAD"),
        (lambda r: r.search_leads(exclude_lead_list=[UNKNOWN_ID]), "LEAD"),
        (lambda r: r.search_leads(account_list=[UNKNOWN_ID]), "ACCOUNT"),
    ],
    ids=["lead_list", "exclude_lead_list", "account_list"],
)
async def test_a_search_with_an_unknown_list_is_refused_before_loading(
    mock_page, search, list_type: str
):
    browser = _Browser(mock_page)
    hub = _own_lists(browser, LEAD_LIST_ID, list_type=list_type)

    with pytest.raises(InvalidReferenceError, match=f"{UNKNOWN_ID} is not one of"):
        await _run(mock_page, browser, search)

    assert browser.navigated == [hub]


async def test_the_lookup_stops_on_the_page_that_holds_the_list(mock_page):
    browser = _Browser(mock_page)
    # 60 lists; the id is the 27th, on the second hub page.
    browser.loads[SALES_LEAD_LISTS_URL] = [*_boot(), _lead_lists(0, 25, 60)]
    browser.loads[f"{SALES_LEAD_LISTS_URL}?page=2"] = [_lead_lists(25, 25, 60)]
    browser.loads[f"{SALES_LEAD_LISTS_URL}?page=3"] = [_lead_lists(50, 10, 60)]
    query = build_search_query(None, [SearchFilter(LEAD_LIST, ids=("100000526",))])
    browser.loads[search_page_url("people", query, 1)] = [
        *_boot(),
        _search("salesApiLeadSearch", query, 0, [], 0),
    ]

    await _run(mock_page, browser, lambda r: r.search_leads(lead_list=["100000526"]))

    assert browser.navigated[:2] == [
        SALES_LEAD_LISTS_URL,
        f"{SALES_LEAD_LISTS_URL}?page=2",
    ]
    assert f"{SALES_LEAD_LISTS_URL}?page=3" not in browser.navigated


async def test_a_lookup_that_could_not_read_every_list_is_not_a_refusal(mock_page):
    browser = _Browser(mock_page)
    # Page 2 of the hub never loads, so the id may be on it.
    browser.loads[SALES_LEAD_LISTS_URL] = [*_boot(), _lead_lists(0, 25, 30)]

    with pytest.raises(LinkedInScraperException, match="Could not confirm") as raised:
        await _run(mock_page, browser, lambda r: r.get_lead_list(UNKNOWN_ID))

    assert not isinstance(raised.value, InvalidReferenceError)


async def test_leads_the_search_does_not_serve_are_reported(mock_page):
    browser = _Browser(mock_page)
    _own_lists(browser, LEAD_LIST_ID, entity_count=25)
    leads = [_lead(f"ACwAAA{n:04d}", f"Person {n}") for n in range(22)]
    withheld = [
        _lead(f"ACwAAA99{n:02d}", "Withheld", blockThirdPartyDataSharing=True)
        for n in range(2)
    ]
    browser.loads[_page_url(1)] = [
        *_boot(),
        _search("salesApiLeadSearch", QUERY, 0, [*leads, *withheld], 24),
    ]

    result = await _run(mock_page, browser, lambda r: r.get_lead_list(LEAD_LIST_ID))

    assert result["lead_list"]["entityCount"] == 25
    assert result["paging"]["count"] == 22
    assert "leads" not in result["section_errors"]
    error = result["section_errors"]["lead_list"]
    assert error["error_type"] == "unavailable"
    assert "holds 25 leads" in error["error_message"]
    assert "serves 24" in error["error_message"]


async def test_a_list_the_search_serves_whole_reports_no_shortfall(mock_page):
    browser = _Browser(mock_page)
    _own_lists(browser, LEAD_LIST_ID, entity_count=2)
    leads = [_lead(f"ACwAAA{n:04d}", f"Person {n}") for n in range(2)]
    browser.loads[_page_url(1)] = [
        *_boot(),
        _search("salesApiLeadSearch", QUERY, 0, leads, 2),
    ]

    result = await _run(mock_page, browser, lambda r: r.get_lead_list(LEAD_LIST_ID))

    assert "section_errors" not in result


async def test_search_paging_count_is_the_leads_returned(mock_page):
    browser = _Browser(mock_page)
    hub = _own_lists(browser, LEAD_LIST_ID)
    browser.loads[_page_url(1)] = [
        *_boot(),
        _search("salesApiLeadSearch", QUERY, 0, [], 0),
    ]

    result = await _run(
        mock_page, browser, lambda r: r.search_leads(lead_list=[LEAD_LIST_ID])
    )

    assert browser.navigated == [hub, _page_url(1)]
    # LinkedIn's own paging.count is 25 here, the page size it was asked for.
    assert result["paging"]["count"] == 0
