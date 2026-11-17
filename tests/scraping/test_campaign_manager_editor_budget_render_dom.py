"""Browser-DOM tests: the budget input re-renders on focus and commits on change.

The stand-in is synthetic and a claim about the algorithm only: like the app's
currency input, it shows a formatted amount while blurred, swaps in the raw
number one microtask after focus (a framework re-render), and commits only on
``change``. Nothing reaches LinkedIn; skipped when chromium is not installed.
"""

from __future__ import annotations

from typing import Any

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping import campaign_manager_editor as editor

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

_ID = "bid-and-budget__daily-budget"
_SCOPE = "[data-live-test-bid-and-budget]"


def _currency_input(on_input: str = "") -> str:
    return f"""
<div data-live-test-bid-and-budget>
  <input id="{_ID}" value="¤10.00">
</div>
<button id="after">after</button>
<script>
const input = document.getElementById('{_ID}');
let raw = '10';
function log(entry) {{
    document.body.dataset.log = (document.body.dataset.log || '') + entry + '|';
}}
input.addEventListener('focusin', () => queueMicrotask(() => {{ input.value = raw; }}));
input.addEventListener('focusout', () => queueMicrotask(() => {{
    input.value = '¤' + Number(raw).toFixed(2);
}}));
input.addEventListener('input', () => {{ {on_input} }});
input.addEventListener('change', () => {{ raw = input.value; log('commit:' + raw); }});
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


async def test_amount_replaces_the_value_the_focus_render_shows(dom_page):
    await dom_page.set_content(_currency_input())

    done = await editor.set_text(dom_page, _ID, _SCOPE, "11")

    assert done is True
    assert await _log(dom_page) == ["commit:11"]
    assert await dom_page.evaluate("document.activeElement.id") != _ID


async def test_input_holding_another_value_is_never_committed(dom_page):
    # The component rewrites what was typed: the input then holds a value
    # other than the planned one, and leaving it would commit that.
    await dom_page.set_content(_currency_input("input.value = input.value + '0';"))

    done = await editor.set_text(dom_page, _ID, _SCOPE, "11")

    assert done is False
    assert await _log(dom_page) == []
    assert await dom_page.evaluate("document.activeElement.id") == _ID
