"""Browser-DOM tests for the invite dialog's tick, state, press and paging programs.

The document is synthetic: each case is a claim about the programs, never
about LinkedIn's markup. Nothing reaches LinkedIn; skipped when chromium is
not installed.
"""

from __future__ import annotations

from typing import Any

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping import page_invites as pi

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

URL = "https://www.linkedin.com/company/100000701/admin/dashboard/?invite=true"


def _row(profile_id: str, *, disabled: bool = False) -> str:
    return (
        '<li role="option"><input type="checkbox" '
        f'id="i18n_checkbox-invitee-suggestion-{profile_id}"'
        f"{' disabled' if disabled else ''}>"
        f'<label for="i18n_checkbox-invitee-suggestion-{profile_id}">'
        "<span>Select Example</span></label></li>"
    )


# The layout measured live: Invite is the last button, with one text span
# that reads "Invite" and then "Invite <n>"; ticking a row adds an empty button
# just before it, so "Unselect all" is no longer next to it.
def _page(rows: str, *, extra: str = "") -> str:
    return (
        '<!DOCTYPE html><html><body><div role="dialog" id="other">'
        '<input type="checkbox" id="x_checkbox-invitee-suggestion-ACoAAone1">'
        "<button>Other</button></div>"
        '<div role="dialog" id="invite"><input role="combobox">'
        f'<ul role="listbox">{rows}</ul>{extra}'
        '<button onclick="document.body.dataset.more=1">Show more results</button>'
        '<button id="unselect">Unselect all</button>'
        '<span id="added"></span>'
        '<button id="invite-btn" onclick="document.body.dataset.pressed=1">'
        '<span class="artdeco-button__text">Invite</span></button></div>'
        "<script>document.querySelector('#invite').addEventListener('change',()=>{"
        "const any=[...document.querySelectorAll('#invite input:checked')].length;"
        "const b=document.getElementById('invite-btn');"
        "document.getElementById('added').innerHTML=any?'<button></button>':'';"
        "b.disabled=!any;b.firstChild.textContent=any?'Invite '+any:'Invite';});"
        "document.getElementById('invite-btn').disabled=true;</script>"
        "</body></html>"
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


async def _load(page: Any, html: str) -> None:
    async def handle(route: Any) -> None:
        if route.request.url == URL:
            await route.fulfill(content_type="text/html; charset=utf-8", body=html)
        else:
            await route.abort()

    await page.route("**/*", handle)
    await page.goto(URL)


async def test_a_row_is_ticked_by_its_exact_profile_id(dom_page):
    await _load(dom_page, _page(_row("ACoAAone1") + _row("xACoAAone1")))

    assert await dom_page.evaluate(pi._TICK_JS, "ACoAAone1") == "ticked"
    state = await dom_page.evaluate(pi._DIALOG_STATE_JS)
    # The other dialog's look-alike and the row whose id merely ends the same
    # way are untouched.
    assert state["checked"] == ["ACoAAone1"]
    assert state["checkedTotal"] == 1
    assert state["rows"] == ["ACoAAone1", "xACoAAone1"]


async def test_a_missing_disabled_or_doubled_row_is_never_clicked(dom_page):
    rows = _row("ACoAAtwo2", disabled=True) + _row("ACoAAdup3") + _row("ACoAAdup3")
    await _load(dom_page, _page(rows))

    assert await dom_page.evaluate(pi._TICK_JS, "ACoAAnone") == "not_found"
    assert await dom_page.evaluate(pi._TICK_JS, "ACoAAtwo2") == "not_found"
    assert await dom_page.evaluate(pi._TICK_JS, "ACoAAdup3") == "ambiguous"
    assert (await dom_page.evaluate(pi._DIALOG_STATE_JS))["checkedTotal"] == 0


async def test_a_checked_box_outside_the_rows_is_counted(dom_page):
    extra = '<input type="checkbox" id="select-all" checked>'
    await _load(dom_page, _page(_row("ACoAAone1"), extra=extra))

    state = await dom_page.evaluate(pi._DIALOG_STATE_JS)
    assert state["checked"] == []
    assert state["checkedTotal"] == 1


async def test_invite_is_pressed_only_once_enabled(dom_page):
    await _load(dom_page, _page(_row("ACoAAone1")))

    assert await dom_page.evaluate(pi._PRESS_INVITE_JS, 1) == "disabled"
    assert await dom_page.evaluate("document.body.dataset.pressed ?? null") is None
    await dom_page.evaluate(pi._TICK_JS, "ACoAAone1")
    assert await dom_page.evaluate(pi._PRESS_INVITE_JS, 1) == "clicked"
    assert await dom_page.evaluate("document.body.dataset.pressed ?? null") == "1"


async def test_paging_presses_only_the_labelled_control(dom_page):
    await _load(dom_page, _page(_row("ACoAAone1")))

    assert (
        await dom_page.evaluate(pi._SHOW_MORE_JS, list(pi.SHOW_MORE_LABELS))
        == "clicked"
    )
    assert await dom_page.evaluate("document.body.dataset.more ?? null") == "1"
    assert await dom_page.evaluate(pi._SHOW_MORE_JS, ["Mehr anzeigen"]) == "none"
    assert await dom_page.evaluate("document.body.dataset.pressed ?? null") is None


async def test_invite_is_pressed_only_when_it_shows_the_selected_count(dom_page):
    await _load(dom_page, _page(_row("ACoAAone1") + _row("ACoAAtwo2")))
    await dom_page.evaluate(pi._TICK_JS, "ACoAAone1")

    # The last button reads "Invite 1": a plan of two must not press it.
    assert await dom_page.evaluate(pi._PRESS_INVITE_JS, 2) == "count_mismatch"
    assert await dom_page.evaluate("document.body.dataset.pressed ?? null") is None
    # Nor may a last button with no count at all, whatever its position.
    await dom_page.evaluate("document.getElementById('invite-btn').textContent='Go'")
    assert await dom_page.evaluate(pi._PRESS_INVITE_JS, 1) == "count_mismatch"
    assert await dom_page.evaluate("document.body.dataset.pressed ?? null") is None


async def test_invite_needs_its_count_as_a_whole_token(dom_page):
    await _load(dom_page, _page(_row("ACoAAone1")))
    await dom_page.evaluate(pi._TICK_JS, "ACoAAone1")
    text = "document.querySelector('#invite-btn span').textContent="

    # A credits figure beside the count, or the count inside another number,
    # does not stand for it.
    for shown in ("'Invite 1 of 100'", "'Invite 11'", "'Invite 1/20'"):
        await dom_page.evaluate(text + shown)
        assert await dom_page.evaluate(pi._PRESS_INVITE_JS, 1) == "count_mismatch"
    assert await dom_page.evaluate("document.body.dataset.pressed ?? null") is None
    await dom_page.evaluate(text + "' Invite  1 '")
    assert await dom_page.evaluate(pi._PRESS_INVITE_JS, 1) == "clicked"
