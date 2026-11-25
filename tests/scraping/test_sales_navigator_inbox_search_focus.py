"""Inbox search types only once the box keeps the focus (synthetic data)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.scraping.identifiers import SALES_INBOX_URL

from .test_sales_navigator import _boot
from .test_sales_navigator_inbox import _Inbox, _list, _run, _thread, _thread_response


class _OpensLater(_Inbox):
    """The app opens the top conversation a moment after the list."""

    def __init__(self, page: Any, order: list[str]) -> None:
        super().__init__(page)
        self.order = order

    async def navigate(self, url: str) -> None:
        await super().navigate(url)

        async def open_later() -> None:
            await asyncio.sleep(0.02)
            self.order.append("opened")
            await self.emit(_thread_response(_thread(9), 9))

        asyncio.get_running_loop().create_task(open_later())


async def test_search_types_after_the_auto_opened_conversation(mock_page):
    order: list[str] = []
    browser = _OpensLater(mock_page, order)
    browser.loads[SALES_INBOX_URL] = [*_boot(), _list([_thread(9)])]
    browser.searched["example"] = [
        _list([_thread(1)], finder="keyword", value="example"),
    ]
    typed = browser.type

    async def type_after(text: str, *, delay: int | None = None) -> None:
        order.append("typed")
        await typed(text, delay=delay)

    mock_page.keyboard.type.side_effect = type_after

    await _run(mock_page, browser, lambda r: r.search_conversations("example"))

    assert order == ["opened", "typed"], (
        "keys typed before the app opened its conversation land there, not in "
        "the search box"
    )


async def test_search_box_without_the_keywords_searches_nothing(mock_page):
    browser = _Inbox(mock_page)
    browser.loads[SALES_INBOX_URL] = [*_boot(), _list([_thread(9)])]
    browser.searched["example"] = [
        _list([_thread(1)], finder="keyword", value="example"),
    ]
    mock_page.locator.return_value.input_value.side_effect = lambda **_: "exam"

    with pytest.raises(
        LinkedInScraperException,
        match="search box did not take the keywords as typed",
    ):
        await _run(mock_page, browser, lambda r: r.search_conversations("example"))
