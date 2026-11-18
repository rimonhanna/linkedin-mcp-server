"""Browser-DOM tests for opening the exclusion and location blocks.

The document is synthetic and follows the app's template as read from its
bundle (2026-11-18): only OR blocks carry ``data-js-targeting-module__facet-block``;
the location and exclusion blocks are plain articles, and an empty exclusion
section is only a button outside every block, whose click renders the block,
open. The whole module sits inside an outer article, as a card may. Labels are
German so a program that read text would fail. Nothing reaches LinkedIn;
skipped when chromium is not installed.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import patch

import pytest

from linkedin_mcp_server.scraping import campaign_manager_targeting as targeting

from .test_campaign_manager_targeting_dom import URL, dom_page  # noqa: F401

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

EDIT = "data-js-targeting-module__edit-facet-block-btn"
CLOSE = (
    '<button data-js-targeting-module__close-facet-block-button="true" '
    'style="display:none" onclick="this.style.display=\'none\'">Schließen</button>'
)
TOOLTIP = '<button type="button" class="button--unstyled">i</button>'


def _pills(count: int) -> str:
    return "".join(
        f'<button type="button" class="pill">Beispiel {i}</button>'
        for i in range(count)
    )


def _or_block(index: int, pills: int) -> str:
    return (
        '<article class="targeting-module__facet-block" '
        f'data-js-targeting-module__facet-block="" '
        f'data-live-test-facet-block-included-ors="{index}">'
        f'<button {EDIT}="includedOrs{index}" onclick="openBlock(this)">Bearbeiten</button>'
        f"{CLOSE}"
        '<div class="targeting-module__facet-block-pills-container">'
        f"{_pills(pills)}{TOOLTIP}</div></article>"
    )


def _locations() -> str:
    return (
        '<article class="targeting-module__facet-block">'
        f'<button {EDIT}="includedLocations" onclick="openBlock(this)">Bearbeiten</button>'
        f"{CLOSE}"
        '<div class="targeting-module__facet-block-pills-container '
        'js-pills-container__includedLocation">'
        f"{_pills(2)}</div></article>"
    )


def _exclusions(facets: list[int], *, open_: bool = False) -> str:
    # One section per excluded facet; an open block adds a per-facet "add"
    # button to each pill row, unstyled like the tooltip.
    sections = "".join(
        '<div class="targeting-module__facet-block-facet-section">'
        '<div class="targeting-module__facet-block-pills-container">'
        f"{_pills(count)}{TOOLTIP if open_ else ''}</div></div>"
        for count in facets
    )
    head = (
        CLOSE.replace('style="display:none" ', "")
        if open_
        else f'<button {EDIT}="excludedFacets" onclick="openBlock(this)">'
        "Bearbeiten</button>" + CLOSE
    )
    return (
        '<article class="targeting-module__facet-block '
        f'js-pills-container__excludedFacet">{head}{sections}</article>'
    )


_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<article class="card"><main>{blocks}</main></article>
<template id="rendered-exclusions">{rendered}</template>
<template id="rendered-locations">{rendered_locations}</template>
<script>
function openBlock(button) {{
  button.closest('article').querySelector(
    '[data-js-targeting-module__close-facet-block-button]').style.display = '';
}}
function render(button, id) {{
  const html = document.getElementById(id).innerHTML;
  button.closest('div.inline').insertAdjacentHTML('afterend', html);
  button.closest('div.inline').remove();
}}
</script></body></html>"""

_EMPTY_EXCLUSIONS = (
    '<div class="inline"><button data-js-targeting-module__inline-add-more-excluded-facets '
    "onclick=\"render(this, 'rendered-exclusions')\">Ausschließen</button></div>"
)
_EMPTY_LOCATION_EXCLUSIONS = (
    f'<div class="inline"><button {EDIT}="excludedLocations" '
    "onclick=\"render(this, 'rendered-locations')\">Ausschließen</button></div>"
)
_RENDERED_LOCATIONS = (
    '<article class="targeting-module__facet-block">'
    + CLOSE.replace('style="display:none" ', "")
    + '<div class="targeting-module__facet-block-pills-container '
    'js-pills-container__excludedLocation"></div></article>'
)


async def _load(page: Any, exclusions: str, rendered: str = "") -> None:
    blocks = (
        _locations()
        + _EMPTY_LOCATION_EXCLUSIONS
        + _or_block(0, 3)
        + _or_block(1, 1)
        + _or_block(2, 2)
        + _or_block(3, 1)
        + '<button data-js-targeting-module__add-more-new-facet-block="">+</button>'
        + exclusions
    )
    html = _PAGE.format(
        blocks=blocks, rendered=rendered, rendered_locations=_RENDERED_LOCATIONS
    )

    async def handle(route: Any) -> None:
        if route.request.url == URL:
            await route.fulfill(content_type="text/html; charset=utf-8", body=html)
        else:
            await route.abort()

    await page.route("**/*", handle)
    await page.goto(URL)


def _module(page: Any) -> targeting._TargetingPage:
    return targeting._TargetingPage(page, cast(Any, None))


async def test_an_existing_exclusion_block_opens_by_its_edit_button(dom_page):  # noqa: F811
    await _load(dom_page, _exclusions([1]))
    module = _module(dom_page)

    with patch.object(targeting, "_STEP_PAUSE", 0.0):
        index = await module.open_block("excludedFacets")

    # Locations, four OR blocks, then the exclusions: the outer card is not one.
    assert index == 5
    assert await dom_page.evaluate(targeting._BLOCK_STATE_JS, index) == {
        "open": [5],
        "pills": 1,
    }


async def test_exclusion_pills_count_across_its_facets_but_not_add_buttons(
    dom_page,  # noqa: F811
):
    await _load(dom_page, _exclusions([2, 1]))
    module = _module(dom_page)

    with patch.object(targeting, "_STEP_PAUSE", 0.0):
        index = await module.open_block("excludedFacets")
        await module.expect_pills(index, 3)


async def test_an_empty_exclusion_section_opens_by_its_own_button(dom_page):  # noqa: F811
    await _load(dom_page, _EMPTY_EXCLUSIONS, _exclusions([], open_=True))
    module = _module(dom_page)

    with patch.object(targeting, "_STEP_PAUSE", 0.0):
        index = await module.open_block("excludedFacets")

    assert await dom_page.evaluate(targeting._BLOCK_STATE_JS, index) == {
        "open": [index],
        "pills": 0,
    }
    assert await dom_page.evaluate(
        "(i) => document.querySelectorAll('article')[i + 1].className", index
    ) == ("targeting-module__facet-block js-pills-container__excludedFacet")


async def test_empty_excluded_locations_open_by_their_exclude_button(dom_page):  # noqa: F811
    await _load(dom_page, _exclusions([1]))
    module = _module(dom_page)

    with patch.object(targeting, "_STEP_PAUSE", 0.0):
        index = await module.open_block("excludedLocations")

    assert index == 1
    assert await dom_page.evaluate(targeting._BLOCK_STATE_JS, index) == {
        "open": [1],
        "pills": 0,
    }


async def test_an_exclusion_button_rendering_no_block_fails_closed(dom_page):  # noqa: F811
    await _load(dom_page, _EMPTY_EXCLUSIONS, rendered="")
    module = _module(dom_page)

    with (
        patch.object(targeting, "_STEP_PAUSE", 0.0),
        patch.object(targeting, "_DOM_TIMEOUT", 0.3),
        pytest.raises(targeting._Stop) as stop,
    ):
        await module.open_block("excludedFacets")

    assert stop.value.status == "control_unavailable"
    assert stop.value.message == "The targeting block did not open."
    assert stop.value.missing is True


async def test_an_or_edit_button_outside_its_block_is_missing(dom_page):  # noqa: F811
    await _load(
        dom_page,
        f'<div><button {EDIT}="includedOrs7">Bearbeiten</button></div>'
        + _exclusions([1]),
    )

    result = await dom_page.evaluate(targeting._OPEN_BLOCK_JS, "includedOrs7")

    assert result == {"state": "missing", "count": 1}


async def test_the_block_summary_has_counts_and_section_names_only(dom_page):  # noqa: F811
    await _load(dom_page, _EMPTY_EXCLUSIONS)

    summary = await dom_page.evaluate(targeting._BLOCKS_SUMMARY_JS)

    in_block = {"count": 1, "in_block": 1, "visible": 1, "disabled": 0}
    assert summary == {
        "blocks": 5,
        "open": [],
        "edit": {
            "includedLocations": in_block,
            "includedOrs0": in_block,
            "includedOrs1": in_block,
            "includedOrs2": in_block,
            "includedOrs3": in_block,
            "excludedLocations": {**in_block, "in_block": 0},
        },
        "close": {"count": 5, "in_block": 5, "visible": 0},
        "articles_in_blocks": 0,
        "exclude_button": {"count": 1, "in_block": 0},
        "add_group_button": 1,
        "exclusion_blocks": 0,
        "excluded_location_blocks": 0,
    }


# --- the live layout of 2026-11-18 ---------------------------------------------
# Locations, an empty location exclusion, the profile language, buyer groups
# (a section, not an article), two OR blocks and one facet exclusion of four
# values. Opening the exclusion swaps its edit button for close and, a moment
# later, renders its search inside it, with articles of its own.

_LIVE_EXCLUSION = (
    '<article class="targeting-module__facet-block js-pills-container__excludedFacet">'
    f'<button {EDIT}="excludedFacets" onclick="openExclusion(this)">Bearbeiten</button>'
    '<div class="targeting-module__facet-block-facet-section">'
    '<div class="targeting-module__facet-block-pills-container">'
    f"{_pills(4)}</div></div></article>"
)
_LIVE = (
    _locations()
    + _EMPTY_LOCATION_EXCLUSIONS
    + '<article class="targeting-module__facet-block targeting-module__language-block">'
    "<select><option>Deutsch</option></select></article>"
    + '<section class="targeting-module__facet-block js-pills-container__includedFacet-1">'
    f'<button {EDIT}="buyerGroups">Hinzufügen</button></section>'
    + _or_block(0, 8)
    + _or_block(1, 1)
    + '<button data-js-targeting-module__add-more-new-facet-block="">+</button>'
    + _LIVE_EXCLUSION
)
_LIVE_SCRIPT = """<script>
function openExclusion(button) {
  const article = button.closest('article');
  button.insertAdjacentHTML('afterend',
    '<button data-js-targeting-module__close-facet-block-button="true">Schließen</button>');
  article.querySelectorAll('.targeting-module__facet-block-pills-container')
    .forEach((row) => row.insertAdjacentHTML('beforeend',
      '<button type="button" class="button--unstyled">+</button>'));
  button.remove();
  setTimeout(() => article.insertAdjacentHTML('beforeend',
    '<div class="facet-browser"><input id="facet-browser__search-input">'
    + '<article class="card">Zielgruppen</article>'
    + '<article class="card">Merkmale</article></div>'), 50);
}
</script>"""


async def _load_live(page: Any) -> None:
    html = (
        '<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>'
        f"<main>{_LIVE}</main>{_LIVE_SCRIPT}</body></html>"
    )

    async def handle(route: Any) -> None:
        if route.request.url == URL:
            await route.fulfill(content_type="text/html; charset=utf-8", body=html)
        else:
            await route.abort()

    await page.route("**/*", handle)
    await page.goto(URL)


async def test_live_layout_the_open_exclusion_stays_its_block_once_its_search_renders(
    dom_page,  # noqa: F811
):
    await _load_live(dom_page)
    module = _module(dom_page)

    with patch.object(targeting, "_STEP_PAUSE", 0.0):
        index = await module.open_block("excludedFacets")
        await module.expect_pills(index, 4)
        await dom_page.wait_for_selector("main article article")
        # The section loop opens it again: still the one open block.
        again = await module.open_block("excludedFacets")

    # Locations, language, two OR blocks, then the exclusion.
    assert index == again == 4
    assert await dom_page.evaluate(targeting._BLOCK_STATE_JS, index) == {
        "open": [4],
        "pills": 4,
    }


async def test_live_layout_summary_names_the_open_exclusion(dom_page):  # noqa: F811
    await _load_live(dom_page)
    await dom_page.evaluate(targeting._OPEN_BLOCK_JS, "excludedFacets")
    await dom_page.wait_for_selector("main article article")

    summary = await dom_page.evaluate(targeting._BLOCKS_SUMMARY_JS)

    in_block = {"count": 1, "in_block": 1, "visible": 1, "disabled": 0}
    assert summary == {
        "blocks": 5,
        "open": [4],
        "edit": {
            "includedLocations": in_block,
            "includedOrs0": in_block,
            "includedOrs1": in_block,
            "excludedLocations": {**in_block, "in_block": 0},
            "buyerGroups": {**in_block, "in_block": 0},
        },
        "close": {"count": 4, "in_block": 4, "visible": 1},
        "articles_in_blocks": 2,
        "exclude_button": {"count": 0, "in_block": 0},
        "add_group_button": 1,
        "exclusion_blocks": 1,
        "excluded_location_blocks": 0,
    }


# The inline button inside a block: a layout the bundle does not show, so it
# is the case that proves the rendered block is chosen by its marker.
_INLINE_IN_BLOCK = (
    '<article class="targeting-module__facet-block"><div class="inline">'
    "<button data-js-targeting-module__inline-add-more-excluded-facets "
    "onclick=\"this.closest('article').insertAdjacentHTML('afterend', "
    "document.getElementById('rendered-exclusions').innerHTML); "
    'this.remove()">Ausschließen</button>'
    "</div></article>"
)


async def test_the_inline_exclusion_button_never_names_its_own_block(dom_page):  # noqa: F811
    await _load(dom_page, _INLINE_IN_BLOCK, _exclusions([], open_=True))

    result = await dom_page.evaluate(targeting._OPEN_BLOCK_JS, "excludedFacets")

    assert result == {"state": "clicked", "index": None}


async def test_an_inline_button_inside_a_block_opens_the_block_it_renders(dom_page):  # noqa: F811
    await _load(dom_page, _INLINE_IN_BLOCK, _exclusions([], open_=True))
    module = _module(dom_page)

    with patch.object(targeting, "_STEP_PAUSE", 0.0):
        index = await module.open_block("excludedFacets")

    found = await dom_page.evaluate(targeting._RENDERED_BLOCK_JS, "excludedFacets")
    assert found["found"] == [index]
    assert await dom_page.evaluate(targeting._BLOCK_STATE_JS, index) == {
        "open": [index],
        "pills": 0,
    }


async def test_the_summary_says_whether_the_inline_button_sits_in_a_block(dom_page):  # noqa: F811
    await _load(dom_page, _INLINE_IN_BLOCK)

    summary = await dom_page.evaluate(targeting._BLOCKS_SUMMARY_JS)

    assert summary["exclude_button"] == {"count": 1, "in_block": 1}
