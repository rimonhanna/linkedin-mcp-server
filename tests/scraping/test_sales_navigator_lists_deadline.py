"""A long Sales Navigator list read stops at a page boundary before its deadline."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from linkedin_mcp_server.scraping import sales_navigator as reader_module

from .test_sales_navigator import LIST, LIST_URL, _Browser, _run, _table_pages

SECONDS_PER_PAGE = 7.5


class _SlowBrowser(_Browser):
    """Every page load, pacing wait included, takes SECONDS_PER_PAGE."""

    def __init__(self, page: Any) -> None:
        super().__init__(page)
        self.now = 0.0

    async def navigate(self, url: str) -> None:
        self.now += SECONDS_PER_PAGE
        await super().navigate(url)


class _OneSlowPage(_SlowBrowser):
    """Page 2 takes 40 s longer than the others."""

    async def navigate(self, url: str) -> None:
        if url == f"{LIST_URL}&page=2":
            self.now += 40.0
        await super().navigate(url)


async def _read(mock_page, browser: _SlowBrowser, deadline: float, page: int = 1):
    clock = SimpleNamespace(monotonic=lambda: browser.now)
    with patch.object(reader_module, "time", clock):
        return await _run(
            mock_page,
            browser,
            lambda r: r.get_account_list(LIST, page, deadline=deadline),
        )


async def test_a_read_that_would_outrun_the_deadline_returns_what_it_read(mock_page):
    browser = _SlowBrowser(mock_page)
    browser.loads.update(_table_pages(1, 35, 870))

    result = await _read(mock_page, browser, deadline=135.0)

    # 18 pages end at 135 s; a 19th would end at 142.5 s, past the deadline.
    assert len(browser.navigated) == 18
    assert browser.now <= 135.0
    assert len(result["accounts"]) == 18 * 25
    assert result["next_page"] == 19
    assert result["paging"] == {
        "page": 1,
        "start": 0,
        "count": 450,
        "total": 870,
        "more": True,
        "next_page": 19,
    }
    error = result["section_errors"]["accounts"]
    assert error["error_type"] == "page_limit"
    assert "ran out of time after 18 pages" in error["error_message"]
    assert "next_page=19" in error["error_message"]


async def test_the_next_call_continues_where_the_deadline_stopped(mock_page):
    browser = _SlowBrowser(mock_page)
    browser.loads.update(_table_pages(19, 35, 870))

    result = await _read(mock_page, browser, deadline=135.0, page=19)

    assert browser.navigated[0] == f"{LIST_URL}&page=19"
    assert len(browser.navigated) == 17
    assert len(result["accounts"]) == 870 - 18 * 25
    assert "next_page" not in result
    assert "section_errors" not in result
    assert result["paging"]["more"] is False


async def test_the_first_page_is_read_even_past_the_deadline(mock_page):
    browser = _SlowBrowser(mock_page)
    browser.loads.update(_table_pages(1, 35, 870))

    # Queued past its deadline before it started.
    result = await _read(mock_page, browser, deadline=-1.0)

    assert browser.navigated == [LIST_URL]
    assert len(result["accounts"]) == 25
    assert result["next_page"] == 2
    assert result["paging"]["more"] is True


async def test_the_slowest_page_decides_whether_another_fits(mock_page):
    browser = _OneSlowPage(mock_page)
    browser.loads.update(_table_pages(1, 35, 870))

    # Page 2 took 47.5 s, so a page is assumed to take that long from then on:
    # page 3 fits (55 + 47.5 = 102.5), and after it (62.5 s) another would end
    # at 110 s, past a 105 s deadline. The last page's 7.5 s would read on.
    result = await _read(mock_page, browser, deadline=105.0)

    assert len(browser.navigated) == 3
    assert result["next_page"] == 4
