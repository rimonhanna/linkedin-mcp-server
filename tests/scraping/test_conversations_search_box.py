"""The messaging search box is found by structure, not by English text.

The documents are synthetic: each case is a claim about the selector, never
about LinkedIn's markup. Nothing reaches LinkedIn; skipped when chromium is not
installed.
"""

from __future__ import annotations

from typing import Any

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping.conversations import _SEARCH_BOX_SELECTOR

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

_TOP_BAR = '<header><input type="search" id="global" placeholder="Search"></header>'


@pytest.fixture
async def dom_page():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            page = await (await browser.new_context()).new_page()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            yield page
        finally:
            await browser.close()


async def _found_ids(page: Any, body: str) -> list[str]:
    await page.set_content(f"<html><body>{_TOP_BAR}{body}</body></html>")
    return await page.locator(_SEARCH_BOX_SELECTOR).evaluate_all(
        "els => els.map(e => e.id)"
    )


@pytest.mark.parametrize(
    "label",
    ["Nachrichten durchsuchen", "ابحث في الرسائل", "Berichten zoeken", "Rechercher"],
)
async def test_search_input_is_found_whatever_the_ui_language(dom_page, label):
    body = (
        f'<main><form role="search"><input id="box" aria-label="{label}" '
        f'placeholder="{label}"></form></main>'
    )

    assert await _found_ids(dom_page, body) == ["box"]


async def test_search_typed_input_in_main_is_found_without_a_landmark(dom_page):
    body = '<main><div><input type="search" id="box" placeholder="x"></div></main>'

    assert await _found_ids(dom_page, body) == ["box"]


async def test_global_top_bar_search_is_never_matched(dom_page):
    body = "<main><p>no messaging search here</p></main>"

    assert await _found_ids(dom_page, body) == []


async def test_english_label_is_still_an_extra_alternative(dom_page):
    body = '<main><input id="box" aria-label="Search messages"></main>'

    assert await _found_ids(dom_page, body) == ["box"]
