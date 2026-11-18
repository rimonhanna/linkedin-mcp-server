"""Browser-DOM tests for Save and exit on a new ad set's footer.

A new ad set's footer (the app's bundle, 2026-11-18) holds Previous and a
link in one container and, in another, Save and exit followed by the confirm
hovercard whose trigger is Next. The documents are synthetic; nothing reaches
LinkedIn. Skipped when chromium is not installed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping.campaign_manager_write import CampaignWriteFlow

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

_LABELS = {
    "en": ("Previous", "Save and exit", "Next"),
    "de": ("Zurück", "Speichern und beenden", "Weiter"),
}


def _footer(
    labels: tuple[str, str, str], *, previous: bool = True, save: str = ""
) -> str:
    back, save_label, forward = labels
    return (
        "<div data-js-wizard-container-footer-actions><div>"
        + (
            f"<button onclick=\"clicked('previous')\">{back}</button>"
            if previous
            else ""
        )
        + '<a href="#">account</a></div><div>'
        + f"<button {save} onclick=\"clicked('save')\">{save_label}</button>"
        + '<div><button data-live-test-confirm-hovercard__button--trigger="save" '
        + f"onclick=\"clicked('next')\">{forward}</button></div></div></div>"
    )


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


async def _click(page: Any, body: str) -> tuple[list[str], CampaignWriteFlow]:
    await page.set_content(
        "<script>function clicked(n) { document.body.dataset.clicks = "
        "(document.body.dataset.clicks || '') + n + ' '; }</script>" + body
    )
    flow = CampaignWriteFlow(MagicMock(), MagicMock())
    await flow.save_new_and_exit(page)
    clicks = (await page.evaluate("document.body.dataset.clicks || ''")).split()
    return clicks, flow


@pytest.mark.parametrize("locale", sorted(_LABELS))
@pytest.mark.parametrize("previous", [True, False])
async def test_clicks_save_and_exit_before_the_hovercard_next(
    dom_page, locale, previous
):
    clicks, _flow = await _click(dom_page, _footer(_LABELS[locale], previous=previous))

    assert clicks == ["save"]


@pytest.mark.parametrize(
    "body",
    [
        # Save and exit hidden (a reservation ad set): Previous is not in Next's
        # container, so it is never taken for it.
        _footer(_LABELS["en"], save="hidden"),
        # A draft's or launched ad set's footer.
        "<div data-js-wizard-container-footer-actions><div>"
        "<button onclick=\"clicked('save')\">Save and exit</button>"
        "<button data-live-test-wizard-container__button--next "
        "onclick=\"clicked('next')\">Next</button></div></div>",
        _footer(_LABELS["en"]) * 2,
        "<div data-js-wizard-container-footer-actions><div>"
        "<button onclick=\"clicked('save')\">Save and exit</button></div></div>",
        "<p>no footer</p>",
    ],
    ids=["save-hidden", "draft-footer", "two-footers", "no-next", "none"],
)
async def test_clicks_nothing_on_another_layout(dom_page, body):
    clicks, flow = await _click(dom_page, body)

    assert clicks == []
    assert flow.footer_disabled is False


async def test_a_disabled_save_and_exit_is_reported_not_clicked(dom_page):
    clicks, flow = await _click(dom_page, _footer(_LABELS["en"], save="disabled"))

    assert clicks == []
    assert flow.footer_disabled is True
