"""Page-scoped routing that lets an account-list write send only its plan.

While a list-write tool holds the page, every non-GET request to the Sales
Navigator API is aborted, the app's own included, except inside
``explicit_list_write``, which admits exactly one request: the planned method
on the planned resource, entity and action with the planned JSON body. A second
request, a different list, a different set of accounts or a request the app
sends on its own never leaves the browser.

The one exception outside a window is the boot record every Sales Navigator
page load posts (``salesApiWebAppBoot?action=recordBoot``): the reads let it
through on every load, and a page that never records its boot looks less like
the member, not more. Inside a window even that is aborted.

Outside the tools the route falls through untouched, so the read tools behave
as before. It is registered at page creation like the other guards, so no
route change happens mid-session.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from weakref import WeakKeyDictionary

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.scraping.sales_navigator_payload import parse_sn_call

_ROUTE = "**/sales-api/**"
# See ``messaging_read_state`` for why a route registered mid-session is bounded.
_REGISTRATION_TIMEOUT_SECONDS = 15.0
_BOOT = ("salesApiWebAppBoot", "recordBoot")


@dataclass(frozen=True, slots=True)
class PlannedListWrite:
    """The one request a write window lets through."""

    method: str
    resource: str
    entity_id: str | None
    action: str | None
    # Compared decoded, so key order and whitespace do not matter.
    body: Any


@dataclass(slots=True)
class ListWriteWindow:
    planned: PlannedListWrite
    admitted: int = 0
    refused: list[str] = field(default_factory=list)


def _json_body(request: Any) -> Any:
    try:
        text = request.post_data
        return json.loads(text) if text else None
    except (TypeError, ValueError):
        return None


def _effective_method(method: str, headers: dict[str, str]) -> str:
    """The method a request performs, a tunneled one included."""
    override = headers.get("x-http-method-override", "").strip().upper()
    return override or method


def admission_refusal(
    window: ListWriteWindow, method: str, url: str, body: Any
) -> str | None:
    """Why a request must not leave, or None when it is the planned one."""
    if window.admitted:
        return "a write was already sent"
    planned = window.planned
    call = parse_sn_call(url, method)
    if call is None:
        return "not a Sales Navigator API request"
    if method != planned.method:
        return f"method {method} is not the planned {planned.method}"
    if call.resource != planned.resource:
        return "not the planned resource"
    if call.entity_id != planned.entity_id:
        return "not the planned list"
    if call.params.get("action") != planned.action:
        return "not the planned action"
    if body != planned.body:
        return "body differs from the plan"
    return None


def _is_boot(url: str) -> bool:
    call = parse_sn_call(url, "POST")
    return call is not None and (call.resource, call.params.get("action")) == _BOOT


class _PageGuard:
    def __init__(self) -> None:
        self.holders = 0
        self.window: ListWriteWindow | None = None
        self.refused_outside: list[str] = []
        self.registration: asyncio.Task[None] | None = None
        self.registration_failed = False
        self.route_handler = self.handle

    async def handle(self, route: Any) -> None:
        request = route.request
        if request.method.upper() == "GET" or not self.holders:
            await route.fallback()
            return
        # The tunnel header can be absent from ``request.headers`` while it is
        # provisional, so read the full set.
        headers = {
            key.casefold(): value
            for key, value in (await request.all_headers()).items()
        }
        method = _effective_method(request.method.upper(), headers)
        if method == "GET":
            await route.fallback()
            return
        window = self.window
        if window is None:
            if method == "POST" and _is_boot(request.url):
                await route.fallback()
                return
            self.refused_outside.append(method)
            await route.abort()
            return
        try:
            reason = admission_refusal(window, method, request.url, _json_body(request))
        except Exception:
            reason = "admission error"
        # The window can close while the headers are read.
        if reason is None and self.window is window:
            # Counted before it leaves, so a failed send still counts as sent.
            window.admitted += 1
            await route.fallback()
            return
        window.refused.append(reason or "window closed")
        await route.abort()

    async def install(self, page: Any) -> None:
        if self.registration_failed:
            raise LinkedInScraperException(
                "Sales Navigator list protection registration failed, so "
                "nothing was changed. Call close_session to restart the "
                "browser, then retry."
            )
        if self.registration is None:
            self.registration = asyncio.create_task(
                page.route(_ROUTE, self.route_handler)
            )
            self.registration.add_done_callback(self._registration_finished)
        try:
            async with asyncio.timeout(_REGISTRATION_TIMEOUT_SECONDS):
                await asyncio.shield(self.registration)
        except TimeoutError:
            raise LinkedInScraperException(
                "Sales Navigator list protection did not register within "
                f"{_REGISTRATION_TIMEOUT_SECONDS:g}s, so nothing was changed. "
                "Call close_session to restart the browser, then retry."
            ) from None

    def _registration_finished(self, task: asyncio.Task[None]) -> None:
        if task.cancelled() or task.exception() is not None:
            self.registration_failed = True
            self.registration = None


_GUARDS: WeakKeyDictionary[Any, _PageGuard] = WeakKeyDictionary()


def _guard_for(page: Any) -> _PageGuard:
    state = _GUARDS.get(page)
    if state is None:
        state = _PageGuard()
        _GUARDS[page] = state
    return state


async def register_sales_list_routes(page: Any) -> None:
    """Route the Sales Navigator API on a page that has not navigated yet."""
    await _guard_for(page).install(page)


@asynccontextmanager
async def without_sales_list_writes(page: Any) -> AsyncIterator[None]:
    """Abort every Sales Navigator write the page sends while the block runs."""
    state = _guard_for(page)
    await state.install(page)
    state.holders += 1
    try:
        yield
    finally:
        state.holders -= 1


@asynccontextmanager
async def explicit_list_write(
    page: Any, planned: PlannedListWrite
) -> AsyncIterator[ListWriteWindow]:
    """Admit the one request ``planned`` describes, once.

    Only inside ``without_sales_list_writes``, so every other write stays
    aborted before and after the window.
    """
    state = _guard_for(page)
    if not state.holders:
        raise LinkedInScraperException(
            "A Sales Navigator list write was attempted outside its protection."
        )
    if state.window is not None:
        raise LinkedInScraperException(
            "A Sales Navigator list write is already in progress on this page."
        )
    window = ListWriteWindow(planned=planned)
    state.window = window
    try:
        yield window
    finally:
        state.window = None
