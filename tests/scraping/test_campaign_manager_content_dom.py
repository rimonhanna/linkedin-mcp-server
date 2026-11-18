"""The content library programs against synthetic markup in headless chromium.

Labels come in two locales to show that no visible text is read. The "Remove
from library" control records a click like every other button, and no test
may ever see it.
"""

from __future__ import annotations

from typing import Any

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping.campaign_manager_content import (
    click_library,
    library_rows,
    select_organic,
)

pytestmark = [pytest.mark.browser_dom, pytest.mark.xdist_group("browser_runtime")]

_LABELS = {
    "en": ("Remove from library", "Add to ad set", "Company posts"),
    "de": (
        "Aus Bibliothek entfernen",
        "Zur Anzeigengruppe hinzufügen",
        "Unternehmensbeiträge",
    ),
}
_TRIGGER = "data-live-test-confirm-hovercard__button--trigger"
# The page's own behaviour: the add button follows the selection.
_SCRIPT = """<script>
function clicked(n) {
    document.body.dataset.clicks = (document.body.dataset.clicks || '') + n + ' ';
}
document.addEventListener('change', () => {
    const add = document.getElementById('add');
    if (add) add.disabled = !document.querySelector('[id^=select-row-]:checked');
});
</script>"""


def _rows(
    count: int, *, disabled: int | None = None, checked: int | None = None
) -> str:
    cells = []
    for i in range(count):
        attrs = (" disabled" if i == disabled else "") + (
            " checked" if i == checked else ""
        )
        cells.append(
            f'<tr><td><input type="checkbox" id="select-row-{i}-checkbox"{attrs} '
            f"onclick=\"clicked('row-{i}')\"></td><td><img alt=''></td></tr>"
        )
    return "<table><tbody>" + "".join(cells) + "</tbody></table>"


def _actions(
    locale: str,
    *,
    wrapped: bool = True,
    remove_last: bool = False,
    extra_after: bool = False,
    add_enabled: bool = False,
    add_hidden: bool = False,
    triggers: int = 1,
) -> str:
    remove, add, _ = _LABELS[locale]
    button = f"<button onclick=\"clicked('remove')\">{remove}</button>"
    trigger = (
        f"<span {_TRIGGER}>{button}</span>"
        if wrapped
        else button.replace("<button ", f"<button {_TRIGGER} ", 1)
    )
    add_button = (
        f'<button id="add" aria-label="{add}"'
        + ("" if add_enabled else " disabled")
        + (' style="display:none"' if add_hidden else "")
        + f" onclick=\"clicked('add')\">{add}</button>"
    )
    extra = "<button onclick=\"clicked('extra')\">More</button>" if extra_after else ""
    parts = (
        [add_button, trigger * triggers]
        if remove_last
        else [trigger * triggers, extra, add_button]
    )
    return (
        "<div><button onclick=\"clicked('back')\">Back</button>"
        + "".join(parts)
        + "</div>"
    )


def _pills(locale: str, *, organic: str = "true", disabled: bool = False) -> str:
    label = _LABELS[locale][2]
    return (
        '<button role="checkbox" id="sponsored-content-library__pill-filter-DARK" '
        'aria-checked="false" onclick="clicked(\'dark\')">Dark</button>'
        f'<button role="checkbox" id="sponsored-content-library__pill-filter-ORGANIC" '
        f'aria-checked="{organic}"' + (" disabled" if disabled else "") + " "
        "onclick=\"this.setAttribute('aria-checked', 'true'); clicked('organic')\">"
        f"{label}</button>"
    )


@pytest.fixture
async def page():
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


async def _load(page: Any, body: str) -> None:
    await page.set_content(_SCRIPT + body)


async def _clicks(page: Any) -> list[str]:
    return (await page.evaluate("document.body.dataset.clicks || ''")).split()


def _ids(count: int) -> list[str]:
    return [f"select-row-{i}-checkbox" for i in range(count)]


async def _add(page: Any, index: int, count: int) -> list[str]:
    if await click_library(page, "row", index, _ids(count)):
        await click_library(page, "add", index, _ids(count))
    return await _clicks(page)


@pytest.mark.parametrize("locale", sorted(_LABELS))
@pytest.mark.parametrize("wrapped", [True, False], ids=["wrapper", "on-button"])
async def test_add_checks_the_row_then_clicks_add_and_never_remove(
    page, locale, wrapped
):
    await _load(page, _pills(locale) + _rows(3) + _actions(locale, wrapped=wrapped))

    state = await library_rows(page)

    assert state == {
        "rows": [
            {"id": f"select-row-{i}-checkbox", "disabled": False, "checked": False}
            for i in range(3)
        ],
        "add": {"disabled": True},
    }
    assert await _add(page, 1, 3) == ["row-1", "add"]


@pytest.mark.parametrize(
    "actions",
    [
        {"remove_last": True},
        {"extra_after": True},
        {"add_enabled": True},
        {"add_hidden": True},
        {"triggers": 2},
    ],
    ids=["remove-last", "two-after", "add-enabled-early", "add-hidden", "two-triggers"],
)
@pytest.mark.parametrize("locale", sorted(_LABELS))
async def test_no_click_on_an_unexpected_action_layout(page, locale, actions):
    await _load(page, _rows(2) + _actions(locale, **actions))

    assert await _add(page, 0, 2) == []


async def test_without_the_remove_trigger_nothing_is_the_add_button(page):
    await _load(
        page,
        _rows(2)
        + '<div><button id="add" disabled onclick="clicked(\'add\')">Add</button></div>',
    )

    assert (await library_rows(page) or {})["add"] is None
    assert await _add(page, 0, 2) == []


@pytest.mark.parametrize(
    ("rows", "count"),
    [
        (_rows(2, disabled=1), 2),
        (_rows(2, checked=0), 2),
        (_rows(3), 2),
        (_rows(1), 2),
    ],
    ids=["disabled-row", "other-row-checked", "more-rows", "fewer-rows"],
)
async def test_no_click_when_the_rows_differ_from_the_plan(page, rows, count):
    await _load(page, rows + _actions("en"))

    assert await _add(page, 1, count) == []


async def test_add_is_not_clicked_unless_exactly_the_row_is_checked(page):
    await _load(page, _rows(2) + _actions("en"))

    assert await click_library(page, "add", 1, _ids(2)) is False
    assert await click_library(page, "row", 0, _ids(2)) is True
    assert await click_library(page, "add", 1, _ids(2)) is False
    assert await _clicks(page) == ["row-0"]


@pytest.mark.parametrize("locale", sorted(_LABELS))
async def test_the_company_posts_filter_is_clicked_only_when_unselected(page, locale):
    await _load(page, _pills(locale, organic="true"))
    assert await select_organic(page) == "checked"
    assert await _clicks(page) == []

    await _load(page, _pills(locale, organic="false"))
    assert await select_organic(page) == "clicked"
    assert await _clicks(page) == ["organic"]

    await _load(page, _pills(locale, organic="false", disabled=True))
    assert await select_organic(page) is None

    await _load(page, "<p>no filters</p>")
    assert await select_organic(page) is None
