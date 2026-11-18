"""Campaign Manager targeting: search for values and change an ad set's targeting.

Every value is identified through the responses the app fetches itself. A
search row is the typeahead answer's element at the same index (the
checkbox ``#fb-search-result-input-<i>`` renders ``elements[i]``), and a pill
is the record's value at the same position in its block (the pills follow
``targetingCriteria`` order). Nothing here reads visible text or the app's
internals: blocks are found by their data attributes, rows and pills by
position, and every position is checked against a count from the response
first. Exclusions are one block (LinkedIn's single ``exclude.or``): an added
exclusion goes into it, opened by its own button even while it is empty,
never into a new AND group.

A change is resolved in full before anything is clicked: each added value is
found in a typeahead answer and each removed one in its block. Only then is
the write window opened, and it admits only saves whose targeting lies
between the original and the planned one, so a run that stops halfway can
have saved a subset of the change but never a value nobody asked for.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NotRequired, TypedDict

from patchright.async_api import Error as PlaywrightError

from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
    LinkedInScraperException,
)
from linkedin_mcp_server.core.humanize import human_pause, human_type
from linkedin_mcp_server.scraping.campaign_manager_api import (
    CampaignManagerCapture,
    CmResponse,
    CmWriteResponse,
)
from linkedin_mcp_server.scraping.campaign_manager_changes import (
    EDITABLE_STATUSES,
    Refusal,
    TargetingKey,
    TargetingPlan,
    admit_campaign_patch,
    pill_index,
    plan_targeting,
    targeting_keys,
)
from linkedin_mcp_server.scraping.campaign_manager_entities_payload import (
    TargetingEntity,
    render_entities,
    typeahead_entities,
)
from linkedin_mcp_server.scraping.campaign_manager_guard import (
    CmWriteRequest,
    WriteWindow,
    without_campaign_manager_writes,
)
from linkedin_mcp_server.scraping.campaign_manager_payload import (
    _UI_LABELS,
    build_targeting,
)
from linkedin_mcp_server.scraping.campaign_manager_political import (
    DECLARATION_REQUIRED,
    DECLARATION_STATUSES,
    POLITICAL_INTENT,
    UNANSWERED,
    declaration_note,
    declaration_stop,
    declared_intent,
    political_dialog,
    political_question,
    question_sentence,
    refuse_intent,
    sent_intent,
)
from linkedin_mcp_server.scraping.campaign_manager_results import (
    campaign_action_result,
    retry_safe_from,
)
from linkedin_mcp_server.scraping.campaign_manager_write import (
    ANCHORS,
    CampaignWriteFlow,
    footer_disabled_reason,
)
from linkedin_mcp_server.scraping.identifiers import campaign_manager_url
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

logger = logging.getLogger(__name__)

_TOOL = "update_campaign_targeting"
_TYPEAHEAD_RESOURCE = "campaignManagerAdTargetingEntities"
_GEO_PREFIX = "urn:li:geo:"
_TYPEAHEAD_TIMEOUT = 8.0
_DOM_TIMEOUT = 5.0
_POLL = 0.2
_SAVE_TIMEOUT = 20.0
_TYPE_BUDGET = 10.0
_STEP_PAUSE = 0.6
# A step left with less than this before the caller's deadline is not started.
_DEADLINE_MARGIN = 15.0

# A block is the innermost article holding its edit or close button: only the
# OR blocks carry ``data-js-targeting-module__facet-block``, the location and
# exclusion blocks do not (read from the app's bundle, 2026-11-18).
_BLOCK_HELPERS = """
    const EDIT = '[data-js-targeting-module__edit-facet-block-btn]';
    const CLOSE = '[data-js-targeting-module__close-facet-block-button]';
    const ARTICLE = 'article:has(' + EDIT + ', ' + CLOSE + '):not(:has(article))';
    // An empty exclusion section has no block, only a button outside every
    // block; clicking it renders the block, open, which this class marks.
    const RENDERED_ON_OPEN = {
        excludedFacets: '.js-pills-container__excludedFacet',
        excludedLocations: '.js-pills-container__excludedLocation',
    };
    const marked = (article, marker) =>
        article.matches(marker) || article.querySelector(marker) !== null;
    const PILLS = '.targeting-module__facet-block-pills-container '
        + 'button:not(.button--unstyled)';
    const visible = (el) => el.getClientRects().length > 0;
    const articles = [...document.querySelectorAll(ARTICLE)];
    const open = [];
    articles.forEach((article, i) => {
        if ([...article.querySelectorAll(CLOSE)].some(visible)) open.push(i);
    });
"""
# Which blocks are open, and how many pills block ``index`` shows.
_BLOCK_STATE_JS = (
    """(index) => {
    /* linkedin-mcp:cm-pill */"""
    + _BLOCK_HELPERS
    + """
    const article = articles[index];
    return { open, pills: article ? article.querySelectorAll(PILLS).length : -1 };
}"""
)
_OPEN_BLOCK_JS = (
    """(attr) => {
    /* linkedin-mcp:cm-pill */"""
    + _BLOCK_HELPERS
    + """
    const name = 'data-js-targeting-module__edit-facet-block-btn';
    let buttons = [...document.querySelectorAll(EDIT)]
        .filter((button) => button.getAttribute(name) === attr);
    if (buttons.length === 0 && attr === 'excludedFacets') {
        buttons = [...document.querySelectorAll(
            '[data-js-targeting-module__inline-add-more-excluded-facets]'
        )];
    }
    if (buttons.length !== 1) return { state: 'missing', count: buttons.length };
    const button = buttons[0];
    const article = button.closest(ARTICLE);
    if (!article && !RENDERED_ON_OPEN[attr]) return { state: 'missing', count: 1 };
    // null: the block is rendered by the click.
    const index = article ? articles.indexOf(article) : null;
    if (open.length) return { state: 'busy', index, open };
    if (button.disabled) return { state: 'disabled', index };
    if (!visible(button)) return { state: 'hidden', index };
    button.click();
    return { state: 'clicked', index };
}"""
)
# The block an outside button rendered: the one block its class marks.
_RENDERED_BLOCK_JS = (
    """(attr) => {
    /* linkedin-mcp:cm-pill */"""
    + _BLOCK_HELPERS
    + """
    const found = [];
    articles.forEach((article, i) => {
        if (marked(article, RENDERED_ON_OPEN[attr])) found.push(i);
    });
    return { open, found };
}"""
)
# What the module shows of its blocks, for the failure notes: section names
# and counts only, never a value.
_BLOCKS_SUMMARY_JS = (
    """() => {
    /* linkedin-mcp:cm-pill */"""
    + _BLOCK_HELPERS
    + """
    const name = 'data-js-targeting-module__edit-facet-block-btn';
    const section = /^(includedOrs\\d+|includedLocations|excludedLocations|excludedFacets|buyerGroups)$/;
    const edit = {};
    for (const button of document.querySelectorAll(EDIT)) {
        const attr = button.getAttribute(name);
        const key = section.test(attr) ? attr : 'other';
        const seen = edit[key] || { count: 0, in_block: 0, visible: 0, disabled: 0 };
        seen.count += 1;
        seen.in_block += button.closest(ARTICLE) ? 1 : 0;
        seen.visible += visible(button) ? 1 : 0;
        seen.disabled += button.disabled ? 1 : 0;
        edit[key] = seen;
    }
    const count = (selector) => document.querySelectorAll(selector).length;
    return {
        blocks: articles.length,
        open,
        edit,
        exclude_button: count('[data-js-targeting-module__inline-add-more-excluded-facets]'),
        add_group_button: count('[data-js-targeting-module__add-more-new-facet-block]'),
        exclusion_blocks: articles.filter(
            (article) => marked(article, RENDERED_ON_OPEN.excludedFacets)
        ).length,
    };
}"""
)
_CLOSE_BLOCK_JS = (
    """(index) => {
    /* linkedin-mcp:cm-pill */"""
    + _BLOCK_HELPERS
    + """
    const article = articles[index];
    if (!article) return false;
    const closers = [...article.querySelectorAll(CLOSE)].filter(visible);
    if (closers.length !== 1 || closers[0].disabled) return false;
    closers[0].click();
    return true;
}"""
)
_PILL_JS = (
    """({ index, expected, pill }) => {
    /* linkedin-mcp:cm-pill */"""
    + _BLOCK_HELPERS
    + """
    if (open.length !== 1 || open[0] !== index) return { clicked: false, reason: 'block' };
    const pills = [...articles[index].querySelectorAll(PILLS)];
    if (pills.length !== expected) {
        return { clicked: false, reason: 'count', count: pills.length };
    }
    const target = pills[pill];
    if (!target || target.disabled) return { clicked: false, reason: 'disabled' };
    target.click();
    return { clicked: true };
}"""
)
# "Narrow audience further": a new AND block after the last OR block.
_ADD_BLOCK_JS = (
    """(click) => {
    /* linkedin-mcp:cm-pill */"""
    + _BLOCK_HELPERS
    + """
    const buttons = [...document.querySelectorAll(
        'button[data-js-targeting-module__add-more-new-facet-block]'
    )];
    if (buttons.length !== 1) return { state: 'missing', count: buttons.length };
    if (open.length) return { state: 'busy', open };
    if (buttons[0].disabled) return { state: 'disabled' };
    if (!visible(buttons[0])) return { state: 'hidden' };
    if (!click) return { state: 'ready' };
    buttons[0].click();
    return { state: 'clicked' };
}"""
)
# The new block: the one active article, open, at the expected OR index.
_NEW_BLOCK_JS = (
    """(expected) => {
    /* linkedin-mcp:cm-pill */"""
    + _BLOCK_HELPERS
    + """
    const active = articles.filter(
        (article) => article.getAttribute('data-js-targeting-module__facet-block') === 'active'
    );
    if (active.length !== 1) return { ready: false, active: active.length };
    const index = articles.indexOf(active[0]);
    const ors = active[0].getAttribute('data-live-test-facet-block-included-ors');
    return {
        ready: ors === String(expected) && open.length === 1 && open[0] === index,
        index,
        ors,
        open,
        pills: active[0].querySelectorAll(PILLS).length,
    };
}"""
)
_FOCUS_SEARCH_JS = """(location) => {
    /* linkedin-mcp:cm-typeahead-rows */
    const selector = location
        ? 'input[id^="targeting-entities-typeahead__search-input-"]'
        : '#facet-browser__search-input';
    const inputs = [...document.querySelectorAll(selector)]
        .filter((el) => el.getClientRects().length > 0);
    if (inputs.length !== 1 || inputs[0].disabled) {
        return { ok: false, count: inputs.length };
    }
    inputs[0].focus();
    inputs[0].select();
    return { ok: document.activeElement === inputs[0], length: inputs[0].value.length };
}"""
_ROW_HELPERS = """
    const PREFIX = 'fb-search-result-input-';
    const inputs = [...document.querySelectorAll('input[id^="' + PREFIX + '"]')];
    const rows = document.querySelectorAll('li[id^="fb-search-result-list-item-"]');
    const ordered = inputs.every((el, i) => {
        const row = el.closest('li');
        return el.id === PREFIX + i
            && row !== null && row.id === 'fb-search-result-list-item-' + i;
    });
"""
_ROWS_JS = (
    """() => {
    /* linkedin-mcp:cm-typeahead-rows */"""
    + _ROW_HELPERS
    + """
    return {
        rows: rows.length,
        ordered,
        disabled: inputs.map((el) => el.disabled),
        checked: inputs.map((el) => el.checked),
    };
}"""
)
_SELECT_ROW_JS = (
    """({ row, expected }) => {
    /* linkedin-mcp:cm-typeahead-rows */"""
    + _ROW_HELPERS
    + """
    if (inputs.length !== expected || rows.length !== expected || !ordered) {
        return { clicked: false, reason: 'count' };
    }
    const input = inputs[row];
    if (!input) return { clicked: false, reason: 'count' };
    if (input.disabled) return { clicked: false, reason: 'disabled' };
    if (input.checked) return { clicked: false, reason: 'checked' };
    input.click();
    return { clicked: true };
}"""
)


class TargetingAdd(TypedDict):
    """A value to target: its URN and the search text that finds it."""

    urn: str
    query: str
    exclude: NotRequired[bool]


@dataclass(frozen=True)
class _Row:
    entity: TargetingEntity
    disabled: bool
    checked: bool


@dataclass
class _Add:
    urn: str
    query: str
    exclude: bool
    facet: str | None = None
    name: str | None = None
    # The block it was found in during the read phase, and its row there.
    probed: str | None = None
    row: _Row | None = None


@dataclass(frozen=True)
class _Run:
    record: Mapping[str, Any]
    plan: TargetingPlan
    adds: Mapping[str, _Add]
    url: str
    target: dict[str, Any]
    planned: dict[str, Any]
    political_ad: bool | None = None

    @property
    def campaign(self) -> str:
        return str(self.record["id"])

    @property
    def account(self) -> str:
        return str(self.record["accountId"])

    def ours(self, write: CmWriteResponse) -> bool:
        return (
            write.call.resource == "campaignManagerCampaigns"
            and write.call.entity_id == self.campaign
        )


class _Stop(Exception):
    """A step that cannot run; ``status`` is the tool's word for why.

    ``missing``: a targeting block was not found, so the failure notes get
    the module's block structure.
    """

    def __init__(self, status: str, message: str, *, missing: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.missing = missing


def _unavailable(message: str) -> _Stop:
    return _Stop("control_unavailable", message)


def _not_found(message: str) -> _Stop:
    return _Stop("control_unavailable", message, missing=True)


def _check_refused(window: WriteWindow) -> None:
    """Stop once the guard refused a save of the ad set."""
    refused = [
        item
        for item in window.refused
        if item[0].call.resource == "campaignManagerCampaigns"
    ]
    if not refused:
        return
    declaration = declaration_stop(refused)
    if declaration is not None:
        raise _Stop(*declaration)
    raise _Stop("change_blocked", _BLOCKED)


_OUT_OF_TIME = "The time for this call ran out before the next step."
_BLOCKED = (
    "Campaign Manager tried to save something other than the planned change, "
    "so that save was stopped."
)


def _or_index(section: str) -> int | None:
    if section.startswith("include_or:"):
        return int(section.split(":")[1])
    return None


def _block_attr(section: str, emptied: Collection[int] = ()) -> str:
    """The block's edit-button value; OR blocks emptied before it are gone."""
    blocks = {
        "include_locations": "includedLocations",
        "exclude_locations": "excludedLocations",
        "exclude_facets": "excludedFacets",
    }
    if section in blocks:
        return blocks[section]
    index = _or_index(section)
    if index is None:
        raise ValueError(f"{section} has no targeting block.")
    return f"includedOrs{index - sum(1 for n in emptied if n < index)}"


def _is_location_section(section: str) -> bool:
    return section.endswith("_locations")


def _is_exclude(section: str) -> bool:
    return not section.startswith("include")


def _probe_section(urn: str, exclude: bool) -> str:
    """The block to search first; the answer's ``facetUrn`` then decides."""
    if urn.startswith(_GEO_PREFIX):
        return "exclude_locations" if exclude else "include_locations"
    return "exclude_facets" if exclude else "include_or:0"


def _is_typeahead(query: str) -> Callable[[CmResponse], bool]:
    def matches(response: CmResponse) -> bool:
        return (
            response.ok
            and response.call.resource == _TYPEAHEAD_RESOURCE
            and response.call.params.get("query") == query
        )

    return matches


def _sent_keys(body: Any) -> frozenset[TargetingKey] | None:
    """The targeting a campaign save sets, or None when it sets none."""
    patch = body.get("patch") if isinstance(body, Mapping) else None
    updates = patch.get("$set") if isinstance(patch, Mapping) else None
    if not isinstance(updates, Mapping) or "targetingCriteria" not in updates:
        return None
    keys = targeting_keys(updates["targetingCriteria"])
    return frozenset(keys) if keys is not None else None


def _values(keys: Collection[TargetingKey]) -> set[tuple[bool, str, str]]:
    return {(_is_exclude(key[0]), key[1], key[2]) for key in keys}


def _value(key: TargetingKey, names: Mapping[str, str]) -> dict[str, Any]:
    value: dict[str, Any] = {
        "urn": key[2],
        "facetUrn": key[1],
        "exclude": _is_exclude(key[0]),
    }
    if key[2] in names:
        value["name"] = names[key[2]]
    return value


def _record_names(record: Mapping[str, Any]) -> dict[str, str]:
    targeting = build_targeting(record.get("targetingCriteria"))
    if targeting is None:
        return {}
    facets = [facet for group in targeting["include"] for facet in group["anyOf"]]
    facets += targeting["exclude"]
    return {
        segment["urn"]: segment["name"]
        for facet in facets
        for segment in facet["segments"]
        if segment["name"]
    }


def _target(record: Mapping[str, Any]) -> dict[str, Any]:
    target: dict[str, Any] = {
        "kind": "campaign",
        "uiLabel": _UI_LABELS["campaign"],
        "id": record["id"],
        "accountId": record["accountId"],
    }
    for key in ("name", "status"):
        if isinstance(record.get(key), str):
            target[key] = record[key]
    return target


def _out_of_time(deadline: float | None) -> bool:
    return deadline is not None and deadline - time.monotonic() < _DEADLINE_MARGIN


def _find(rows: Sequence[_Row], urn: str) -> int | None:
    """The index of the one row whose answer element is ``urn``."""
    found = [i for i, row in enumerate(rows) if row.entity["urn"] == urn]
    return found[0] if len(found) == 1 else None


def _emptied(plan: TargetingPlan) -> set[int]:
    """OR blocks whose every value the plan removes."""
    removed = set(plan.removes)
    indices = {_or_index(key[0]) for key in plan.original} - {None}
    return {
        index
        for index in indices
        if index is not None
        and all(key in removed for key in plan.original if _or_index(key[0]) == index)
    }


def _new_sections(plan: TargetingPlan) -> set[str]:
    """OR blocks the plan creates: none of their values exist yet.

    An empty exclusion section is not one: its own button opens it.
    """
    existing = {key[0] for key in plan.original}
    return {
        key[0]
        for key in plan.adds
        if key[0] not in existing and _or_index(key[0]) is not None
    }


def _count(keys: Sequence[TargetingKey], section: str) -> int:
    return sum(1 for key in keys if key[0] == section)


class _TargetingPage:
    """The targeting module of one open ad set page, driven by position."""

    def __init__(self, page: Any, capture: CampaignManagerCapture) -> None:
        self.page = page
        self.capture = capture
        self._open: tuple[str, int] | None = None

    async def _state(self, index: int) -> dict[str, Any]:
        state = await self.page.evaluate(_BLOCK_STATE_JS, index)
        if not isinstance(state, dict):
            raise _unavailable("The targeting section could not be read.")
        return state

    async def _until(
        self, index: int, done: Callable[[dict[str, Any]], bool], message: str
    ) -> None:
        stop = time.monotonic() + _DOM_TIMEOUT
        while not done(await self._state(index)):
            if time.monotonic() >= stop:
                raise _unavailable(message)
            await asyncio.sleep(_POLL)

    async def _click_edit(self, attr: str) -> dict[str, Any]:
        result = await self.page.evaluate(_OPEN_BLOCK_JS, attr)
        return result if isinstance(result, dict) else {}

    async def _close(self, indices: Sequence[int]) -> None:
        for index in indices:
            if not await self.page.evaluate(_CLOSE_BLOCK_JS, index):
                raise _unavailable("An open targeting block could not be closed.")
        await self._until(
            -1, lambda s: s["open"] == [], "An open targeting block stayed open."
        )
        self._open = None

    async def can_add_block(self) -> bool:
        """Whether the page offers exactly one "narrow further" button."""
        result = await self.page.evaluate(_ADD_BLOCK_JS, False)
        return isinstance(result, dict) and result.get("state") in ("ready", "busy")

    async def open_new_block(self, ors: int) -> int:
        """Add an AND block, which must open empty as OR block ``ors``."""
        result = await self.page.evaluate(_ADD_BLOCK_JS, True)
        if isinstance(result, dict) and result.get("state") == "busy":
            await self._close(result["open"])
            result = await self.page.evaluate(_ADD_BLOCK_JS, True)
        if not (isinstance(result, dict) and result.get("state") == "clicked"):
            logger.debug("New targeting block not added: %s", result)
            raise _unavailable(
                "The button that adds a targeting group was not found on the ad "
                "set page."
            )
        stop = time.monotonic() + _DOM_TIMEOUT
        while True:
            state = await self.page.evaluate(_NEW_BLOCK_JS, ors)
            if isinstance(state, dict) and state.get("ready") is True:
                break
            if time.monotonic() >= stop:
                logger.debug("New targeting block %s not ready: %s", ors, state)
                raise _unavailable(
                    "Adding a targeting group did not open the one new group expected."
                )
            await asyncio.sleep(_POLL)
        if state["pills"] != 0:
            raise _unavailable("The new targeting group is not empty.")
        index = state["index"]
        self._open = (f"includedOrs{ors}", index)
        await human_pause(_STEP_PAUSE)
        return index

    async def open_block(self, attr: str) -> int:
        """Open the block whose edit button carries ``attr``; its article index."""
        if self._open is not None and self._open[0] == attr:
            index = self._open[1]
            if (await self._state(index))["open"] == [index]:
                return index
        self._open = None
        result = await self._click_edit(attr)
        if result.get("state") == "busy":
            await self._close(result["open"])
            result = await self._click_edit(attr)
        if result.get("state") == "disabled":
            raise _Stop(
                "not_editable",
                "Campaign Manager does not allow this ad set's targeting to be edited.",
            )
        if result.get("state") != "clicked":
            logger.debug("Targeting block %s not opened: %s", attr, result)
            raise _not_found(
                "The targeting block to change was not found on the ad set page."
            )
        index = result["index"]
        if index is None:
            index = await self._rendered(attr)
        await self._until(
            index, lambda s: s["open"] == [index], "The targeting block did not open."
        )
        self._open = (attr, index)
        await human_pause(_STEP_PAUSE)
        return index

    async def _rendered(self, attr: str) -> int:
        """The index of the one block the click rendered, open."""
        stop = time.monotonic() + _DOM_TIMEOUT
        while True:
            state = await self.page.evaluate(_RENDERED_BLOCK_JS, attr)
            if (
                isinstance(state, dict)
                and len(state.get("found", [])) == 1
                and state.get("open") == state["found"]
            ):
                return state["found"][0]
            if time.monotonic() >= stop:
                logger.debug("Targeting block %s not rendered: %s", attr, state)
                raise _not_found(
                    "Opening the targeting block did not show it on the ad set page."
                )
            await asyncio.sleep(_POLL)

    async def pills(self, index: int) -> int:
        return (await self._state(index))["pills"]

    async def expect_pills(self, index: int, count: int) -> None:
        await self._until(
            index,
            lambda s: s["open"] == [index] and s["pills"] == count,
            "The targeting block does not show the values Campaign Manager "
            "returned for it.",
        )

    async def remove_pill(self, index: int, expected: int, pill: int) -> None:
        result = await self.page.evaluate(
            _PILL_JS, {"index": index, "expected": expected, "pill": pill}
        )
        if not (isinstance(result, dict) and result.get("clicked") is True):
            logger.debug("Pill %s of %s not clicked: %s", pill, expected, result)
            raise _unavailable(
                "The targeting block does not show the values Campaign Manager "
                "returned for it."
            )
        await self.expect_pills(index, expected - 1)
        await human_pause(_STEP_PAUSE)

    async def clear_search(self, location: bool) -> None:
        """Focus the block's search box and empty it; typing saves nothing."""
        result = await self.page.evaluate(_FOCUS_SEARCH_JS, location)
        if not (isinstance(result, dict) and result.get("ok") is True):
            raise _unavailable("The targeting search box was not found.")
        if result.get("length"):
            await self.page.keyboard.press("Backspace")

    async def search(self, query: str, *, location: bool) -> list[_Row]:
        """Type ``query`` and return the rows, each mapped to its answer element."""
        matches = _is_typeahead(query)
        after = len(self.capture.responses)
        await self.clear_search(location)
        await human_type(self.page, query, budget_seconds=_TYPE_BUDGET)
        try:
            await self.capture.wait_for(
                matches,
                after=after,
                timeout=_TYPEAHEAD_TIMEOUT,
                throttle=False,
                wanted=(_TYPEAHEAD_RESOURCE,),
            )
        except LinkedInScraperException:
            # A query typed before can be answered from the app's cache with
            # no request; its rows are then the last answer to that query.
            pass
        await self.capture.settle()
        answers = self.capture.matching(matches)
        if not answers:
            raise _unavailable(
                f"Campaign Manager did not answer the targeting search {query!r}."
            )
        entities = typeahead_entities(answers[-1].payload)
        if entities is None:
            raise _unavailable(
                "Campaign Manager answered the targeting search in a shape this "
                "tool does not recognise."
            )
        stop = time.monotonic() + _DOM_TIMEOUT
        while True:
            rows = await self.page.evaluate(_ROWS_JS)
            if (
                isinstance(rows, dict)
                and rows.get("ordered") is True
                and rows.get("rows") == len(entities)
                and len(rows.get("disabled", [])) == len(entities)
            ):
                break
            if time.monotonic() >= stop:
                logger.debug("Typeahead rows %s for %d elements", rows, len(entities))
                raise _unavailable(
                    "The search rows on the page do not match Campaign Manager's "
                    "answer."
                )
            await asyncio.sleep(_POLL)
        return [
            _Row(entity, bool(disabled), bool(checked))
            for entity, disabled, checked in zip(
                entities, rows["disabled"], rows["checked"], strict=True
            )
        ]

    async def select_row(self, row: int, expected: int) -> None:
        result = await self.page.evaluate(
            _SELECT_ROW_JS, {"row": row, "expected": expected}
        )
        if not (isinstance(result, dict) and result.get("clicked") is True):
            logger.debug("Typeahead row %s not clicked: %s", row, result)
            raise _unavailable(
                "The search row for the value is no longer the one Campaign "
                "Manager returned."
            )


class CampaignTargetingEditor:
    """Search targeting values and change an ad set's targeting through its UI."""

    def __init__(self, session: ScrapingSession, navigator: PageNavigator):
        self._session = session
        self._flow = CampaignWriteFlow(session, navigator)

    async def search_targeting_entities(
        self, account_id: str, campaign_id: str, query: str
    ) -> dict[str, Any]:
        """Targeting values Campaign Manager offers for ``query`` on this ad set.

        Read only: the first OR block is opened and ``query`` typed into its
        search, neither of which the app saves. ``addable`` is whether that
        block lets the value be selected.
        """
        page = self._session.page
        async with self._flow.capturing() as capture:
            record = await self._flow.read_campaign(capture, account_id, campaign_id)
            if isinstance(record, tuple):
                raise InvalidReferenceError(record[1])
            module = _TargetingPage(page, capture)
            await self._flow.wait_for_anchor(page, ANCHORS["targeting_first_or"])
            async with without_campaign_manager_writes(page):
                try:
                    await module.open_block(_block_attr("include_or:0"))
                    rows = await module.search(query, location=False)
                    await module.clear_search(location=False)
                except _Stop as stop:
                    await self._note_blocks(page, stop)
                    raise LinkedInScraperException(stop.message) from None
        url = campaign_manager_url(
            str(record["accountId"]), f"/campaigns/{record['id']}/details"
        )
        result: dict[str, Any] = {"url": url, "sections": {}}
        if text := render_entities(row.entity for row in rows):
            result["sections"]["targeting_entities"] = text
        result["targeting_entities"] = [
            {**row.entity, "addable": not row.disabled} for row in rows
        ]
        return result

    async def update_campaign_targeting(
        self,
        account_id: str,
        campaign_id: str,
        *,
        add: Sequence[TargetingAdd] | None = None,
        remove: Sequence[str] | None = None,
        confirm_send: bool,
        deadline: float | None = None,
        political_ad: bool | None = None,
    ) -> dict[str, Any]:
        """Add and remove targeting values of one ad set.

        Each add is the typeahead row for its ``query`` whose answer element
        has its ``urn``; each removal is the pill at the URN's position in its
        block. ``deadline`` is a ``time.monotonic()`` instant: no step starts
        within 15 s of it. ``political_ad`` is the user's own declaration
        whether the ad set's ads are political, for the question Save and
        exit asks on EU targeting; None never answers it.
        """
        adds = [
            _Add(item["urn"], item["query"], item.get("exclude", False))
            for item in add or []
        ]
        page = self._session.page
        async with self._flow.capturing() as capture:
            record = await self._flow.read_campaign(capture, account_id, campaign_id)
            if isinstance(record, tuple):
                status, message = record
                return campaign_action_result(
                    _TOOL, page.url, status, message, retry_safe=True
                )
            url = campaign_manager_url(
                str(record["accountId"]), f"/campaigns/{record['id']}/details"
            )
            target = _target(record)

            def refusal(status: str, message: str, **extra: Any) -> dict[str, Any]:
                return campaign_action_result(
                    _TOOL, url, status, message, retry_safe=True, target=target, **extra
                )

            if record.get("status") not in EDITABLE_STATUSES:
                return refusal(
                    "not_editable",
                    f"An ad set in status {record.get('status')} cannot be changed.",
                )
            original = targeting_keys(record.get("targetingCriteria"))
            if original is None:
                return refusal(
                    "not_editable", "The ad set's targeting could not be read."
                )
            module = _TargetingPage(page, capture)
            await self._flow.wait_for_anchor(page, ANCHORS["targeting_edit"])
            try:
                async with without_campaign_manager_writes(page):
                    plan = await self._resolve(
                        module,
                        record["targetingCriteria"],
                        original,
                        adds,
                        list(remove or []),
                        deadline,
                    )
            except _Stop as stop:
                await self._note_blocks(page, stop)
                return refusal(stop.status, f"{stop.message} Nothing was changed.")
            if not isinstance(plan, TargetingPlan):
                status, message = plan
                return refusal(status, message)
            names = _record_names(record)
            names.update({item.urn: item.name for item in adds if item.name})
            run = _Run(
                record=record,
                plan=plan,
                adds={item.urn: item for item in adds},
                url=url,
                target=target,
                planned={
                    "remove": [_value(key, names) for key in plan.removes],
                    "add": [_value(key, names) for key in plan.adds],
                },
                political_ad=political_ad,
            )
            if political_ad is not None:
                run.planned[POLITICAL_INTENT] = declared_intent(political_ad)
            if not confirm_send:
                # The app asks about the targeting as it will be saved.
                question = political_question(plan.target)
                return refusal(
                    "confirmation_required",
                    "Dry run: the ad set and every value were found and nothing "
                    "was changed. Call again with confirm_send true to apply the "
                    "planned change."
                    + (question_sentence(question) if political_ad is None else ""),
                    planned={**run.planned, "politicalAdQuestion": question},
                )
            return await self._apply(capture, module, run, deadline)

    async def _note_blocks(self, page: Any, stop: _Stop) -> None:
        """Record the targeting blocks a missing-block stop saw; a diagnostic."""
        if not stop.missing:
            return
        try:
            summary = await page.evaluate(_BLOCKS_SUMMARY_JS)
        except PlaywrightError:
            logger.debug("Targeting blocks not recorded", exc_info=True)
            return
        await self._flow.record_page_structure(page, targeting_blocks=summary)

    async def _resolve(
        self,
        module: _TargetingPage,
        criteria: Any,
        original: list[TargetingKey],
        adds: list[_Add],
        remove: list[str],
        deadline: float | None,
    ) -> TargetingPlan | Refusal:
        """Find every add in a typeahead answer and check every block's pills."""
        known = {key[2]: key[1] for key in original}
        for item in adds:
            if item.urn in known:
                item.facet = known[item.urn]
                continue
            if _out_of_time(deadline):
                return ("deadline", f"{_OUT_OF_TIME} Nothing was changed.")
            item.probed = _probe_section(item.urn, item.exclude)
            await module.open_block(_block_attr(item.probed))
            item.row = await self._row(module, item.probed, item)
            if item.row is None:
                return (
                    "entity_not_found",
                    f"Searching {item.query!r} did not return {item.urn}. Pass the "
                    "query search_targeting_entities found it with.",
                )
            item.facet = item.row.entity["facetUrn"]
            item.name = item.row.entity["name"]
        plan = plan_targeting(
            criteria,
            add=[(item.urn, item.facet or "", item.exclude) for item in adds],
            remove=remove,
        )
        if isinstance(plan, tuple):
            return plan
        if any(key[0] == "include_language" for key in plan.removes):
            return (
                "invalid_change",
                "Profile language is not changed through targeting values.",
            )
        emptied = _emptied(plan)
        if any(_or_index(key[0]) in emptied for key in plan.adds):
            return (
                "invalid_change",
                "This change empties a targeting group and adds to it; remove and "
                "add in two calls.",
            )
        by_urn = {item.urn: item for item in adds}
        new = _new_sections(plan)
        if new and not await module.can_add_block():
            return (
                "control_unavailable",
                "The ad set page does not offer a new targeting group, which "
                "this change needs. Nothing was changed.",
            )
        for section in dict.fromkeys(key[0] for key in plan.removes + plan.adds):
            if _out_of_time(deadline):
                return ("deadline", f"{_OUT_OF_TIME} Nothing was changed.")
            if section not in new:
                index = await module.open_block(_block_attr(section))
                await module.expect_pills(index, _count(original, section))
            for key in plan.adds:
                item = by_urn[key[2]]
                if key[0] != section:
                    continue
                row = item.row
                # A new group does not exist before the apply phase, where its
                # rows are resolved again; the first OR block's answer stands
                # in until then.
                if item.probed != section and section not in new:
                    row = await self._row(module, section, item)
                if row is None:
                    return (
                        "entity_not_found",
                        f"Searching {item.query!r} in the block {item.urn} belongs "
                        "to did not return it.",
                    )
                if row.disabled or row.checked:
                    return (
                        "entity_not_addable",
                        f"Campaign Manager does not allow {item.urn} to be added "
                        "to this ad set's targeting.",
                    )
        return plan

    @staticmethod
    async def _row(module: _TargetingPage, section: str, item: _Add) -> _Row | None:
        location = _is_location_section(section)
        rows = await module.search(item.query, location=location)
        await module.clear_search(location)
        index = _find(rows, item.urn)
        return rows[index] if index is not None else None

    async def _apply(
        self,
        capture: CampaignManagerCapture,
        module: _TargetingPage,
        run: _Run,
        deadline: float | None,
    ) -> dict[str, Any]:
        page = module.page
        plan = run.plan
        draft = run.record.get("status") == "DRAFT"
        # Only the newer editor autosaves a draft; the classic one saves a
        # draft like a launched ad set, once, by Save and exit.
        newer_editor = not await self._flow.saves_by_footer(page)

        def admit(request: CmWriteRequest) -> str | None:
            sent = sent_intent(request.body)
            allowed: dict[str, Any] = {"targetingCriteria": plan.target}
            if sent is not None:
                allowed[POLITICAL_INTENT] = sent
            refusal = admit_campaign_patch(
                request,
                campaign_id=run.campaign,
                record=run.record,
                allowed=allowed,
                path_fields=("targetingCriteria",),
            )
            if refusal is not None or sent is None:
                return refusal
            return refuse_intent(
                sent,
                run.record.get(POLITICAL_INTENT),
                run.political_ad,
                newer_editor=newer_editor,
            )

        def check(window: WriteWindow) -> None:
            if _out_of_time(deadline):
                raise _Stop("deadline", _OUT_OF_TIME)
            _check_refused(window)

        after = capture.write_count
        stop: _Stop | None = None
        saved: CmWriteResponse | None = None
        async with self._flow.apply(page, admit) as window:
            try:
                emptied: list[int] = []
                for section in dict.fromkeys(key[0] for key in plan.removes):
                    check(window)
                    index = await module.open_block(_block_attr(section, emptied))
                    expected = _count(plan.original, section)
                    await module.expect_pills(index, expected)
                    for key in plan.removes:
                        if key[0] != section:
                            continue
                        check(window)
                        pill = pill_index(plan.original, section, key[2])
                        await module.remove_pill(index, expected, pill)
                        expected -= 1
                    or_index = _or_index(section)
                    if expected == 0 and or_index is not None:
                        emptied.append(or_index)
                new = _new_sections(plan)
                created: set[str] = set()
                # Each section's adds together, so a new group is made once
                # and the groups are made in their planned order.
                sections = list(dict.fromkeys(key[0] for key in plan.adds))
                ordered = [k for s in sections for k in plan.adds if k[0] == s]
                for key in ordered:
                    check(window)
                    item = run.adds[key[2]]
                    attr = _block_attr(key[0], emptied)
                    if key[0] in new and key[0] not in created:
                        index = await module.open_new_block(
                            int(attr.removeprefix("includedOrs"))
                        )
                        created.add(key[0])
                    else:
                        index = await module.open_block(attr)
                    pills = await module.pills(index)
                    location = _is_location_section(key[0])
                    rows = await module.search(item.query, location=location)
                    row = _find(rows, item.urn)
                    if row is None or rows[row].disabled or rows[row].checked:
                        raise _unavailable(
                            f"Searching {item.query!r} no longer offers {item.urn}."
                        )
                    await module.select_row(row, len(rows))
                    await module.expect_pills(index, pills + 1)
                    await module.clear_search(location)
                    await human_pause(_STEP_PAUSE)
                check(window)
                if draft and newer_editor:
                    # The last autosave holds the whole change.
                    saved = await self._flow.wait_saved(
                        capture,
                        lambda w: run.ours(w) and _sent_keys(w.body) == plan.target,
                        after,
                    )
                else:
                    if not await self._flow.save_and_exit(page, draft=draft):
                        if self._flow.footer_disabled:
                            raise _unavailable(footer_disabled_reason(run.record))
                        raise _unavailable("The ad set's Save and exit was not found.")
                    saved = await self._footer_save(
                        capture,
                        page,
                        window,
                        lambda w: (
                            run.ours(w)
                            and (w.restli_method or "").lower() == "partial_update"
                        ),
                        after,
                        run.political_ad,
                    )
            except _Stop as exc:
                stop = exc
                await self._note_blocks(page, stop)
        answers = [write for write in capture.writes[after:] if run.ours(write)]
        if stop is not None or saved is None or window.refused:
            # Only our own unsaved edit is discarded by leaving.
            await self._flow.leave(page, run.url)
        return await self._verify(capture, run, window, answers, stop, saved)

    async def _footer_save(
        self,
        capture: CampaignManagerCapture,
        page: Any,
        window: WriteWindow,
        predicate: Callable[[CmWriteResponse], bool],
        after: int,
        political_ad: bool | None,
    ) -> CmWriteResponse | None:
        """The Save and exit's answer; None if none came.

        The political-ad question it may ask first is answered with
        ``political_ad``, the caller's declaration; without one it is closed
        with Escape and the change stops (``declaration_required``).
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _SAVE_TIMEOUT
        asks = True
        while True:
            saved = await self._flow.wait_saved(
                capture, predicate, after, timeout=_POLL
            )
            if saved is not None or loop.time() >= deadline:
                return saved
            _check_refused(window)
            if not asks:
                continue
            question = await political_dialog(page, political_ad)
            if question == "answered":
                asks = False
            elif question not in ("absent", None):
                await page.keyboard.press("Escape")
                if question == "open":
                    raise _Stop("declaration_required", DECLARATION_REQUIRED)
                raise _unavailable(UNANSWERED)

    async def _verify(
        self,
        capture: CampaignManagerCapture,
        run: _Run,
        window: WriteWindow,
        answers: Sequence[CmWriteResponse],
        stop: _Stop | None,
        saved: CmWriteResponse | None,
    ) -> dict[str, Any]:
        def result(status: str, message: str, **extra: Any) -> dict[str, Any]:
            extra.setdefault("retry_safe", retry_safe_from(window))
            return campaign_action_result(
                _TOOL,
                run.url,
                status,
                message,
                target=run.target,
                planned=run.planned,
                refused=window.refused,
                **extra,
            )

        if not window.admitted:
            if stop is not None and stop.status in DECLARATION_STATUSES:
                return result(stop.status, stop.message)
            if window.refused:
                return result("change_blocked", f"{_BLOCKED} Nothing was saved.")
            if stop is not None and stop.status in ("deadline", "not_editable"):
                return result(stop.status, f"{stop.message} Nothing was saved.")
            reason = (
                stop.message
                if stop is not None
                else ("Campaign Manager did not save the change.")
            )
            return result("control_unavailable", f"{reason} Nothing was saved.")
        if len(answers) == len(window.admitted) and not any(w.ok for w in answers):
            # Every save that left was answered with an error: nothing was kept.
            return result(
                "save_failed",
                f"Campaign Manager refused the save (HTTP {answers[-1].status}); "
                "nothing was saved.",
                retry_safe=True,
            )
        try:
            reread = await self._flow.read_campaign(capture, run.account, run.campaign)
        except LinkedInScraperException:
            reread = None
        record = reread if isinstance(reread, dict) else None
        keys = (
            targeting_keys(record.get("targetingCriteria"))
            if record is not None
            else None
        )
        note = declaration_note(run.political_ad, window.admitted, record)
        if keys is None:
            return result(
                "save_unverified",
                "Campaign Manager accepted a save but the ad set could not be read "
                "back, so the change is unconfirmed. Check it with get_campaign "
                "before trying again." + note,
            )
        before, now = _values(run.plan.original), _values(keys)
        applied: dict[str, Any] = {
            "add": [
                {"urn": urn, "facetUrn": facet, "exclude": exclude}
                for exclude, facet, urn in sorted(now - before)
            ],
            "remove": [
                {"urn": urn, "facetUrn": facet, "exclude": exclude}
                for exclude, facet, urn in sorted(before - now)
            ],
        }
        if run.political_ad is not None and record is not None:
            applied[POLITICAL_INTENT] = record.get(POLITICAL_INTENT)
        if frozenset(keys) == run.plan.target and saved is not None and saved.ok:
            return result(
                "updated", "The ad set's targeting was changed." + note, applied=applied
            )
        if stop is not None or saved is None:
            reason = (
                stop.message
                if stop is not None
                else ("Campaign Manager stopped saving the change.")
            )
            return result(
                "partially_applied",
                f"{reason} Part of the change may have been saved; applied shows "
                "what the ad set holds now." + note,
                applied=applied,
            )
        return result(
            "save_unverified",
            "Campaign Manager accepted the save but the ad set's targeting does "
            "not match the planned change; applied shows what it holds now." + note,
            applied=applied,
        )
