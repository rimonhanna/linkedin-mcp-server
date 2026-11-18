"""Paging of the Sales Navigator account-lists read (synthetic data)."""

from __future__ import annotations

from linkedin_mcp_server.scraping.identifiers import SALES_ACCOUNT_LISTS_URL

from .test_sales_navigator import (
    LIST_URL,
    _boot,
    _Browser,
    _list_metadata,
    _lists,
    _run,
    _table,
)

LISTS_PAGE_2 = f"{SALES_ACCOUNT_LISTS_URL}?page=2"


async def test_one_page_of_lists_reports_no_more(mock_page):
    browser = _Browser(mock_page)
    browser.loads[SALES_ACCOUNT_LISTS_URL] = [*_boot(), _lists(0, 3, 3)]

    result = await _run(mock_page, browser, lambda r: r.list_account_lists())

    assert browser.navigated == [SALES_ACCOUNT_LISTS_URL]
    assert len(result["account_lists"]) == 3
    assert result["paging"] == {
        "page": 1,
        "start": 0,
        "count": 3,
        "total": 3,
        "more": False,
        "next_page": None,
    }


async def test_thirty_lists_are_read_from_linkedins_two_pages(mock_page):
    browser = _Browser(mock_page)
    browser.loads[SALES_ACCOUNT_LISTS_URL] = [*_boot(), _lists(0, 25, 30)]
    browser.loads[LISTS_PAGE_2] = [_lists(25, 5, 30)]

    result = await _run(mock_page, browser, lambda r: r.list_account_lists())

    assert browser.navigated == [SALES_ACCOUNT_LISTS_URL, LISTS_PAGE_2]
    assert result["paging"] == {
        "page": 1,
        "start": 0,
        "count": 30,
        "total": 30,
        "more": False,
        "next_page": None,
    }


async def test_page_two_reads_only_linkedins_second_page(mock_page):
    browser = _Browser(mock_page)
    # The page also emits the first page's call; only start=25 may be read.
    browser.loads[LISTS_PAGE_2] = [*_boot(), _lists(0, 25, 30), _lists(25, 5, 30)]

    result = await _run(mock_page, browser, lambda r: r.list_account_lists(2))

    assert browser.navigated == [LISTS_PAGE_2]
    assert [item["id"] for item in result["account_lists"]] == [
        str(100000425 + index) for index in range(5)
    ]
    assert result["paging"]["start"] == 25
    assert result["paging"]["count"] == 5
    assert result["paging"]["more"] is False


async def test_a_page_beyond_the_end_returns_no_lists_and_no_more(mock_page):
    browser = _Browser(mock_page)
    browser.loads[LISTS_PAGE_2] = [*_boot(), _lists(25, 0, 3)]

    result = await _run(mock_page, browser, lambda r: r.list_account_lists(2))

    assert result["account_lists"] == []
    assert "section_errors" not in result
    assert result["paging"] == {
        "page": 2,
        "start": 25,
        "count": 0,
        "total": 3,
        "more": False,
        "next_page": None,
    }


async def test_a_read_cut_at_the_cap_reports_more_and_the_next_page(mock_page):
    browser = _Browser(mock_page)
    for page in range(1, 41):
        url = (
            SALES_ACCOUNT_LISTS_URL
            if page == 1
            else f"{SALES_ACCOUNT_LISTS_URL}?page={page}"
        )
        browser.loads[url] = [_lists((page - 1) * 25, 25, 1010)]

    result = await _run(mock_page, browser, lambda r: r.list_account_lists())

    assert result["paging"]["count"] == 1000
    assert result["paging"]["total"] == 1010
    assert result["paging"]["more"] is True
    assert result["paging"]["next_page"] == 41


async def test_a_page_that_does_not_load_reports_more_and_the_page_to_retry(
    mock_page,
):
    browser = _Browser(mock_page)
    browser.loads[SALES_ACCOUNT_LISTS_URL] = [*_boot(), _lists(0, 25, 30)]

    result = await _run(mock_page, browser, lambda r: r.list_account_lists())

    assert result["paging"] == {
        "page": 1,
        "start": 0,
        "count": 25,
        "total": 30,
        "more": True,
        "next_page": 2,
    }
    assert result["next_page"] == 2
    error = result["section_errors"]["account_lists"]
    assert "page 2 did not load (pass next_page=2 to retry)" in error["error_message"]


async def test_a_total_that_moves_reports_more_with_no_next_page(mock_page):
    browser = _Browser(mock_page)
    browser.loads[SALES_ACCOUNT_LISTS_URL] = [*_boot(), _lists(0, 25, 30)]
    browser.loads[LISTS_PAGE_2] = [_lists(25, 6, 31)]

    result = await _run(mock_page, browser, lambda r: r.list_account_lists())

    assert result["paging"]["more"] is True
    assert result["paging"]["next_page"] is None
    assert "next_page" not in result
    assert "section_errors" in result


async def test_two_full_pages_end_the_read_with_no_more(mock_page):
    browser = _Browser(mock_page)
    browser.loads[SALES_ACCOUNT_LISTS_URL] = [*_boot(), _lists(0, 25, 50)]
    browser.loads[LISTS_PAGE_2] = [_lists(25, 25, 50)]

    result = await _run(mock_page, browser, lambda r: r.list_account_lists())

    assert browser.navigated == [SALES_ACCOUNT_LISTS_URL, LISTS_PAGE_2]
    assert result["paging"] == {
        "page": 1,
        "start": 0,
        "count": 50,
        "total": 50,
        "more": False,
        "next_page": None,
    }


async def test_get_account_list_reports_paging(mock_page):
    browser = _Browser(mock_page)
    page_2 = f"{LIST_URL}&page=2"
    browser.loads[LIST_URL] = [
        *_boot(),
        _table(0, 25, 27, "CRM_OPPORTUNITY,NOTES"),
        _table(0, 25, 27, "UNIFIED_RECOMMENDED_LEAD"),
        _list_metadata(),
    ]
    browser.loads[page_2] = [
        _table(25, 2, 27, "UNIFIED_RECOMMENDED_LEAD"),
        _table(25, 2, 27, "CRM_OPPORTUNITY,NOTES"),
    ]

    result = await _run(mock_page, browser, lambda r: r.get_account_list(LIST_URL))

    assert result["paging"] == {
        "page": 1,
        "start": 0,
        "count": 27,
        "total": 27,
        "more": False,
        "next_page": None,
    }
