"""Save a Sales Navigator lead or account search, through its own controls.

The search page is loaded from the URL the search tools build (a charged page
load), and its own search request must carry exactly the asked filters, as
for a search. Then the page's "Save search" switch is turned on, which makes
the app save the search under a name it generates, and the Saved searches
panel's Edit form renames it to the asked name. ``sales_navigator_list_guard``
holds the page throughout: every Sales Navigator write is aborted except,
inside the window around each of those two presses, the app's own request
when it is exactly the planned one. The search page is then loaded again and
the saved search read back from the panel by its id, name and filters before
it is reported saved. The request shapes are in
``sales_navigator_saved_search_payload``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from linkedin_mcp_server.exceptions import ActionLimitError
from linkedin_mcp_server.pacing import (
    SALES_LIST_WRITES,
    JobStore,
    admit_write,
    cooldown_resume_at,
    note_throttle_signal,
    record_write,
)
from linkedin_mcp_server.scraping.contracts import before_the_reply_deadline
from linkedin_mcp_server.scraping.sales_navigator_list_guard import (
    ListWriteWindow,
    PlannedListWrite,
    explicit_list_write,
    without_sales_list_writes,
)
from linkedin_mcp_server.scraping.sales_navigator_payload import (
    SearchFilter,
    build_search_query,
    search_page_url,
)
from linkedin_mcp_server.scraping.sales_navigator_saved_search_payload import (
    KINDS,
    SAVED_SEARCHES,
    create_request,
    created_key,
    find_saved,
    readback_mismatch,
    rename_request,
    saved_search_result,
)

logger = logging.getLogger(__name__)

# The results page's save switch and the Saved searches panel, by the app's
# own untranslated attributes (measured 2026-11-27). The switch is a hidden
# checkbox whose parent element takes the click; the panel's tabs are Lead
# then Account, and its Edit form's last button that is not Delete is Save.
SAVE_SWITCH = 'input[id^="save-search-"][type=checkbox]'
PANEL_OPENER = "button[data-x--link--saved-searches]"
PANEL = '[data-sn-view-name="subpage-saved-searches-panel"]'
PANEL_TABS = f"{PANEL} button[role=tab]"
NAME_FIELD = "input[data-saved-search-panel-input]"
EDIT_FORM = f"li:has({NAME_FIELD})"
FORM_BUTTONS = f"{EDIT_FORM} button:not([data-saved-search-panel-delete-button])"
_FORM_TIMEOUT_MS = 10_000
_TYPE_DELAY_MS = 20
_SEND_TIMEOUT = 10.0
_ANSWER_TIMEOUT = 30.0
_PANEL_READ_TIMEOUT = 15.0
_SWITCH_JS = """(input) => { input.parentElement.click(); }"""


class _Stopped(Exception):
    """A stop before a request was sent: the cap, a cooldown or the page."""


class SavedSearchLedger:
    """What one call sent, for the tool's error path (see ``ListWriteLedger``)."""

    def __init__(self) -> None:
        self.windows: list[ListWriteWindow] = []
        self.kind = ""
        self.name = ""
        self.search_url: str | None = None
        self.saved_id: str | None = None

    @property
    def admitted(self) -> bool:
        return any(window.admitted for window in self.windows)

    def unconfirmed(self, error: BaseException) -> dict[str, Any]:
        return saved_search_result(
            "save_unconfirmed",
            "The save was sent, then the tool stopped on an unexpected error "
            f"({type(error).__name__}), so whether Sales Navigator saved the "
            "search under that name is unknown. Check Saved searches in Sales "
            "Navigator before trying again; a repeat could save it twice.",
            dry_run=False,
            kind=self.kind,
            name=self.name,
            retry_safe=False,
            search_url=self.search_url,
            saved_id=self.saved_id,
        )


_ledger: ContextVar[SavedSearchLedger | None] = ContextVar(
    "sales_saved_search_ledger", default=None
)


@contextmanager
def tracking_saved_search() -> Iterator[SavedSearchLedger]:
    ledger = SavedSearchLedger()
    token = _ledger.set(ledger)
    try:
        yield ledger
    finally:
        _ledger.reset(token)


def _current_ledger() -> SavedSearchLedger:
    return _ledger.get() or SavedSearchLedger()


class SalesNavigatorSearchSaver:
    """Own the saved-search write; the reader owns the page loads."""

    def __init__(self, reader: Any):
        self._reader = reader
        self._session = reader._session

    async def plan(
        self, kind: str, criteria: Mapping[str, Any]
    ) -> tuple[str | None, list[tuple[str, SearchFilter]], str]:
        """Keywords, filters and page-1 URL, exactly as the search tool builds them."""
        if kind == "leads":
            keywords, filters = await self._reader.lead_criteria(**criteria)
        else:
            keywords, filters = await self._reader.account_criteria(**criteria)
        query = build_search_query(keywords, [f for _, f in filters])
        return keywords, filters, search_page_url(KINDS[kind][0], query, 1)

    def _admit(self, needed: int = 1) -> None:
        """Refuse unless the next ``needed`` writes all fit under every cap now."""
        now = datetime.now(timezone.utc)
        resume_at = cooldown_resume_at(JobStore(), now)
        if resume_at is not None:
            raise _Stopped(
                "The account is paused after LinkedIn pushed back, until "
                f"{resume_at.isoformat(timespec='seconds')}; nothing was sent."
            )
        try:
            admit_write(JobStore(), SALES_LIST_WRITES, now, needed=needed)
        except ActionLimitError as exc:
            raise _Stopped(f"{exc} Nothing was sent.") from exc

    async def _press(
        self, planned: PlannedListWrite, press: Any
    ) -> tuple[bool, dict[str, Any] | None]:
        """Press the app's control inside ``planned``'s window.

        Returns whether the app's request was admitted, and its answer
        (``None`` when no answer arrived).
        """
        page = self._session.page
        answer: dict[str, Any] | None = None
        async with explicit_list_write(page, planned) as window:
            _current_ledger().windows.append(window)
            try:
                with before_the_reply_deadline():
                    await press()
                    try:
                        async with asyncio.timeout(_SEND_TIMEOUT):
                            await window.decided.wait()
                    except TimeoutError:
                        # Off the page before the window closes: a request
                        # the app sends late cannot leave once the tool has
                        # answered that nothing was sent.
                        await self._leave_page()
                        raise
                    request = window.request
                    if request is not None:
                        async with asyncio.timeout(_ANSWER_TIMEOUT):
                            response = await request.response()
                            if response is not None:
                                answer = {
                                    "status": response.status,
                                    "restliId": await response.header_value(
                                        "x-restli-id"
                                    ),
                                }
            except Exception:
                logger.warning("The Sales Navigator search save failed", exc_info=True)
            finally:
                if window.admitted:
                    record_write(
                        JobStore(), SALES_LIST_WRITES, datetime.now(timezone.utc)
                    )
        if answer is not None and answer["status"] == 429:
            note_throttle_signal("http_429")
        return bool(window.admitted), answer

    async def _leave_page(self) -> None:
        try:
            await self._session.page.goto("about:blank")
        except Exception:
            logger.warning("Could not leave the Sales Navigator page", exc_info=True)

    async def _open_panel(self, kind: str) -> None:
        page = self._session.page
        await page.locator(PANEL_OPENER).first.click()
        tabs = page.locator(PANEL_TABS)
        await tabs.first.wait_for(state="visible", timeout=_FORM_TIMEOUT_MS)
        if kind == "accounts":
            await tabs.nth(1).click()

    async def _rename(self, kind: str, saved_id: str, planned: PlannedListWrite) -> Any:
        """Open the panel's Edit form for ``saved_id`` and type the name; its Save."""
        page = self._session.page
        await self._open_panel(kind)
        edit = page.locator(
            f'{PANEL} button.edit-saved-search-button[data-saved-search="{saved_id}"]'
        )
        try:
            await edit.first.wait_for(state="visible", timeout=_FORM_TIMEOUT_MS)
        except Exception:
            raise _Stopped(
                "Sales Navigator's Saved searches panel did not show the new "
                "search, so it was not renamed."
            ) from None
        await edit.first.click()
        name_field = page.locator(NAME_FIELD).first
        try:
            await name_field.wait_for(state="visible", timeout=_FORM_TIMEOUT_MS)
        except Exception:
            raise _Stopped(
                "Sales Navigator did not open the saved search's Edit form, so "
                "it was not renamed."
            ) from None
        name = planned.body["patch"]["$set"]["name"]
        await name_field.fill("")
        await name_field.press_sequentially(name, delay=_TYPE_DELAY_MS)
        buttons = page.locator(FORM_BUTTONS)
        count = await buttons.count()
        save = buttons.nth(count - 1)
        if (
            await name_field.input_value() != name
            or count != 2
            or not await save.is_enabled()
        ):
            raise _Stopped(
                "Sales Navigator's Edit form did not take the name as given, so "
                "the search was not renamed."
            )
        return save

    async def _read_back(
        self, kind: str, url: str, saved_id: str
    ) -> Mapping[str, Any] | None:
        """Load the search page again and read ``saved_id`` from the panel."""
        page = self._session.page
        finder = KINDS[kind][3]
        reads: list[Any] = []

        def is_read(response: Any) -> bool:
            request = response.request
            return (
                request.method.upper() == "GET"
                and f"/sales-api/{SAVED_SEARCHES}?" in response.url
                and f"q={finder}" in response.url
            )

        async def collect(response: Any) -> None:
            if is_read(response):
                try:
                    reads.append(json.loads(await response.text()))
                except Exception:
                    logger.warning("A saved-search read was unreadable", exc_info=True)

        def listener(response: Any) -> None:
            asyncio.ensure_future(collect(response))

        await self._reader._navigator._navigate_to_page(url)
        page.on("response", listener)
        try:
            await self._open_panel(kind)
            async with asyncio.timeout(_PANEL_READ_TIMEOUT):
                while not reads:
                    await asyncio.sleep(0.25)
        except TimeoutError:
            return None
        finally:
            try:
                page.remove_listener("response", listener)
            except Exception:
                pass
        for payload in reads:
            if (found := find_saved(payload, saved_id)) is not None:
                return found
        return None

    async def save_search(
        self,
        kind: str,
        name: str,
        *,
        criteria: Mapping[str, Any],
        confirm_send: bool,
    ) -> dict[str, Any]:
        keywords, filters, url = await self.plan(kind, criteria)
        if not confirm_send:
            return saved_search_result(
                "dry_run",
                "Nothing was saved. confirm_send true loads this search, saves "
                "it with Sales Navigator's Save search and renames it to this "
                "name: two requests.",
                dry_run=True,
                kind=kind,
                name=name,
                search_url=url,
            )
        ledger = _current_ledger()
        ledger.kind, ledger.name, ledger.search_url = kind, name, url

        def finish(status: str, message: str, **extra: Any) -> dict[str, Any]:
            return saved_search_result(
                status,
                message,
                dry_run=False,
                kind=kind,
                name=name,
                search_url=url,
                saved_id=ledger.saved_id,
                **extra,
            )

        criteria_only: Sequence[SearchFilter] = [f for _, f in filters]
        page = self._session.page
        async with without_sales_list_writes(page):
            # Raises when the page did not search exactly the asked filters.
            await self._reader._search(
                KINDS[kind][0],
                "salesApiLeadSearch" if kind == "leads" else "salesApiAccountSearch",
                keywords,
                filters,
                1,
            )
            create = create_request(kind, keywords, criteria_only)
            switch = page.locator(SAVE_SWITCH)
            try:
                # Both writes, the create and the rename: a create the rename
                # cannot follow would leave the search under the app's name.
                self._admit(needed=2)
                if await switch.count() != 1 or await switch.first.is_checked():
                    raise _Stopped(
                        "Sales Navigator did not show its Save search switch "
                        "turned off on this search, so nothing was sent."
                    )
            except _Stopped as exc:
                return finish("not_sent", str(exc))
            admitted, answer = await self._press(
                create, lambda: switch.first.evaluate(_SWITCH_JS)
            )
            if not admitted:
                return finish(
                    "not_sent",
                    "Sales Navigator's save request was not exactly this search "
                    "and was stopped in the browser; nothing was saved.",
                )
            status = (answer or {}).get("status") or 0
            if status == 0 or status >= 500 or status == 429:
                return finish(
                    "save_unconfirmed",
                    "The save was sent but "
                    + (
                        "no answer arrived"
                        if not status
                        else f"Sales Navigator answered HTTP {status}"
                    )
                    + ". Check Saved searches in Sales Navigator before trying "
                    "again; a repeat could save it twice.",
                    retry_safe=False,
                )
            if not 200 <= status < 300:
                return finish(
                    "refused_by_linkedin",
                    f"Sales Navigator refused the save (HTTP {status}); nothing "
                    "was saved.",
                    retry_safe=False,
                )
            key = created_key((answer or {}).get("restliId"))
            if key is None:
                return finish(
                    "save_unconfirmed",
                    "Sales Navigator accepted the save but named no saved search, "
                    "so it could not be renamed. Check Saved searches in Sales "
                    "Navigator before trying again.",
                    retry_safe=False,
                )
            seat, ledger.saved_id = key
            rename = rename_request(seat, ledger.saved_id, name)
            try:
                self._admit()
                save = await self._rename(kind, ledger.saved_id, rename)
            except _Stopped as exc:
                return finish(
                    "name_not_set",
                    f"Sales Navigator saved the search under its own name. {exc} "
                    "Rename it in Saved searches; do not save it again.",
                    retry_safe=False,
                )
            admitted, answer = await self._press(rename, save.click)
            status = (answer or {}).get("status") or 0
            if not admitted or not 200 <= status < 300:
                return finish(
                    "name_unconfirmed",
                    "Sales Navigator saved the search, but whether it took the "
                    "name is "
                    + ("not known" if admitted else "no: the rename was stopped")
                    + ". Check Saved searches; do not save it again.",
                    retry_safe=False,
                )
            with before_the_reply_deadline() as reading:
                element = await self._read_back(kind, url, ledger.saved_id)
            if reading.cancelled_caught or element is None:
                return finish(
                    "save_unverified",
                    "Sales Navigator answered the save and the rename, but the "
                    "saved search could not be read back. Check Saved searches "
                    "before trying again.",
                    retry_safe=False,
                )
            if why := readback_mismatch(element, name, keywords, criteria_only):
                return finish(
                    "save_unverified",
                    f"Sales Navigator saved a search, but {why} when read back. "
                    "Check Saved searches before trying again.",
                    retry_safe=False,
                )
            return finish(
                "saved",
                "Sales Navigator saved the search under this name; read back.",
                retry_safe=False,
            )
