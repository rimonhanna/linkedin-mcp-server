"""Page-scoped routing that lets an account-list write send only its plan.

While a list-write tool holds the page, every non-GET request to the Sales
Navigator API is aborted, the app's own included, except inside
``explicit_list_write``, which admits exactly one request: the planned method
on the planned resource and entity, with exactly the planned query and JSON
body (and no body at all when the plan has none). A second request, a
different list, a different set of accounts or any other request the app
sends never leaves the browser. A window opened around the app's own form
admits the app's request when, and only when, it is exactly the plan.

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
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse
from weakref import WeakKeyDictionary

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.scraping.sales_navigator_payload import (
    SN_API_PREFIX,
    parse_sn_call,
)

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
    # The whole query, in the order it is sent; compared as a set of pairs.
    query: tuple[tuple[str, str], ...]
    # Compared decoded, so key order and whitespace do not matter.
    body: Any
    # For a body the app composes with parts the plan cannot know (a
    # generated name, the seat, display labels): the decoded body is admitted
    # when this names no difference from the plan, in place of the equality
    # check. The difference it names is the refusal's reason.
    body_mismatch: Callable[[Any], str | None] | None = field(
        default=None, compare=False
    )

    @property
    def path(self) -> str:
        path = SN_API_PREFIX + self.resource
        if self.entity_id is not None:
            path += f"/{self.entity_id}"
        if self.query:
            path += "?" + "&".join(f"{key}={value}" for key, value in self.query)
        return path


@dataclass(slots=True)
class ListWriteWindow:
    planned: PlannedListWrite
    admitted: int = 0
    refused: list[str] = field(default_factory=list)
    # The admitted request, for its answer.
    request: Any = None
    # Set once a request was admitted or refused, for a write the app sends.
    decided: asyncio.Event = field(default_factory=asyncio.Event)


def _body_text(request: Any) -> str | None:
    try:
        text = request.post_data
    except (TypeError, ValueError):
        return None
    return text if isinstance(text, str) else None


def _effective_method(method: str, headers: dict[str, str]) -> str:
    """The method a request performs, a tunneled one included."""
    override = headers.get("x-http-method-override", "").strip().upper()
    return override or method


def admission_refusal(
    window: ListWriteWindow, method: str, url: str, text: str | None
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
    sent = sorted(tuple(pair.partition("=")[::2]) for pair in _query(url))
    if sent != sorted(planned.query):
        return "query differs from the plan"
    if planned.body is None:
        # Compared raw: an unreadable body would decode to None and match.
        if text:
            return "body differs from the plan"
        return None
    try:
        body = json.loads(text) if text else None
    except ValueError:
        return "body differs from the plan"
    if planned.body_mismatch is not None:
        why = planned.body_mismatch(body)
        return None if why is None else f"body differs from the plan: {why}"
    if body != planned.body:
        return "body differs from the plan"
    return None


def _query(url: str) -> list[str]:
    return [pair for pair in urlparse(url).query.split("&") if pair]


def _for_planned_resource(planned: PlannedListWrite, method: str, url: str) -> bool:
    call = parse_sn_call(url, method)
    return call is not None and call.resource == planned.resource


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
        # Judged by the window open when it arrived: one that closes while
        # the headers are read admits nothing, and a later one never sees it.
        window = self.window
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
        if window is None:
            if method == "POST" and _is_boot(request.url):
                await route.fallback()
                return
            self.refused_outside.append(method)
            await route.abort()
            return
        try:
            reason = admission_refusal(window, method, request.url, _body_text(request))
        except Exception:
            reason = "admission error"
        if reason is None and self.window is window:
            # Counted before it leaves, so a failed send still counts as sent.
            window.admitted += 1
            window.request = request
            window.decided.set()
            await route.fallback()
            return
        window.refused.append(reason or "window closed")
        # Only a refusal of the planned resource answers the wait for the
        # app's request; a stray write (a tracking call) leaves it waiting.
        if _for_planned_resource(window.planned, method, request.url):
            window.decided.set()
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
