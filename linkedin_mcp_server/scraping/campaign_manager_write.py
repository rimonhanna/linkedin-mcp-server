"""The page-owning steps every Campaign Manager write tool shares.

A write runs in three phases over one capture: a latched read that verifies
the target, an apply phase inside ``explicit_campaign_write`` where only the
app's requests the plan admits leave the browser, and a latched re-read that
verifies what LinkedIn kept. This module owns the navigation, the record
read, the write window with its route pin, the footer's Save and exit, and
leaving a page whose edit was refused.

Per the AGENTS.md Scraping Rules nothing here reads visible text: the target
is identified by URL path and the record's ids, the footer button by its
container attribute and position.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import logging
import re

from patchright.async_api import Error as PlaywrightError
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.response_structure import current_log, is_field_name
from linkedin_mcp_server.scraping.campaign_manager import _SETUP_PATH, _refusal
from linkedin_mcp_server.scraping.campaign_manager_api import (
    CampaignManagerCapture,
    CmResponse,
    CmWriteResponse,
)
from linkedin_mcp_server.scraping.campaign_manager_changes import Refusal, ms_to_date
from linkedin_mcp_server.scraping.campaign_manager_guard import (
    Admission,
    CmWriteRequest,
    WriteWindow,
    explicit_campaign_write,
    without_campaign_manager_writes,
)
from linkedin_mcp_server.scraping.campaign_manager_payload import (
    envelope_record,
    same_id,
)
from linkedin_mcp_server.scraping.campaign_manager_results import (
    campaign_action_result,
)
from linkedin_mcp_server.scraping.identifiers import (
    campaign_manager_url,
    normalize_ad_account_id,
    normalize_campaign_id,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

logger = logging.getLogger(__name__)

_READY_TIMEOUT = 12.0
_SAVE_TIMEOUT = 20.0
# The editor renders after the record arrives (the app shell shows a loader
# until then).
_ANCHOR_TIMEOUT = 15.0
_DIGITS = re.compile(r"\d+")

# Every editor anchor the write tools use, observed 2026-11-17. A control
# that never renders records which of these the page had.
ANCHORS = {
    "budget_block": "[data-live-test-bid-and-budget]",
    "daily_budget": "[data-live-test-bid-and-budget] #bid-and-budget__daily-budget",
    "total_budget": "[data-live-test-bid-and-budget] #bid-and-budget__total-budget",
    "start_date": 'input[data-datepicker-input][name="start"]',
    "end_date": 'input[data-datepicker-input][name="end"]',
    "name_input": "#campaign-name",
    "status_switch": ".campaign-header-status__toggle-container input[role=switch]",
    "objective": 'input[type=radio][name="objective-selection"]',
    "footer": "[data-js-wizard-container-footer-actions]",
    "footer_disabled": "[data-js-wizard-container-footer-actions] button:disabled",
    "footer_new_next": (
        "[data-js-wizard-container-footer-actions] "
        "[data-live-test-confirm-hovercard__button--trigger]"
    ),
    "targeting_edit": "[data-js-targeting-module__edit-facet-block-btn]",
    "targeting_first_or": (
        '[data-js-targeting-module__edit-facet-block-btn="includedOrs0"]'
    ),
    "targeting_or_blocks": "[data-live-test-facet-block-included-ors]",
    "facet_search": "#facet-browser__search-input",
    "location_search": 'input[id^="targeting-entities-typeahead__search-input-"]',
    "save_audience": "#targeting-module-save-audience-button",
    "create_menu": "#ma-header-actions-create-trigger",
    "library_filter": "#sponsored-content-library__pill-filter-ORGANIC",
    "library_rows": 'input[type=checkbox][id^="select-row-"][id$="-checkbox"]',
    "library_add": ".sponsored-content-library__sponsor-button",
    "library_remove": "[data-live-test-confirm-hovercard__button--trigger]",
}
# The app shell's loader, by role and element only.
_LOADER = '[role=progressbar], progress, [aria-busy="true"]'

# Read only: an anchor rendered off screen is scrolled to, never focused,
# clicked or typed into, so a dry run waiting here cannot trigger an autosave.
_ANCHOR_JS = """(selector) => {
    /* linkedin-mcp:cm-anchor */
    const shown = [...document.querySelectorAll(selector)]
        .filter((el) => el.getClientRects().length > 0);
    if (shown.length === 0) return false;
    const box = shown[0].getBoundingClientRect();
    if (box.bottom < 0 || box.top > window.innerHeight) {
        shown[0].scrollIntoView({ block: 'center' });
    }
    return true;
}"""
# Counts and visibility per anchor; no text, no values.
_STRUCTURE_JS = """({ anchors, loader }) => {
    /* linkedin-mcp:cm-anchor */
    const visible = (el) => el.getClientRects().length > 0;
    const out = {};
    for (const [name, selector] of Object.entries(anchors)) {
        const found = [...document.querySelectorAll(selector)];
        out[name] = { count: found.length, visible: found.filter(visible).length };
    }
    return { anchors: out, loading: [...document.querySelectorAll(loader)].some(visible) };
}"""
# Save and exit is the footer button right before Next: after Previous on a
# draft, alone on a launched campaign (observed 2026-11).
_FOOTER_CANDIDATES = {True: 2, False: 1}
_FOOTER_JS = """(expected) => {
    /* linkedin-mcp:cm-footer */
    const footers = [...document.querySelectorAll(
        '[data-js-wizard-container-footer-actions]'
    )];
    if (footers.length !== 1) return { count: -footers.length, clicked: false };
    const candidates = [...footers[0].querySelectorAll(
        'button:not([data-live-test-wizard-container__button--next])'
    )];
    const target = candidates[candidates.length - 1];
    const buttons = [...footers[0].querySelectorAll('button')];
    const next = buttons[buttons.length - 1];
    if (
        candidates.length !== expected ||
        buttons[buttons.length - 2] !== target ||
        !next.matches('[data-live-test-wizard-container__button--next]') ||
        target.getClientRects().length === 0
    ) {
        return { count: candidates.length, clicked: false };
    }
    if (target.disabled) return { count: candidates.length, clicked: false, disabled: true };
    target.click();
    return { count: candidates.length, clicked: true };
}"""
# A new ad set's footer (the app's bundle, 2026-11-18): Next is a confirm
# hovercard's trigger, and Save and exit the visible button right before it in
# the same container; Previous, when shown, sits in another container.
_NEW_FOOTER_JS = """() => {
    /* linkedin-mcp:cm-footer */
    const footers = [...document.querySelectorAll(
        '[data-js-wizard-container-footer-actions]'
    )];
    if (footers.length !== 1) return { count: -footers.length, clicked: false };
    const buttons = [...footers[0].querySelectorAll('button')]
        .filter((el) => el.getClientRects().length > 0);
    const next = buttons[buttons.length - 1];
    const target = buttons[buttons.length - 2];
    const triggers = buttons.filter(
        (el) => el.hasAttribute('data-live-test-confirm-hovercard__button--trigger')
    );
    if (
        !target ||
        triggers.length !== 1 ||
        triggers[0] !== next ||
        !target.parentElement.contains(next) ||
        footers[0].querySelector('[data-live-test-wizard-container__button--next]')
    ) {
        return { count: buttons.length, clicked: false };
    }
    if (target.disabled) return { count: buttons.length, clicked: false, disabled: true };
    target.click();
    return { count: buttons.length, clicked: true };
}"""
# The classic editor's footer. That editor never autosaves, also not a draft:
# its fields reach LinkedIn only by the footer's Save and exit. Only the newer
# editor (``icc`` in the app's code, its own footer) autosaves a draft's fields
# (read from the app's bundle, 2026-11-17).
_CLASSIC_FOOTER_JS = """() => {
    /* linkedin-mcp:cm-footer */
    return document.querySelectorAll('[data-js-wizard-container-footer-actions]').length;
}"""


def _not_found(message: str) -> Refusal:
    return (
        "target_not_found",
        f"{message} Nothing was changed. Pass the ad_account_id and campaign_id "
        "exactly as list_campaigns returned them.",
    )


def footer_disabled_reason(
    record: Mapping[str, Any], *, start_moved: bool = False
) -> str:
    """Why LinkedIn keeps an ad set's Save and exit disabled, as far as known.

    The classic editor disables it while the ad set fails its own validation
    (``isTriggerButtonDisabled`` in the app's bundle, 2026-11-18). The one cause
    the record shows is a start date that has passed on a not yet started ad
    set: LinkedIn shows that day as its UTC date and then wants it moved.
    ``start_moved`` says the edit already moved it, so it is not the cause.
    """
    schedule = record.get("runSchedule")
    start = schedule.get("start") if isinstance(schedule, Mapping) else None
    passed = (
        not start_moved
        and record.get("status") == "DRAFT"
        and isinstance(start, int)
        and not isinstance(start, bool)
        and ms_to_date(start) < datetime.now(UTC).date()
    )
    if passed:
        return (
            "LinkedIn will not save this ad set because its start date has "
            "passed. Set a start date of today or later with update_campaign, "
            "then try again."
        )
    return (
        "LinkedIn will not save this ad set while one of its fields is "
        "invalid, so its Save and exit button stayed disabled. Open the ad set "
        "in Campaign Manager, correct the field it marks, then try again."
    )


def _request_shape(request: CmWriteRequest) -> dict[str, Any]:
    """A write request's structure: field names and flags, never a value."""
    shape: dict[str, Any] = {
        "method": request.method,
        "resource": request.call.resource,
        "restli_method": request.restli_method,
        "entity_id": request.call.entity_id is not None,
    }
    body = request.body if isinstance(request.body, dict) else None
    patch = body.get("patch") if body is not None else None
    if isinstance(patch, dict):
        shape["patch_keys"] = sorted(key for key in patch if is_field_name(key))
        updates = patch.get("$set")
        if isinstance(updates, dict):
            shape["set_keys"] = sorted(key for key in updates if is_field_name(key))
    elif body is not None:
        shape["body_keys"] = sorted(key for key in body if is_field_name(key))
    return shape


def record_write_window(
    window: WriteWindow,
    *,
    saves: str,
    step: str,
    outcome: str,
    inputs: Mapping[str, Any] | None = None,
) -> None:
    """Note what a stopped change's window let through and refused, value-free.

    ``saves`` is how the editor saves (``autosave`` or ``save_and_exit``),
    ``outcome`` what the wait ended on, ``inputs`` each planned input's
    state by field: only its counts and flags are kept.
    """
    log = current_log()
    if log is None:
        return
    summary: dict[str, Any] = {
        "saves": saves,
        "step": step,
        "outcome": outcome,
        "admitted": [_request_shape(request) for request in window.admitted],
        "refused": [
            {**_request_shape(request), "reason": reason}
            for request, reason in window.refused
        ],
    }
    if inputs:
        summary["inputs"] = {
            field: {
                key: value
                for key, value in state.items()
                if isinstance(value, bool | int) and is_field_name(key)
            }
            for field, state in inputs.items()
            if isinstance(state, dict)
        }
    log.add_write(summary)


class WriteLedger:
    """Every write window one tool call opened, for the tool's error path.

    A flow catches only its own stop types, so a Playwright timeout, a closed
    page or a cancellation after a write left the browser escapes it. The tool
    layer reads this ledger then: with a write admitted the outcome is
    ``save_unverified`` and counted, never an error the agent retries.
    """

    def __init__(self) -> None:
        self.windows: list[WriteWindow] = []
        self.url = ""

    @property
    def admitted(self) -> bool:
        return any(window.admitted for window in self.windows)

    def unverified(self, tool: str, error: BaseException) -> dict[str, Any]:
        """The result for ``error`` raised after a write was admitted."""
        return campaign_action_result(
            tool,
            self.url,
            "save_unverified",
            "A change was sent to LinkedIn, then the tool stopped on an "
            f"unexpected error ({type(error).__name__}), so whether LinkedIn "
            "kept it is unknown. Check Campaign Manager before calling again.",
            retry_safe=False,
            refused=[pair for window in self.windows for pair in window.refused],
        )


_ledger: ContextVar[WriteLedger | None] = ContextVar(
    "campaign_write_ledger", default=None
)


@contextmanager
def tracking_writes() -> Iterator[WriteLedger]:
    """Collect the write windows opened in this context into one ledger."""
    ledger = WriteLedger()
    token = _ledger.set(ledger)
    try:
        yield ledger
    finally:
        _ledger.reset(token)


class _RoutePin:
    """Where the page was when the window opened, and where it went since."""

    def __init__(self, page: Any, expected: Callable[[str], bool] | None) -> None:
        self._page = page
        self._path = urlparse(page.url).path
        self._expected = expected
        self.moved_to: str | None = None
        self.listener = self._record

    def _record(self, frame: Any) -> None:
        if frame != self._page.main_frame or self.moved_to is not None:
            return
        path = urlparse(frame.url).path
        if path != self._path and not (self._expected and self._expected(path)):
            self.moved_to = path


class CampaignWriteFlow:
    """Navigation, verification and the write window shared by every write tool."""

    def __init__(self, session: ScrapingSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator
        self._pin: _RoutePin | None = None
        # The page path an anchor wait last timed out on.
        self._timed_out_on: str | None = None
        # Whether the last unclicked Save and exit was there but disabled.
        self.footer_disabled = False

    @asynccontextmanager
    async def capturing(self) -> AsyncIterator[CampaignManagerCapture]:
        """One capture spanning the read, apply and verify phases.

        Each tool call opens one, so a page that timed out in an earlier call
        gets the full anchor wait again.
        """
        self._timed_out_on = None
        async with CampaignManagerCapture(self._session.page) as capture:
            yield capture

    async def read_campaign(
        self,
        capture: CampaignManagerCapture,
        account_id: str,
        campaign_id: str,
        *,
        suffix: str = "/details",
    ) -> dict[str, Any] | Refusal:
        """Open the campaign's ``suffix`` page latched and return its record.

        Only a record that arrives after this call counts, so a re-read after
        a save never returns the record from before it. A setup redirect, an
        id LinkedIn refuses and a campaign of another account are refusals
        (``target_not_found``); a record that never arrives raises.
        """
        account = normalize_ad_account_id(account_id)
        campaign = normalize_campaign_id(campaign_id)
        url = campaign_manager_url(account, f"/campaigns/{campaign}{suffix}")
        refused = _refusal(account, campaign)

        def is_campaign(response: CmResponse) -> bool:
            record = envelope_record(response.payload)
            return (
                response.ok
                and response.call.resource == "campaignManagerCampaigns"
                and response.call.finder is None
                and response.call.entity_id == campaign
                and record is not None
                and same_id(record.get("id"), campaign)
            )

        page = self._session.page
        async with without_campaign_manager_writes(page):
            await capture.settle()
            after = len(capture.responses)
            await self._navigator._navigate_to_page(url)
            await self._session.check_rate_limit()
            await self._session.dismiss_modal()
            if _SETUP_PATH in urlparse(page.url).path:
                return _not_found(
                    "Campaign Manager opened its campaign setup page instead of "
                    "this ad set."
                )
            response = await capture.wait_for(
                lambda r: is_campaign(r) or refused(r) is not None,
                after=after,
                timeout=_READY_TIMEOUT,
                wanted=("campaignManagerCampaigns",),
            )
            await capture.settle()
        self._timed_out_on = None
        field = refused(response)
        if field is not None:
            return _not_found(
                f"Campaign Manager refused that {field} (HTTP {response.status})."
            )
        record = envelope_record(response.payload) or {}
        if not isinstance(record.get("accountId"), int):
            raise LinkedInScraperException(
                "Campaign Manager returned the ad set in a shape this tool does "
                "not recognise, so nothing was changed."
            )
        if record["accountId"] != int(account):
            return _not_found("That ad set does not belong to that ad account.")
        return record

    async def wait_for_anchor(self, page: Any, selector: str) -> bool:
        """Whether ``selector`` is attached and visible within ``_ANCHOR_TIMEOUT``.

        Once a wait on a page (a path, until the next ``read_campaign``) has
        timed out, later waits there only check once, so a page that never
        renders costs the timeout once. A timeout records the page's anchor
        structure for the failure notes. A page that navigates during the
        wait is also False: the caller's own checks decide what that means.
        """
        path = urlparse(page.url).path
        timeout = 1 if path == self._timed_out_on else _ANCHOR_TIMEOUT * 1000
        try:
            await page.wait_for_function(_ANCHOR_JS, arg=selector, timeout=timeout)
        except PlaywrightTimeoutError:
            self._timed_out_on = path
            await self.record_page_structure(page)
            return False
        except PlaywrightError:
            logger.debug("Anchor wait interrupted", exc_info=True)
            return False
        return True

    async def record_page_structure(
        self, page: Any, *, amend: bool = False, **extra: Any
    ) -> None:
        """Note which known anchors the page has, for the failure notes.

        ``extra`` adds a caller's own value-free summary. With ``amend`` it is
        added to this page's latest record instead, so one stop is recorded
        once. A diagnostic only: a page that navigates meanwhile is not
        recorded.
        """
        log = current_log()
        path = _DIGITS.sub("N", urlparse(page.url).path)
        if (
            amend
            and log is not None
            and log.pages
            and log.pages[-1].get("path") == path
        ):
            log.pages[-1].update(extra)
            return
        try:
            summary = await page.evaluate(
                _STRUCTURE_JS, {"anchors": ANCHORS, "loader": _LOADER}
            )
        except PlaywrightError:
            logger.debug("Page structure not recorded", exc_info=True)
            return
        if log is None or not isinstance(summary, dict):
            logger.debug("Page structure not recorded: %s", summary)
            return
        log.add_page({"path": path, **summary, **extra})

    @asynccontextmanager
    async def apply(
        self,
        page: Any,
        admit: Admission,
        *,
        expected_path: Callable[[str], bool] | None = None,
    ) -> AsyncIterator[WriteWindow]:
        """Open the write window, pinned to the page's current route.

        Once the main frame moves to a path that is neither the one at entry
        nor one ``expected_path`` accepts, every further write is refused and
        ``moved_to`` names the path, so a click that took the app somewhere
        unplanned cannot save anything there. The window joins the current
        ``tracking_writes`` ledger, so an error after an admitted write is
        reported as unverified by the tool layer whichever flow raised it.
        """
        pin = _RoutePin(page, expected_path)
        self._pin = pin

        def pinned(request: CmWriteRequest) -> str | None:
            if pin.moved_to is not None:
                return "the page left the planned route"
            return admit(request)

        page.on("framenavigated", pin.listener)
        try:
            async with explicit_campaign_write(page, admit=pinned) as window:
                ledger = _ledger.get()
                if ledger is not None:
                    ledger.windows.append(window)
                    ledger.url = page.url
                yield window
        finally:
            try:
                page.remove_listener("framenavigated", pin.listener)
            except Exception:
                logger.debug("Could not remove route pin listener", exc_info=True)

    def moved_to(self) -> str | None:
        """The unplanned path the last write window's page moved to, if any."""
        return self._pin.moved_to if self._pin is not None else None

    async def save_and_exit(self, page: Any, *, draft: bool) -> bool:
        """Click the footer's Save and exit; False, unclicked, on any other layout.

        The footer must be the only one, hold exactly the buttons a draft or a
        launched campaign shows, and its last one must be enabled and visible.
        Unclicked, the page's anchors are recorded and ``footer_disabled`` says
        whether the button was there but disabled.
        """
        return await self._click_footer(
            page, await page.evaluate(_FOOTER_JS, _FOOTER_CANDIDATES[draft])
        )

    async def save_new_and_exit(self, page: Any) -> bool:
        """``save_and_exit`` on a new ad set's footer, whose Next is a hovercard."""
        return await self._click_footer(page, await page.evaluate(_NEW_FOOTER_JS))

    async def _click_footer(self, page: Any, result: Any) -> bool:
        clicked = isinstance(result, dict) and result.get("clicked") is True
        self.footer_disabled = (
            isinstance(result, dict) and result.get("disabled") is True
        )
        if not clicked:
            logger.debug("Save and exit not clicked: %s", result)
            await self.record_page_structure(page)
        return clicked

    async def saves_by_footer(self, page: Any) -> bool:
        """Whether the page is the classic editor, which saves only by its footer."""
        return await page.evaluate(_CLASSIC_FOOTER_JS) == 1

    async def wait_saved(
        self,
        capture: CampaignManagerCapture,
        predicate: Callable[[CmWriteResponse], bool],
        after: int,
        *,
        timeout: float = _SAVE_TIMEOUT,
    ) -> CmWriteResponse | None:
        """LinkedIn's answer to an admitted write from ``after`` on; None if none came."""
        return await capture.wait_for_write(predicate, after=after, timeout=timeout)

    async def leave(self, page: Any, url: str) -> None:
        """Navigate to ``url``, discarding the page's unsaved edit.

        Only used after our own edit was refused, so the browser's leave-page
        prompt is accepted; any other dialog is dismissed. The handler is
        removed afterwards so no later tool's dialog is answered by it.
        """

        async def answer(dialog: Any) -> None:
            if dialog.type == "beforeunload":
                await dialog.accept()
            else:
                await dialog.dismiss()

        page.on("dialog", answer)
        try:
            await self._navigator._navigate_to_page(url)
        finally:
            try:
                page.remove_listener("dialog", answer)
            except Exception:
                logger.debug("Could not remove dialog listener", exc_info=True)
