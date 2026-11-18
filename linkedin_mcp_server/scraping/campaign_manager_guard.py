"""Page-scoped routing that keeps Campaign Manager reads from writing.

An account without an active campaign is redirected to the campaign setup page,
and the app then POSTs to its own billing configuration unprompted. A read tool
must not let that through, so every Campaign Manager API request that is not a
read is aborted while a read has latched the page. ``page.route`` does not see
requests a service worker handles; measured 2026-11 that www.linkedin.com
registers no service worker, so every Campaign Manager request reaches the route.

Once latched, only ``explicit_campaign_write`` lets a non-read request through,
and only the ones its ``admit`` accepts.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from weakref import WeakKeyDictionary

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.scraping.campaign_manager_payload import (
    CmCall,
    parse_cm_call,
)

_CM_ROUTES = ("**/campaign-manager-api/**",)
# See ``messaging_read_state``: a page's first route mid-session can hang on
# Patchright's per-session cache toggle, so pages are routed at creation and
# this bound covers a page that reaches a read without that.
_REGISTRATION_TIMEOUT_SECONDS = 15.0

# Resources the app POSTs to while pages and blocks open, observed live
# 2026-11 to answer with data and change nothing. Their ``?action=`` POSTs to
# campaigns, accounts and legal-term tracking are not here: unobserved effect.
READ_ONLY_POST_RESOURCES = frozenset(
    f"campaignManager{name}"
    for name in (
        "EntityRecommendations",
        "Limits",
        "LegoWidgets",
        "AudienceCounts",
        "InventoryReach",
        "AudienceInsights",
    )
)


def is_campaign_manager_read(
    method: str, headers: Mapping[str, str], resource: str | None
) -> bool:
    """GET, a POST tunneling a GET, or a POST to a read-only resource."""
    method = method.upper()
    if method == "GET":
        return True
    if method != "POST":
        return False
    lowered = {key.casefold(): value for key, value in headers.items()}
    if lowered.get("x-http-method-override", "").casefold() == "get":
        return True
    return resource in READ_ONLY_POST_RESOURCES


@dataclass(frozen=True, slots=True)
class CmWriteRequest:
    call: CmCall
    url: str
    method: str
    restli_method: str | None
    # The JSON-decoded body; None when absent or not JSON.
    body: Any


@dataclass(slots=True)
class WriteWindow:
    admitted: list[CmWriteRequest] = field(default_factory=list)
    refused: list[tuple[CmWriteRequest, str]] = field(default_factory=list)


Admission = Callable[[CmWriteRequest], "str | None"]


def _json_body(request: Any) -> Any:
    try:
        text = request.post_data
        return json.loads(text) if text else None
    except ValueError:
        return None


class _PageGuard:
    def __init__(self) -> None:
        # Nothing is blocked until a Campaign Manager read latches it.
        self.read_mode = False
        self.admission: tuple[Admission, WriteWindow] | None = None
        self.registered: set[str] = set()
        self.registration: asyncio.Task[None] | None = None
        self.registration_failed = False
        self.route_handler = self.handle

    async def handle(self, route: Any) -> None:
        request = route.request
        method = request.method.upper()
        admission = self.admission
        if method == "GET" or not (self.read_mode or admission is not None):
            await route.fallback()
            return
        # The tunnel and Rest.li method headers can be absent from
        # ``request.headers`` while it is provisional, so read the full set.
        headers = {
            key.casefold(): value
            for key, value in (await request.all_headers()).items()
        }
        call = parse_cm_call(request.url, method, headers, request.post_data)
        if is_campaign_manager_read(method, headers, call.resource if call else None):
            await route.fallback()
            return
        # The window can close, or another open, while the headers are read.
        if admission is None or call is None or self.admission is not admission:
            await route.abort()
            return
        admit, window = admission
        write = CmWriteRequest(
            call=call,
            url=request.url,
            method=method,
            restli_method=headers.get("x-restli-method"),
            body=_json_body(request),
        )
        try:
            reason = admit(write)
        except Exception:
            reason = "admission error"
        if reason is None:
            # Recorded before it leaves, so a failed send still counts as sent.
            window.admitted.append(write)
            await route.fallback()
        else:
            window.refused.append((write, reason))
            await route.abort()

    async def install(self, page: Any) -> None:
        if self.registration_failed:
            raise LinkedInScraperException(
                "Campaign Manager write protection registration failed. Call "
                "close_session to restart the browser, then retry."
            )
        if self.registration is None:
            self.registration = asyncio.create_task(self._install(page))
            self.registration.add_done_callback(self._registration_finished)
        # A cancelled or timed-out caller must not leave a half-registered
        # route that a later caller registers again, so the task outlives it.
        try:
            async with asyncio.timeout(_REGISTRATION_TIMEOUT_SECONDS):
                await asyncio.shield(self.registration)
        except TimeoutError:
            # Reading unguarded would let the app create a billing configuration.
            raise LinkedInScraperException(
                "Campaign Manager write protection did not register within "
                f"{_REGISTRATION_TIMEOUT_SECONDS:g}s, so Campaign Manager was "
                "not opened and nothing was changed. Call close_session to "
                "restart the browser, then retry."
            ) from None

    def _registration_finished(self, task: asyncio.Task[None]) -> None:
        if task.cancelled() or task.exception() is not None:
            self.registration_failed = True
            # A failed task's traceback retains the page through this weak-map value.
            self.registration = None

    async def _install(self, page: Any) -> None:
        for pattern in _CM_ROUTES:
            if pattern not in self.registered:
                await page.route(pattern, self.route_handler)
                self.registered.add(pattern)


# Values do not retain the page; the page owns its callbacks until it closes.
_GUARDS: WeakKeyDictionary[Any, _PageGuard] = WeakKeyDictionary()


def _guard_for(page: Any) -> _PageGuard:
    state = _GUARDS.get(page)
    if state is None:
        state = _PageGuard()
        _GUARDS[page] = state
    return state


async def register_campaign_manager_routes(page: Any) -> None:
    """Route the Campaign Manager API on a page that has not navigated yet.

    The route stays inert until ``without_campaign_manager_writes`` latches it,
    so a later read finds it present and makes no protocol call.
    """
    await _guard_for(page).install(page)


@asynccontextmanager
async def without_campaign_manager_writes(page: Any) -> AsyncIterator[None]:
    """Latch write protection for a Campaign Manager read."""
    state = _guard_for(page)
    # Stays latched for late requests: the app can write after the read returns.
    state.read_mode = True
    await state.install(page)
    yield


@asynccontextmanager
async def explicit_campaign_write(
    page: Any, *, admit: Admission
) -> AsyncIterator[WriteWindow]:
    """Let through only the app's non-read requests that ``admit`` accepts.

    ``admit`` returns None to admit a request or the reason to abort it; one
    that raises aborts. Every other non-read request is aborted, and the page
    is latched again on every exit.
    """
    state = _guard_for(page)
    if state.admission is not None:
        raise LinkedInScraperException(
            "A Campaign Manager change is already in progress on this page."
        )
    await state.install(page)
    window = WriteWindow()
    state.admission = (admit, window)
    try:
        yield window
    finally:
        state.admission = None
        state.read_mode = True
