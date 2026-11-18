"""Browser-DOM tests for the targeting module's block, pill and row programs.

The document is synthetic: each case is a claim about the programs, never
about LinkedIn's markup. Visible names repeat on purpose and labels come in
two languages, so a program that read text would pick the wrong control.
``data-fixture-urn`` exists only here, for the assertions: the programs never
read it. Nothing reaches LinkedIn; skipped when chromium is not installed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping import campaign_manager_targeting as targeting
from linkedin_mcp_server.scraping.campaign_manager_changes import (
    pill_index,
    targeting_keys,
)
from linkedin_mcp_server.scraping.campaign_manager_entities_payload import (
    typeahead_entities,
)

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

URL = "https://www.linkedin.com/campaignmanager/accounts/100000001/campaigns/100000301/details"
TITLES = "urn:li:adTargetingFacet:titles"
INDUSTRIES = "urn:li:adTargetingFacet:industries"
LABELS = {
    "en": {"remove": "Remove {}", "edit": "Edit", "close": "Close"},
    "de": {"remove": "{} entfernen", "edit": "Bearbeiten", "close": "Schließen"},
}
# Record order of include_or:1 (titles then industries): what the pills follow.
BLOCK_ONE = [
    (TITLES, "urn:li:title:1"),
    (TITLES, "urn:li:title:2"),
    (INDUSTRIES, "urn:li:industry:4"),
]


def _pills(urns: list[str], lang: str) -> str:
    remove = LABELS[lang]["remove"]
    pills = "".join(
        f'<button type="button" class="pill" aria-label="{remove.format("Example")}" '
        f'data-fixture-urn="{urn}" onclick="document.body.dataset.removed=\'{urn}\';'
        'this.remove()">Example</button>'
        for urn in urns
    )
    tooltip = '<button type="button" class="button--unstyled">i</button>'
    return (
        '<div class="targeting-module__facet-block-pills-container">'
        f"{pills}{tooltip}</div>"
    )


def _block(attr: str, urns: list[str], lang: str, disabled: bool = False) -> str:
    labels = LABELS[lang]
    ors = attr.removeprefix("includedOrs")
    numbered = (
        f' data-live-test-facet-block-included-ors="{ors}"' if ors.isdigit() else ""
    )
    return (
        f'<article data-js-targeting-module__facet-block{numbered} data-fixture-attr="{attr}">'
        f'<button data-js-targeting-module__edit-facet-block-btn="{attr}" '
        f'aria-label="{labels["edit"]}" {"disabled" if disabled else ""} '
        'onclick="openBlock(this)">Edit</button>'
        '<button data-js-targeting-module__close-facet-block-button style="display:none" '
        f'aria-label="{labels["close"]}" onclick="closeBlock(this)">x</button>'
        f'<template>{_pills(urns, lang)}</template><div class="summary">Example</div>'
        "</article>"
    )


def _rows(
    entities: Sequence[Mapping[str, Any]], disabled: list[bool], lang: str
) -> str:
    label = LABELS[lang]["remove"]
    return "".join(
        f'<li id="fb-search-result-list-item-{i}"><label aria-label="{label.format("x")}">'
        f'<input type="checkbox" id="fb-search-result-input-{i}" '
        f'data-fixture-urn="{entity["urn"]}" {"disabled" if off else ""} '
        'onchange="document.body.dataset.selected=this.dataset.fixtureUrn">'
        f"{entity['name']}</label></li>"
        for i, (entity, off) in enumerate(zip(entities, disabled, strict=True))
    )


_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<main>{blocks}</main>
<input id="facet-browser__search-input" type="text">
<input id="targeting-entities-typeahead__search-input-ember12" type="text" {location}>
<ul id="rows">{rows}</ul>
<script>
function openBlock(button) {{
  const article = button.closest('article');
  const template = article.querySelector('template');
  if (template) article.insertAdjacentHTML('beforeend', template.innerHTML);
  if (template) template.remove();
  article.querySelector('[data-js-targeting-module__close-facet-block-button]').style.display = '';
}}
function addBlock(button) {{
  const ors = document.querySelectorAll('[data-live-test-facet-block-included-ors]').length
    + Number(button.dataset.fixtureSkew || 0);
  const copies = Number(button.dataset.fixtureCopies || 1);
  for (let i = 0; i < copies; i++) {{
    button.insertAdjacentHTML('beforebegin',
      '<article data-js-targeting-module__facet-block="active" '
      + 'data-live-test-facet-block-included-ors="' + ors + '">'
      + '<div class="targeting-module__facet-block-pills-container">'
      + '<button type="button" class="button--unstyled">i</button></div>'
      + '<button data-js-targeting-module__close-facet-block-button="true" '
      + 'onclick="closeBlock(this)">x</button></article>');
  }}
}}
function closeBlock(button) {{
  button.style.display = 'none';
}}
</script></body></html>"""


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


async def _load(
    page: Any,
    *,
    lang: str = "en",
    rows: str = "",
    disabled_edit: bool = False,
    location_hidden: bool = True,
    add_buttons: int = 1,
    add_skew: int = 0,
    add_copies: int = 1,
) -> None:
    # The id is generated by the app, so it differs from load to load.
    add = (
        "<button data-js-targeting-module__add-more-new-facet-block "
        f'id="ember{100 + add_buttons}" data-fixture-skew="{add_skew}" '
        f'data-fixture-copies="{add_copies}" aria-label="{LABELS[lang]["edit"]}" '
        'onclick="addBlock(this)">+</button>'
    ) * add_buttons
    blocks = (
        _block("includedOrs0", ["urn:li:title:9"], lang)
        + _block("includedOrs1", [urn for _f, urn in BLOCK_ONE], lang, disabled_edit)
        + add
        + _block("excludedFacets", ["urn:li:organization:9"], lang)
    )
    html = _PAGE.format(
        blocks=blocks,
        rows=rows,
        location='style="display:none"' if location_hidden else "",
    )

    async def handle(route: Any) -> None:
        if route.request.url == URL:
            await route.fulfill(content_type="text/html; charset=utf-8", body=html)
        else:
            await route.abort()

    await page.route("**/*", handle)
    await page.goto(URL)


async def _flag(page: Any, name: str) -> Any:
    return await page.evaluate(f"document.body.dataset.{name} ?? null")


def _keys() -> list[tuple[str, str, str]]:
    criteria = {
        "include": {
            "and": [
                {
                    "or": [
                        {
                            "facet": {"urn": TITLES},
                            "segments": [{"urn": "urn:li:title:9"}],
                        }
                    ]
                },
                {
                    "or": [
                        {
                            "facet": {"urn": TITLES},
                            "segments": [
                                {"urn": "urn:li:title:1"},
                                {"urn": "urn:li:title:2"},
                            ],
                        },
                        {
                            "facet": {"urn": INDUSTRIES},
                            "segments": [{"urn": "urn:li:industry:4"}],
                        },
                    ]
                },
            ]
        },
        "exclude": {"or": []},
    }
    keys = targeting_keys(criteria)
    assert keys is not None
    return keys


# --- blocks -------------------------------------------------------------------


async def test_opening_a_block_clicks_its_own_edit_button(dom_page):
    await _load(dom_page)

    opened = await dom_page.evaluate(targeting._OPEN_BLOCK_JS, "includedOrs1")
    state = await dom_page.evaluate(targeting._BLOCK_STATE_JS, opened["index"])

    assert opened == {"state": "clicked", "index": 1}
    assert state == {"open": [1], "pills": 3}


async def test_the_tooltip_button_in_the_pill_row_is_not_a_pill(dom_page):
    await _load(dom_page)
    await dom_page.evaluate(targeting._OPEN_BLOCK_JS, "includedOrs0")

    state = await dom_page.evaluate(targeting._BLOCK_STATE_JS, 0)

    assert state["pills"] == 1


async def test_a_second_block_is_not_opened_while_one_is_open(dom_page):
    await _load(dom_page)
    await dom_page.evaluate(targeting._OPEN_BLOCK_JS, "includedOrs0")

    busy = await dom_page.evaluate(targeting._OPEN_BLOCK_JS, "includedOrs1")
    assert busy == {"state": "busy", "index": 1, "open": [0]}

    assert await dom_page.evaluate(targeting._CLOSE_BLOCK_JS, 0) is True
    assert (await dom_page.evaluate(targeting._BLOCK_STATE_JS, -1))["open"] == []


async def test_a_disabled_edit_button_is_reported_and_not_clicked(dom_page):
    await _load(dom_page, disabled_edit=True)

    result = await dom_page.evaluate(targeting._OPEN_BLOCK_JS, "includedOrs1")

    assert result == {"state": "disabled", "index": 1}
    assert (await dom_page.evaluate(targeting._BLOCK_STATE_JS, 1))["open"] == []


async def test_an_unknown_block_is_missing(dom_page):
    await _load(dom_page)

    result = await dom_page.evaluate(targeting._OPEN_BLOCK_JS, "includedOrs7")

    assert result == {"state": "missing", "count": 0}


# --- pills --------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["en", "de"])
@pytest.mark.parametrize("urn", [urn for _f, urn in BLOCK_ONE])
async def test_the_pill_at_the_urns_record_position_is_clicked(dom_page, lang, urn):
    await _load(dom_page, lang=lang)
    await dom_page.evaluate(targeting._OPEN_BLOCK_JS, "includedOrs1")
    pill = pill_index(_keys(), "include_or:1", urn)

    result = await dom_page.evaluate(
        targeting._PILL_JS, {"index": 1, "expected": 3, "pill": pill}
    )

    assert result == {"clicked": True}
    assert await _flag(dom_page, "removed") == urn


async def test_a_pill_count_other_than_the_records_clicks_nothing(dom_page):
    await _load(dom_page)
    await dom_page.evaluate(targeting._OPEN_BLOCK_JS, "includedOrs1")

    result = await dom_page.evaluate(
        targeting._PILL_JS, {"index": 1, "expected": 4, "pill": 0}
    )

    assert result == {"clicked": False, "reason": "count", "count": 3}
    assert await _flag(dom_page, "removed") is None


async def test_a_pill_of_a_block_that_is_not_open_is_never_clicked(dom_page):
    await _load(dom_page)
    await dom_page.evaluate(targeting._OPEN_BLOCK_JS, "includedOrs0")

    result = await dom_page.evaluate(
        targeting._PILL_JS, {"index": 1, "expected": 3, "pill": 0}
    )

    assert result == {"clicked": False, "reason": "block"}
    assert await _flag(dom_page, "removed") is None


# --- typeahead rows ---------------------------------------------------------------

_ANSWER = {
    "elements": [
        {
            "urn": "urn:li:title:5",
            "name": "Engineer",
            "facetUrn": TITLES,
            "targetable": True,
        },
        {
            "urn": "urn:li:title:6",
            "name": "Engineer",
            "facetUrn": TITLES,
            "targetable": True,
        },
        {
            "urn": "urn:li:title:7",
            "name": "Engineer",
            "facetUrn": TITLES,
            "targetable": True,
        },
    ]
}


@pytest.mark.parametrize("lang", ["en", "de"])
async def test_the_row_at_the_answers_index_is_selected_despite_equal_names(
    dom_page, lang
):
    entities = typeahead_entities(_ANSWER)
    assert entities is not None
    await _load(dom_page, lang=lang, rows=_rows(entities, [False] * 3, lang))
    rows = [targeting._Row(entity, False, False) for entity in entities]
    row = targeting._find(rows, "urn:li:title:7")

    result = await dom_page.evaluate(
        targeting._SELECT_ROW_JS, {"row": row, "expected": 3}
    )

    assert row == 2
    assert result == {"clicked": True}
    assert await _flag(dom_page, "selected") == "urn:li:title:7"


async def test_rows_report_which_are_disabled_in_order(dom_page):
    entities = typeahead_entities(_ANSWER) or []
    await _load(dom_page, rows=_rows(entities, [False, True, False], "en"))

    rows = await dom_page.evaluate(targeting._ROWS_JS)

    assert rows == {
        "rows": 3,
        "ordered": True,
        "disabled": [False, True, False],
        "checked": [False, False, False],
    }


async def test_a_disabled_row_is_never_clicked(dom_page):
    entities = typeahead_entities(_ANSWER) or []
    await _load(dom_page, rows=_rows(entities, [False, True, False], "en"))

    result = await dom_page.evaluate(
        targeting._SELECT_ROW_JS, {"row": 1, "expected": 3}
    )

    assert result == {"clicked": False, "reason": "disabled"}
    assert await _flag(dom_page, "selected") is None


async def test_rows_that_do_not_match_the_answer_count_are_not_clicked(dom_page):
    entities = typeahead_entities(_ANSWER) or []
    await _load(dom_page, rows=_rows(entities[:2], [False, False], "en"))

    result = await dom_page.evaluate(
        targeting._SELECT_ROW_JS, {"row": 1, "expected": 3}
    )

    assert result == {"clicked": False, "reason": "count"}
    assert await _flag(dom_page, "selected") is None


# --- search box ---------------------------------------------------------------


async def test_the_facet_search_box_is_focused_and_selected(dom_page):
    await _load(dom_page)
    await dom_page.fill("#facet-browser__search-input", "old")

    result = await dom_page.evaluate(targeting._FOCUS_SEARCH_JS, False)

    assert result == {"ok": True, "length": 3}


async def test_the_location_search_box_is_used_for_location_blocks(dom_page):
    await _load(dom_page, location_hidden=False)

    result = await dom_page.evaluate(targeting._FOCUS_SEARCH_JS, True)
    focused = await dom_page.evaluate("document.activeElement.id")

    assert result == {"ok": True, "length": 0}
    assert focused == "targeting-entities-typeahead__search-input-ember12"


async def test_a_hidden_location_search_box_is_not_used(dom_page):
    await _load(dom_page)

    result = await dom_page.evaluate(targeting._FOCUS_SEARCH_JS, True)

    assert result == {"ok": False, "count": 0}


# --- the module over a real page ---------------------------------------------------


class _Capture:
    """Answers the typed query with a scripted typeahead response."""

    def __init__(self, payload: dict, query: str) -> None:
        call = SimpleNamespace(
            resource="campaignManagerAdTargetingEntities",
            params={"q": "queryAndMultiFacetTypeahead", "query": query},
        )
        self.answer = SimpleNamespace(ok=True, call=call, payload=payload)
        self.responses: list[Any] = []

    async def wait_for(self, predicate, **_kwargs):
        self.responses.append(self.answer)
        return self.answer

    async def settle(self) -> None:
        return None

    def matching(self, predicate, after: int = 0):
        return [item for item in self.responses[after:] if predicate(item)]


async def test_the_module_removes_by_position_and_adds_by_answer_index(dom_page):
    entities = typeahead_entities(_ANSWER) or []
    await _load(dom_page, lang="de", rows=_rows(entities, [False] * 3, "de"))
    module = targeting._TargetingPage(dom_page, cast(Any, _Capture(_ANSWER, "eng")))

    with (
        patch.object(targeting, "_STEP_PAUSE", 0.0),
        patch.object(targeting, "human_type") as typed,
    ):
        index = await module.open_block("includedOrs1")
        await module.expect_pills(index, 3)
        await module.remove_pill(
            index, 3, pill_index(_keys(), "include_or:1", "urn:li:title:2")
        )
        rows = await module.search("eng", location=False)
        row = targeting._find(rows, "urn:li:title:6")
        assert row == 1
        await module.select_row(row, len(rows))

    typed.assert_awaited_once()
    assert await _flag(dom_page, "removed") == "urn:li:title:2"
    assert await _flag(dom_page, "selected") == "urn:li:title:6"
    assert await module.pills(index) == 2


async def test_the_module_refuses_rows_that_never_match_the_answer(dom_page):
    entities = typeahead_entities(_ANSWER) or []
    await _load(dom_page, rows=_rows(entities[:1], [False], "en"))
    module = targeting._TargetingPage(dom_page, cast(Any, _Capture(_ANSWER, "eng")))

    with (
        patch.object(targeting, "_DOM_TIMEOUT", 0.3),
        patch.object(targeting, "human_type"),
        pytest.raises(targeting._Stop) as stop,
    ):
        await module.search("eng", location=False)

    assert stop.value.status == "control_unavailable"
    assert await _flag(dom_page, "selected") is None


# --- a new AND group ("narrow audience further") ----------------------------------


@pytest.mark.parametrize("lang", ["en", "de"])
async def test_a_new_group_opens_empty_at_the_next_or_index(dom_page, lang):
    await _load(dom_page, lang=lang)
    module = targeting._TargetingPage(dom_page, cast(Any, None))

    with patch.object(targeting, "_STEP_PAUSE", 0.0):
        index = await module.open_new_block(2)

    state = await dom_page.evaluate(targeting._BLOCK_STATE_JS, index)
    ors = await dom_page.evaluate(
        "(i) => document.querySelectorAll('article')[i]"
        ".getAttribute('data-live-test-facet-block-included-ors')",
        index,
    )
    assert state == {"open": [index], "pills": 0}
    assert ors == "2"


async def test_checking_for_the_new_group_button_does_not_click_it(dom_page):
    await _load(dom_page)

    result = await dom_page.evaluate(targeting._ADD_BLOCK_JS, False)

    assert result == {"state": "ready"}
    assert (await dom_page.evaluate(targeting._BLOCK_STATE_JS, -1))["open"] == []


async def test_an_open_block_is_closed_before_a_new_group_is_added(dom_page):
    await _load(dom_page)
    module = targeting._TargetingPage(dom_page, cast(Any, None))

    with patch.object(targeting, "_STEP_PAUSE", 0.0):
        await module.open_block("includedOrs0")
        index = await module.open_new_block(2)

    assert (await dom_page.evaluate(targeting._BLOCK_STATE_JS, index))["open"] == [
        index
    ]


@pytest.mark.parametrize(
    "options",
    [
        {"add_buttons": 0},
        {"add_buttons": 2},
        {"add_skew": 1},
        {"add_copies": 2},
    ],
    ids=["no-button", "two-buttons", "wrong-index", "two-new-groups"],
)
async def test_a_new_group_that_is_not_the_one_expected_fails_closed(dom_page, options):
    await _load(dom_page, **options)
    module = targeting._TargetingPage(dom_page, cast(Any, None))

    with (
        patch.object(targeting, "_STEP_PAUSE", 0.0),
        patch.object(targeting, "_DOM_TIMEOUT", 0.3),
        pytest.raises(targeting._Stop) as stop,
    ):
        await module.open_new_block(2)

    assert stop.value.status == "control_unavailable"
