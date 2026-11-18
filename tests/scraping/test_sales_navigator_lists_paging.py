"""Paging of the Sales Navigator account-lists read (synthetic data)."""

from __future__ import annotations

from linkedin_mcp_server.scraping.identifiers import SALES_ACCOUNT_LISTS_URL

from .test_sales_navigator import _boot, _Browser, _lists, _run

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
