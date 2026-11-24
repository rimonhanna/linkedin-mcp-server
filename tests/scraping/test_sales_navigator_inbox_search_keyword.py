"""Inbox search accepts only the answer for the full keywords (synthetic data)."""

from __future__ import annotations

import logging

import pytest

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.scraping.identifiers import SALES_INBOX_URL

from .test_sales_navigator import _boot
from .test_sales_navigator_inbox import _Inbox, _list, _run, _thread


async def test_search_inserts_the_keywords_in_one_go(mock_page):
    browser = _Inbox(mock_page)
    browser.loads[SALES_INBOX_URL] = [*_boot(), _list([_thread(9)])]
    browser.searched["example words"] = [
        _list([_thread(1)], finder="keyword", value="example%20words"),
    ]

    await _run(mock_page, browser, lambda r: r.search_conversations("example words"))

    mock_page.keyboard.insert_text.assert_awaited_once_with("example words")


async def test_search_ignores_partial_keywords_and_reads_the_final_one(mock_page):
    browser = _Inbox(mock_page)
    browser.loads[SALES_INBOX_URL] = [*_boot(), _list([_thread(9)])]
    browser.searched["example words"] = [
        _list([_thread(7)], finder="keyword", value="exa"),
        _list([_thread(8)], finder="keyword", value="example%20wo"),
        # The app's own casing and encoding of the full keywords still match.
        _list([_thread(1), _thread(2)], finder="keyword", value="Example%20%20Words"),
    ]

    result = await _run(
        mock_page, browser, lambda r: r.search_conversations("example words")
    )

    assert [row["id"] for row in result["conversations"]] == [
        "2-THREAD0001==",
        "2-THREAD0002==",
    ]


async def test_search_answered_only_for_other_keywords_times_out(mock_page, caplog):
    browser = _Inbox(mock_page)
    browser.loads[SALES_INBOX_URL] = [*_boot(), _list([_thread(9)])]
    browser.searched["example words"] = [
        _list([_thread(8)], finder="keyword", value="example%20wo"),
    ]

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(
            LinkedInScraperException,
            match=r"did not return results for these keywords in time "
            r"\(1 search response\(s\) were for other keywords\)",
        ),
    ):
        await _run(
            mock_page, browser, lambda r: r.search_conversations("example words")
        )

    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("'example wo'" in message for message in debug)
    assert not any(
        "example wo" in r.getMessage()
        for r in caplog.records
        if r.levelno > logging.DEBUG
    )
