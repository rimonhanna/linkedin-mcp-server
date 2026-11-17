"""Browser-DOM tests: the political-ad question is answered by its anchors.

The stand-in is synthetic and a claim about the algorithm only: a dialog with
the app's heading and checkbox ids and its Cancel then Confirm footer, plus
the same checkbox inline on the page, as the app renders it on the details
step. Nothing reaches LinkedIn; skipped when chromium is not installed.
"""

from __future__ import annotations

from typing import Any

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping.campaign_manager_political import political_dialog

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]


def _page_with_question(checked: bool, *, extra_button: bool = False) -> str:
    box = "checked" if checked else ""
    extra = "<button>Other</button>" if extra_button else ""
    return f"""
<section><input type="checkbox" id="political-ad-self-declaration"></section>
<div role="dialog" aria-labelledby="political-ad-self-declaration-modal">
  <button id="close">x</button>
  <h2 id="political-ad-self-declaration-modal">Question</h2>
  <input type="checkbox" id="political-ad-self-declaration" {box}>
  <footer><button id="cancel">Cancel</button><button id="confirm">Confirm</button>{extra}</footer>
</div>
<script>
const dialog = document.querySelector('[role=dialog]');
const box = dialog.querySelector('input');
function log(entry) {{
    document.body.dataset.log = (document.body.dataset.log || '') + entry + '|';
}}
document.getElementById('cancel').addEventListener('click', () => log('cancel'));
document.getElementById('confirm').addEventListener('click', () => {{
    log('confirm:' + (box.checked ? 'NOT_POLITICAL' : 'POLITICAL'));
}});
</script>
"""


@pytest.fixture
async def dom_page():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            page = await (await browser.new_context()).new_page()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        page.set_default_timeout(1500)
        try:
            yield page
        finally:
            await browser.close()


async def _log(page: Any) -> list[str]:
    text = await page.evaluate("document.body.dataset.log || ''")
    return [entry for entry in text.split("|") if entry]


async def test_no_dialog_is_absent(dom_page):
    await dom_page.set_content(
        '<input type="checkbox" id="political-ad-self-declaration">'
    )

    assert await political_dialog(dom_page, False) == "absent"


async def test_without_a_declaration_it_is_only_reported(dom_page):
    await dom_page.set_content(_page_with_question(checked=True))

    assert await political_dialog(dom_page, None) == "open"
    assert await _log(dom_page) == []


@pytest.mark.parametrize("checked", [True, False])
async def test_not_political_checks_the_box_and_confirms(dom_page, checked):
    await dom_page.set_content(_page_with_question(checked=checked))

    assert await political_dialog(dom_page, False) == "answered"
    assert await _log(dom_page) == ["confirm:NOT_POLITICAL"]


@pytest.mark.parametrize("checked", [True, False])
async def test_political_unchecks_the_box_and_confirms(dom_page, checked):
    await dom_page.set_content(_page_with_question(checked=checked))

    assert await political_dialog(dom_page, True) == "answered"
    assert await _log(dom_page) == ["confirm:POLITICAL"]
    # The same checkbox inline on the page is never touched.
    assert (
        await dom_page.evaluate("document.querySelector('section input').checked")
        is False
    )


async def test_a_footer_it_does_not_know_is_never_clicked(dom_page):
    await dom_page.set_content(_page_with_question(checked=False, extra_button=True))

    assert await political_dialog(dom_page, False) == "layout"
    assert await _log(dom_page) == []
    assert (
        await dom_page.evaluate("document.querySelector('[role=dialog] input').checked")
        is False
    )
