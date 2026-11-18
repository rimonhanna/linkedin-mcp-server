"""Add an existing company post to a Campaign Manager ad set as an ad.

The post is found in the ad set's content library by URN in the app's own
``campaignManagerContentLibraryEntities?q=organicCompanyContent`` answer, and
eligibility is that answer's verdict. Its table row is tied to it by the row
checkbox id: the table is keyed by ``urn`` (bundle ``@rowIdKey``), so a row id
naming the post is exact. The table shows a filtered, paged copy of the answer
(posts already in the ad set dropped, 5 per page), so a row position is taken
as the answer's position only for index-keyed ids on one page holding exactly
the answer's posts. The add button is the footer's sponsor button,
found by its class, and is never the "Remove from library" control or around it.
A click that hit anything else still writes nothing, because the write window
admits only the app's creative create for this ad set and this post.

An ACTIVE ad set is refused: its new ad would start spending at once. Per the
AGENTS.md Scraping Rules nothing here reads visible text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlparse

import asyncio
import logging
import re

from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
    LinkedInScraperException,
)
from linkedin_mcp_server.scraping.campaign_manager import (
    _LINKEDIN,
    _ROUTE_SCRIPT,
    _error,
)
from linkedin_mcp_server.scraping.campaign_manager_api import (
    CampaignManagerCapture,
    CmResponse,
    CmWriteResponse,
)
from linkedin_mcp_server.scraping.campaign_manager_changes import (
    Refusal,
    WriteRequest,
    admit_creative_create,
    is_create_method,
)
from linkedin_mcp_server.scraping.campaign_manager_entities_payload import (
    SponsorableContent,
    content_library_entries,
    render_sponsorable_posts,
)
from linkedin_mcp_server.scraping.campaign_manager_guard import (
    WriteWindow,
    without_campaign_manager_writes,
)
from linkedin_mcp_server.scraping.campaign_manager_payload import (
    build_campaign,
    build_creative,
    envelope_elements,
    envelope_total,
    restli_list,
)
from linkedin_mcp_server.scraping.campaign_manager_results import (
    campaign_action_result,
    retry_safe_from,
)
from linkedin_mcp_server.scraping.campaign_manager_write import (
    ANCHORS,
    CampaignWriteFlow,
)
from linkedin_mcp_server.scraping.restli import as_int, envelope_paging
from linkedin_mcp_server.scraping.identifiers import (
    campaign_manager_url,
    normalize_ad_account_id,
    normalize_campaign_id,
    normalize_post_urn,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

logger = logging.getLogger(__name__)

TOOL = "add_post_to_campaign"
_LIBRARY_SUFFIX = "/creatives/sponsorable-contents?businessId=personal"
_ORGANIC_PILL = "#sponsored-content-library__pill-filter-ORGANIC"
_ADDABLE_STATUSES = frozenset({"DRAFT", "PAUSED"})
_READY_TIMEOUT = 12.0
_SAVE_TIMEOUT = 20.0
_CREATIVES_WAIT = 8.0
# The table renders after its data arrives; poll the row count this long.
_ROWS_POLL_SECONDS = 0.25
_ROWS_POLLS = 20
_TARGET_KEYS = ("kind", "uiLabel", "id", "accountId", "name", "status")
# The library table's page size (bundle ``@pageSize``).
_TABLE_PAGE_SIZE = 5
_URN_ROW = re.compile(r"select-row-urn:li:[A-Za-z]+:[0-9]+-checkbox")
_INDEX_ROW = re.compile(r"select-row-[0-9]+-checkbox")
_CREATIVE_URN = "urn:li:sponsoredCreative:"

# Shared by both programs: the rows in id order, and the add button. The add
# button is the library footer's sponsor button (bundle class
# ``sponsored-content-library__sponsor-button``, a sibling of the "Remove from
# library" wrapper): exactly one, visible, and neither the remove trigger, inside
# it, nor around it; any other layout finds nothing.
_LIBRARY_HELPERS = """
    const rowBoxes = () => [...document.querySelectorAll(
        'input[type=checkbox][id^="select-row-"][id$="-checkbox"]'
    )];
    const addButton = () => {
        const remove = '[data-live-test-confirm-hovercard__button--trigger]';
        const found = [...document.querySelectorAll(
            '.sponsored-content-library__sponsor-button'
        )].map((el) => {
            if (el.matches('button')) return el;
            const inner = el.querySelectorAll('button');
            return inner.length === 1 ? inner[0] : null;
        });
        if (found.length !== 1 || found[0] === null) return null;
        const add = found[0];
        if (add.getClientRects().length === 0 || add.closest(remove) ||
                add.querySelector(remove) !== null) {
            return null;
        }
        return add;
    };
    const snapshot = () => {
        const add = addButton();
        return {
            rows: rowBoxes().map((b) => ({
                id: b.id, disabled: b.disabled, checked: b.checked,
            })),
            add: add === null ? null : { disabled: add.disabled },
        };
    };
"""

_ROWS_JS = (
    """() => {
    /* linkedin-mcp:cm-content-rows */"""
    + _LIBRARY_HELPERS
    + """
    return snapshot();
}"""
)

# One program per click, each re-checking the whole layout first (the same
# row ids in the same order): ``row`` checks box ``index`` while no box is
# checked and the add button is disabled;
# ``add`` clicks the add button only while exactly that box is checked.
_CLICK_JS = (
    """([step, index, ids]) => {
    /* linkedin-mcp:cm-content-click */"""
    + _LIBRARY_HELPERS
    + """
    const boxes = rowBoxes();
    const add = addButton();
    const same = boxes.length === ids.length &&
        boxes.every((b, i) => b.id === ids[i]);
    const box = boxes[index];
    const others = boxes.filter((b, i) => i !== index && b.checked);
    if (!same || !box || box.disabled || add === null || others.length !== 0) {
        return { clicked: false, state: snapshot() };
    }
    if (step === 'row') {
        if (box.checked || !add.disabled) return { clicked: false, state: snapshot() };
        box.click();
        return { clicked: box.checked, state: snapshot() };
    }
    if (!box.checked || add.disabled) return { clicked: false, state: snapshot() };
    add.click();
    return { clicked: true, state: snapshot() };
}"""
)

_PILL_JS = """(selector) => {
    /* linkedin-mcp:cm-content-pill */
    const pills = [...document.querySelectorAll(selector)];
    if (pills.length !== 1) return { found: false, checked: false, clicked: false };
    const pill = pills[0];
    if (pill.getAttribute('aria-checked') === 'true') {
        return { found: true, checked: true, clicked: false };
    }
    if (pill.disabled || pill.getAttribute('aria-disabled') === 'true') {
        return { found: true, checked: false, clicked: false };
    }
    pill.click();
    return { found: true, checked: false, clicked: true };
}"""


async def library_rows(page: Any) -> dict[str, Any] | None:
    """The library rows and the add button's state, as the page shows them."""
    result = await page.evaluate(_ROWS_JS)
    return result if isinstance(result, dict) else None


async def click_library(page: Any, step: str, index: int, ids: Sequence[str]) -> bool:
    """Check row ``index`` (``step="row"``) or click the add button (``"add"``),
    only while the table still shows rows ``ids``."""
    result = await page.evaluate(_CLICK_JS, [step, index, list(ids)])
    clicked = isinstance(result, dict) and result.get("clicked") is True
    if not clicked:
        logger.debug("Content library %s not clicked: %s", step, result)
    return clicked


async def select_organic(page: Any) -> str | None:
    """Select the company-posts filter: ``"checked"`` if it already was,
    ``"clicked"`` if it was clicked, None if it is absent or disabled."""
    result = await page.evaluate(_PILL_JS, _ORGANIC_PILL)
    if not isinstance(result, dict):
        return None
    if result.get("checked") is True:
        return "checked"
    return "clicked" if result.get("clicked") is True else None


def _rendered(state: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    rows: Any = state.get("rows") if state is not None else None
    if not isinstance(rows, list):
        return None
    found = [cast("dict[str, Any]", row) for row in rows if isinstance(row, dict)]
    if len(found) != len(rows) or not all(isinstance(r.get("id"), str) for r in found):
        return None
    return found


def _row_id(key: str | int) -> str:
    return f"select-row-{key}-checkbox"


def locate_row(
    rows: Sequence[Mapping[str, Any]], post: str, index: int, count: int
) -> int | None:
    """The rendered row of ``post``, or None when no row is provably its own.

    A row id keyed by the post's URN is exact. Index-keyed ids name the post's
    answer position only when the table shows the whole answer on one page.
    Index-keyed ids cannot tell a table still redrawing from a settled one; a
    row redrawn between this check and the click is still bounded by the write
    window, which admits only this ad set's creative create for this post.
    """
    ids = [row.get("id") for row in rows]
    own = _row_id(post)
    if own in ids:
        return ids.index(own) if ids.count(own) == 1 else None
    if (
        len(ids) == count
        and count <= _TABLE_PAGE_SIZE
        and ids == [_row_id(i) for i in range(count)]
    ):
        return index
    return None


def _row_kinds(rows: Sequence[Mapping[str, Any]], post: str) -> list[str]:
    """How each row id is keyed, value-free, for the failure notes."""
    kinds = []
    for row in rows:
        row_id = str(row.get("id"))
        if row_id == _row_id(post):
            kinds.append("this post")
        elif _URN_ROW.fullmatch(row_id):
            kinds.append("urn")
        elif _INDEX_ROW.fullmatch(row_id):
            kinds.append("index")
        else:
            kinds.append("other")
    return kinds


_LIBRARY = ("campaignManagerContentLibraryEntities",)


def _is_library(response: CmResponse) -> bool:
    return (
        response.ok
        and response.call.resource == _LIBRARY[0]
        and response.call.finder == "organicCompanyContent"
        and response.call.params.get("start", "0") == "0"
    )


@dataclass(frozen=True, slots=True)
class _Row:
    # The post's position in the library answer.
    index: int
    entry: SponsorableContent
    # Posts in the library answer.
    count: int
    # Where this tool's library answers start in the capture.
    after: int
    # The post's rendered row, and the row ids re-checked before every click.
    position: int
    ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Library:
    # The answer the table renders, its posts, and where this load's answers
    # start in the capture.
    response: CmResponse
    entries: list[SponsorableContent]
    after: int


def _truncation(response: CmResponse, listed: int) -> str | None:
    """Why ``listed`` posts may not be the whole library; None when provably whole.

    The page size is the request's ``count``, else the answer's
    ``paging.count``. Without a total or a page size nothing proves the page
    complete.
    """
    total = envelope_total(response.payload)
    if total is not None:
        if total <= listed:
            return None
        return (
            f"Campaign Manager reported {total} company posts and returned "
            f"{listed} on its first page; the rest were not read."
        )
    requested = response.call.params.get("count", "")
    paging = envelope_paging(response.payload) or {}
    page_size = int(requested) if requested.isdigit() else as_int(paging.get("count"))
    if page_size is not None and listed < page_size:
        return None
    if page_size is not None:
        return (
            f"Campaign Manager returned a full first page of {listed} company "
            "posts; more may exist and were not read."
        )
    return (
        f"Campaign Manager returned {listed} company posts without saying how "
        "many there are or its page size; more may exist and were not read."
    )


def _creative_id(value: str | None) -> str | None:
    if value is None:
        return None
    tail = value[len(_CREATIVE_URN) :] if value.startswith(_CREATIVE_URN) else value
    return tail if tail.isdigit() else None


class CampaignContentEditor:
    """Own ``add_post_to_campaign``: a company post becomes an ad of an ad set."""

    def __init__(self, session: ScrapingSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator
        self._flow = CampaignWriteFlow(session, navigator)

    async def add_post_to_campaign(
        self,
        account_id: str,
        campaign_id: str,
        post_urn: str,
        *,
        confirm_send: bool,
    ) -> dict[str, Any]:
        """Add the company post ``post_urn`` to a DRAFT or PAUSED ad set.

        ``confirm_send=False`` verifies the ad set, finds the post in its
        content library and checks it may be sponsored, then stops without a
        click. Only a post among the library's first page of company posts can
        be found. Success is the app's creative create answered 2xx and the
        new ad found on the ad set with this post as its reference.
        """
        account = normalize_ad_account_id(account_id)
        campaign = normalize_campaign_id(campaign_id)
        post = normalize_post_urn(post_urn)
        library_path = (
            f"/campaignmanager/accounts/{account}/campaigns/{campaign}{_LIBRARY_SUFFIX}"
        )
        url = campaign_manager_url(account, f"/campaigns/{campaign}{_LIBRARY_SUFFIX}")
        page = self._session.page

        def result(status: str, message: str, **kwargs: Any) -> dict[str, Any]:
            kwargs.setdefault("retry_safe", True)
            return campaign_action_result(TOOL, url, status, message, **kwargs)

        async with self._flow.capturing(account) as capture:
            record = await self._flow.read_campaign(capture, account, campaign)
            if isinstance(record, tuple):
                return result(*record)
            built = build_campaign(record) or {}
            target = {key: built[key] for key in _TARGET_KEYS if key in built}
            refusal = _refuse_status(record.get("status"))
            if refusal is not None:
                return result(*refusal, target=target)

            found = await self._find_post(capture, library_path, post)
            if not isinstance(found, _Row):
                status, message, planned = found
                return result(status, message, target=target, planned=planned)
            index, entry, count = found.index, found.entry, found.count
            planned = {"postUrn": post, "visibility": entry["visibility"]}
            if "contentType" in entry:
                planned["contentType"] = entry["contentType"]
            if not confirm_send:
                return result(
                    "confirmation_required",
                    "The post can be added to this ad set as a new ad. Nothing "
                    "was changed; call again with confirm_send true to add it.",
                    target=target,
                    planned=planned,
                )

            await capture.settle()
            # The table follows the app's latest answer; a reload since the post
            # was checked may have moved it to another row.
            latest = content_library_entries(
                capture.matching(_is_library, found.after)[-1].payload
            )
            urns = [item["urn"] for item in latest or []]
            if len(urns) != count or urns.count(post) != 1 or urns.index(post) != index:
                return result(
                    "control_unavailable",
                    "The content library reloaded while the post was being "
                    "added. Nothing was changed; try again.",
                    target=target,
                    planned=planned,
                )
            answer, window = await self._add(
                capture, page, campaign, post, found.position, found.ids
            )
            retry_safe = retry_safe_from(window)
            common: dict[str, Any] = {
                "target": target,
                "planned": planned,
                "refused": window.refused,
                "retry_safe": retry_safe,
            }
            if answer is None:
                if window.admitted:
                    return result(
                        "save_unverified",
                        "The ad was sent to LinkedIn but no answer came back. "
                        "Check the ad set's ads before trying again.",
                        **common,
                    )
                if window.refused:
                    return result(
                        "change_blocked",
                        "Campaign Manager tried to save something other than "
                        "this post as an ad, so it was stopped. Nothing was "
                        "changed.",
                        **common,
                    )
                if urlparse(page.url).path != urlparse(library_path).path:
                    return result(
                        "needs_more_details",
                        "Campaign Manager wants more details before this post "
                        "can run as an ad, so it opened its ad editor. Nothing "
                        "was saved; finish the ad in Campaign Manager.",
                        **common,
                    )
                return result(
                    "control_unavailable",
                    "The add button was not clicked or Campaign Manager did "
                    "not respond to it. Nothing was changed.",
                    **common,
                )
            if not answer.ok:
                if 400 <= answer.status < 500:
                    return result(
                        "save_failed",
                        f"LinkedIn refused the new ad (HTTP {answer.status}). "
                        "Nothing was added.",
                        **{**common, "retry_safe": True},
                    )
                return result(
                    "save_unverified",
                    f"LinkedIn answered the new ad with HTTP {answer.status}. "
                    "Check the ad set's ads before trying again.",
                    **common,
                )

            created = _creative_id(answer.created_id)
            if created is None:
                return result(
                    "save_unverified",
                    "LinkedIn accepted the new ad but did not name it. Check the "
                    "ad set's ads before trying again.",
                    **common,
                )
            common["created_id"] = created
            applied = await self._verify(capture, account, campaign, post, created)
            if applied is None:
                return result(
                    "save_unverified",
                    "LinkedIn accepted the new ad, but it was not found on the "
                    "ad set afterwards. Check the ad set's ads before trying "
                    "again.",
                    **common,
                )
            return result(
                "added",
                "The post was added to the ad set as a new ad.",
                applied=applied,
                **common,
            )

    async def list_sponsorable_posts(
        self, account_id: str, campaign_id: str
    ) -> dict[str, Any]:
        """The company posts this ad set's content library offers, as listed.

        Reads the same library answer ``add_post_to_campaign`` reads, so each
        ``urn`` is a value that tool accepts. Read-only: Campaign Manager's
        writes are held back throughout.
        """
        account = normalize_ad_account_id(account_id)
        campaign = normalize_campaign_id(campaign_id)
        library_path = (
            f"/campaignmanager/accounts/{account}/campaigns/{campaign}{_LIBRARY_SUFFIX}"
        )
        url = campaign_manager_url(account, f"/campaigns/{campaign}{_LIBRARY_SUFFIX}")
        async with self._flow.capturing(account, reads=True) as capture:
            record = await self._flow.read_campaign(capture, account, campaign)
            if isinstance(record, tuple):
                raise InvalidReferenceError(record[1])
            async with without_campaign_manager_writes(self._session.page):
                loaded = await self._load_library(capture, library_path)
            if not isinstance(loaded, _Library):
                raise LinkedInScraperException(loaded[1])
            response, entries = loaded.response, loaded.entries

        built = build_campaign(record) or {}
        result: dict[str, Any] = {
            "url": url,
            "sections": {},
            "target": {key: built[key] for key in _TARGET_KEYS if key in built},
            "sponsorable_posts": entries,
        }
        if text := render_sponsorable_posts(entries):
            result["sections"]["sponsorable_posts"] = text
        message = _truncation(response, len(entries))
        if message is not None:
            result["section_errors"] = {
                "sponsorable_posts": _error("page_limit", message)
            }
        return result

    async def _load_library(
        self, capture: CampaignManagerCapture, library_path: str
    ) -> _Library | tuple[str, str]:
        """Open the ad set's content library on its company posts filter.

        Returns the answer the table renders, its entries and where this
        load's answers start in the capture, or ``(status, message)``. Call it
        with Campaign Manager's writes held back.
        """
        page = self._session.page
        await capture.settle()
        after = len(capture.responses)
        self._navigator._charge_navigation(f"{_LINKEDIN}{library_path}")
        await page.evaluate(_ROUTE_SCRIPT, library_path)
        await capture.wait_for(
            _is_library, after=after, timeout=_READY_TIMEOUT, wanted=_LIBRARY
        )
        if urlparse(page.url).path != urlparse(library_path).path:
            return (
                "control_unavailable",
                "Campaign Manager did not open this ad set's content library.",
            )
        await self._flow.wait_for_anchor(page, ANCHORS["library_filter"])
        pill = await self._organic_pill(page)
        if pill is None:
            return (
                "control_unavailable",
                "The content library's company posts filter is not available.",
            )
        if pill == "clicked":
            await capture.settle()
            before = len(capture.responses)
            await capture.wait_for(
                _is_library, after=before, timeout=_READY_TIMEOUT, wanted=_LIBRARY
            )
        await capture.settle()
        # The latest answer is the one the table renders.
        response = capture.matching(_is_library, after)[-1]
        entries = content_library_entries(response.payload)
        if entries is None:
            return (
                "control_unavailable",
                "Campaign Manager returned the content library in a shape this "
                "tool does not recognise.",
            )
        return _Library(response, entries, after)

    async def _find_post(
        self, capture: CampaignManagerCapture, library_path: str, post: str
    ) -> _Row | tuple[str, str, dict[str, Any]]:
        """Open the library latched; the post's row, entry and the row count.

        Returns ``(status, message, planned)`` when the post cannot be added.
        """
        page = self._session.page
        planned: dict[str, Any] = {"postUrn": post}
        async with without_campaign_manager_writes(page):
            loaded = await self._load_library(capture, library_path)
            if not isinstance(loaded, _Library):
                status, message = loaded
                return status, f"{message} Nothing was changed.", planned
            response, entries, after = loaded.response, loaded.entries, loaded.after
            index = next(
                (i for i, entry in enumerate(entries) if entry["urn"] == post), None
            )
            if index is None:
                # Only a provably whole library may say the post is not in it.
                beyond = _truncation(response, len(entries)) is not None
                return (
                    "post_not_found",
                    (
                        f"The post is not among the {len(entries)} most recent "
                        "company posts the content library lists, which is all "
                        "this tool reads."
                        if beyond
                        else "The post is not in this ad set's content library "
                        "of company posts."
                    )
                    + " Nothing was changed.",
                    planned,
                )
            entry = entries[index]
            if not entry["sponsorable"]:
                return (
                    "post_not_sponsorable",
                    "LinkedIn does not allow this post to be sponsored; "
                    "planned.reasons says why. Nothing was changed.",
                    {**planned, "reasons": list(entry["nonSponsorableReasons"])},
                )
            anchored = await self._flow.wait_for_anchor(page, ANCHORS["library_rows"])
            located = await self._rendered_row(
                page, post, index, len(entries), amend=not anchored
            )
            if located is None:
                return (
                    "control_unavailable",
                    "The content library did not show a row this tool could "
                    "tie to the post. Nothing was changed.",
                    planned,
                )
            rows, position = located
            if rows[position].get("disabled") is True:
                return (
                    "post_not_sponsorable",
                    "Campaign Manager does not allow this post for this "
                    "campaign's objective or format. Nothing was changed.",
                    {**planned, "reasons": []},
                )
            ids = tuple(str(row["id"]) for row in rows)
            return _Row(index, entry, len(entries), after, position, ids)

    async def _organic_pill(self, page: Any) -> str | None:
        for attempt in range(_ROWS_POLLS):
            pill = await select_organic(page)
            if pill is not None or attempt + 1 == _ROWS_POLLS:
                return pill
            await asyncio.sleep(_ROWS_POLL_SECONDS)
        return None

    async def _rendered_row(
        self, page: Any, post: str, index: int, count: int, *, amend: bool = False
    ) -> tuple[list[dict[str, Any]], int] | None:
        """The rendered rows and the post's row among them, once the table
        settles unselected with a disabled add button; None after the bound,
        with the table's structure recorded for the failure notes (added to the
        page's record when ``amend``: the anchor wait already recorded it)."""
        rows: list[dict[str, Any]] | None = None
        add: Any = None
        for attempt in range(_ROWS_POLLS):
            state = await library_rows(page)
            rows = _rendered(state)
            add = state.get("add") if state is not None else None
            position = locate_row(rows, post, index, count) if rows else None
            if (
                rows is not None
                and position is not None
                and isinstance(add, dict)
                and add.get("disabled") is True
                and not any(row.get("checked") for row in rows)
            ):
                return rows, position
            if attempt + 1 < _ROWS_POLLS:
                await asyncio.sleep(_ROWS_POLL_SECONDS)
        await self._flow.record_page_structure(
            page,
            amend=amend,
            library_rows={
                "answer_posts": count,
                "rows": len(rows) if rows is not None else None,
                "row_ids": _row_kinds(rows or [], post),
                "checked": sum(1 for row in rows or [] if row.get("checked")),
                "add": add if isinstance(add, dict) else None,
            },
        )
        return None

    async def _add(
        self,
        capture: CampaignManagerCapture,
        page: Any,
        campaign: str,
        post: str,
        position: int,
        ids: Sequence[str],
    ) -> tuple[CmWriteResponse | None, WriteWindow]:
        """Check the row and click add inside a window admitting one create."""
        admitted: list[WriteRequest] = []

        def admit(req: WriteRequest) -> str | None:
            if admitted:
                return "second creative create"
            reason = admit_creative_create(req, campaign_id=campaign, reference=post)
            if reason is None:
                admitted.append(req)
            return reason

        def is_create(write: CmWriteResponse) -> bool:
            body = write.body if isinstance(write.body, dict) else {}
            return (
                write.call.resource == "campaignManagerCreatives"
                and write.call.entity_id is None
                and is_create_method(write.restli_method)
                and body.get("reference") == post
            )

        prefix = urlparse(page.url).path.split("/creatives/", 1)[0] + "/"
        after = capture.write_count
        answer: CmWriteResponse | None = None
        async with self._flow.apply(
            page, admit, expected_path=lambda path: path.startswith(prefix)
        ) as window:
            if await click_library(page, "row", position, ids) and await click_library(
                page, "add", position, ids
            ):
                answer = await self._flow.wait_saved(
                    capture, is_create, after, timeout=_SAVE_TIMEOUT
                )
        return answer, window

    async def _verify(
        self,
        capture: CampaignManagerCapture,
        account: str,
        campaign: str,
        post: str,
        created: str,
    ) -> dict[str, Any] | None:
        """Re-read the ad set; the new ad if it is listed there for this post."""

        def is_creatives(response: CmResponse) -> bool:
            return (
                response.ok
                and response.call.resource == "campaignManagerCreatives"
                and response.call.finder == "campaignIds"
                and restli_list(response.call.params.get("campaignIds", ""))
                == [campaign]
            )

        await capture.settle()
        after = len(capture.responses)
        try:
            record = await self._flow.read_campaign(capture, account, campaign)
            if isinstance(record, tuple):
                return None
            response = await capture.wait_for(
                is_creatives,
                after=after,
                timeout=_CREATIVES_WAIT,
                throttle=False,
                wanted=("campaignManagerCreatives",),
            )
        except LinkedInScraperException:
            logger.debug("Re-read after adding the post failed", exc_info=True)
            return None
        for element in envelope_elements(response.payload) or []:
            creative = build_creative(element)
            if (
                creative is not None
                and str(creative["id"]) == created
                and element.get("reference") == post
            ):
                applied: dict[str, Any] = {"creativeId": created, "postUrn": post}
                if "status" in creative:
                    applied["status"] = creative["status"]
                return applied
        return None


def _refuse_status(status: Any) -> Refusal | None:
    if status == "ACTIVE":
        return (
            "campaign_active",
            "This ad set is running, and a new ad on it would start spending at "
            "once, so nothing was added. Pause the ad set first, or add the post "
            "to a draft or paused ad set.",
        )
    if status not in _ADDABLE_STATUSES:
        return (
            "not_editable",
            f"This ad set is {status or 'in an unknown state'}, so posts cannot "
            "be added to it. Nothing was changed.",
        )
    return None
