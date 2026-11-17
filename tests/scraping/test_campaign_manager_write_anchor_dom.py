"""Browser-DOM tests for the write flow's wait for an editor anchor.

The documents are synthetic stand-ins for an app shell that renders its editor
late, or never. Nothing reaches LinkedIn; skipped when chromium is not
installed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.response_structure import ResponseLog, track
from linkedin_mcp_server.scraping import campaign_manager_write as write_module
from linkedin_mcp_server.scraping.campaign_manager_write import (
    ANCHORS,
    CampaignWriteFlow,
)

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

# The shell shows a loader; ``render`` replaces it with the budget block after
# ``delay`` ms. Every focus, click, key and input is logged.
_SHELL = """
<div role="progressbar" id="loader"></div>
<div data-js-wizard-container-footer-actions hidden><button>x</button></div>
<script>
function log(entry) {
    document.body.dataset.log = (document.body.dataset.log || '') + entry + '|';
}
for (const type of ['focusin', 'click', 'keydown', 'input', 'change']) {
    document.addEventListener(type, () => log(type), true);
}
function render(delay) {
    setTimeout(() => {
        document.getElementById('loader').remove();
        const block = document.createElement('div');
        block.setAttribute('data-live-test-bid-and-budget', '');
        block.style.marginTop = '3000px';
        block.innerHTML = '<input id="bid-and-budget__daily-budget" value="10">';
        document.body.appendChild(block);
    }, delay);
}
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
        page.set_default_timeout(3000)
        try:
            yield page
        finally:
            await browser.close()


async def _log(page: Any) -> list[str]:
    text = await page.evaluate("document.body.dataset.log || ''")
    return [entry for entry in text.split("|") if entry]


async def test_an_editor_rendered_late_is_waited_for_and_left_untouched(dom_page):
    # The page's own script renders it: evaluate runs in an isolated world.
    await dom_page.set_content(_SHELL + "<script>render(600)</script>")
    flow = CampaignWriteFlow(MagicMock(), MagicMock())

    assert await flow.wait_for_anchor(dom_page, ANCHORS["daily_budget"]) is True

    assert await _log(dom_page) == []
    assert await dom_page.evaluate("document.activeElement === document.body")
    # Rendered below the fold, so it was scrolled into view.
    assert await dom_page.evaluate("window.scrollY") > 0


async def test_an_editor_that_never_renders_records_the_anchor_structure(dom_page):
    await dom_page.set_content(_SHELL)
    log = ResponseLog("Campaign Manager")
    track(log)
    flow = CampaignWriteFlow(MagicMock(), MagicMock())

    with patch.object(write_module, "_ANCHOR_TIMEOUT", 0.5):
        found = await flow.wait_for_anchor(dom_page, ANCHORS["daily_budget"])

    assert found is False
    (page,) = log.pages
    assert page["loading"] is True
    assert set(page["anchors"]) == set(ANCHORS)
    assert page["anchors"]["daily_budget"] == {"count": 0, "visible": 0}
    assert page["anchors"]["footer"] == {"count": 1, "visible": 0}
    assert set(page) == {"path", "loading", "anchors"}
    assert await _log(dom_page) == []
