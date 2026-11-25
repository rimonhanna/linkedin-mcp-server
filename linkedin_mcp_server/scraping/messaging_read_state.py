"""Page-scoped routing for passive messaging read-state protection."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs, urlparse
from weakref import WeakKeyDictionary, WeakSet

from linkedin_mcp_server.core.exceptions import LinkedInScraperException

# The Sales Navigator entries take every POST under its thread resource (mark
# read or unread and the archive patch among them) and its message actions
# (send); a thread id is a path segment, hence ``**``. Only these: the app's
# other POSTs (boot, coach, heartbeats, telemetry) pass, since aborting them
# protects nothing and makes the session look unlike a browser's.
_READ_STATE_WRITE_ROUTES = (
    "**/voyager/api/voyagerMessagingDashMessengerConversations*",
    "**/voyager/api/voyagerMessagingDashMessagingBadge*",
    "**/sales-api/salesApiMessagingThreads**",
    "**/sales-api/salesApiMessageActions**",
)
_READ_STATE_PATHS = (
    "/voyager/api/voyagerMessagingDashMessengerConversations",
    "/voyager/api/voyagerMessagingDashMessagingBadge",
    "/sales-api/salesApiMessagingThreads",
    "/sales-api/salesApiMessageActions",
)
# Patchright's ``page.route`` for a page's first route (and ``unroute`` of its
# last) re-sends ``Network.setCacheDisabled`` to every CDP session of the page
# (``CRNetworkManager._forEachSession``: main frame, out-of-process frames,
# workers) and waits for all of them with no deadline. So such a route change
# mid-session can hang once those sessions exist. Measured 2026-11-11: on a
# page that had just scrolled /feed/ it never answered, and get_inbox sat in it
# until the server was stopped 376 s later. Pages are therefore routed at
# creation, before their first navigation (``register_read_state_routes``);
# this bound covers a page that reaches a read without that.
_REGISTRATION_TIMEOUT_SECONDS = 15.0
# The one request a Sales Navigator send window lets through.
_SALES_SEND_PATH = "/sales-api/salesApiMessageActions"
_SALES_SEND_ACTION = "createMessage"


def _is_sales_send(request: Any) -> bool:
    parsed = urlparse(request.url)
    return parsed.path == _SALES_SEND_PATH and parse_qs(parsed.query).get("action") == [
        _SALES_SEND_ACTION
    ]


class _PageGuard:
    def __init__(self) -> None:
        self.depth = 0
        # Nothing is blocked until a passive read latches it.
        self.read_mode = False
        self.registered: set[str] = set()
        self.listening = False
        self.registration: asyncio.Task[None] | None = None
        self.registration_failed = False
        self.protected: WeakSet[Any] = WeakSet()
        # A Sales Navigator send window: one createMessage may pass while
        # read_mode stays on, and the request that took it is remembered.
        self.send_allowance = 0
        self.allowed: WeakSet[Any] = WeakSet()
        # Store stable callback identities; readers are recreated for each tool.
        self.request_listener = self.observe
        self.route_handler = self.handle

    def _takes_the_send(self, request: Any) -> bool:
        """Whether ``request`` is the one send the open window lets through.

        Asked from both callbacks, in either order, with the same answer.
        """
        if request in self.allowed:
            return True
        if request in self.protected or not self.send_allowance:
            return False
        if request.method.upper() != "POST" or not _is_sales_send(request):
            return False
        self.send_allowance = 0
        self.allowed.add(request)
        return True

    def observe(self, request: Any) -> None:
        if (
            self.read_mode
            and request.method.upper() == "POST"
            and urlparse(request.url).path.startswith(_READ_STATE_PATHS)
            and not self._takes_the_send(request)
        ):
            self.protected.add(request)

    async def handle(self, route: Any) -> None:
        request = route.request
        # A request notification can precede its asynchronous route callback.
        # Remember the notification's read scope even if that scope has exited.
        if (
            request.method.upper() == "POST"
            and (self.read_mode or request in self.protected)
            and not self._takes_the_send(request)
        ):
            await route.abort()
        else:
            await route.fallback()

    async def install(self, page: Any) -> None:
        if self.registration_failed:
            raise LinkedInScraperException("Read-state protection registration failed.")
        if self.registration is None:
            self.registration = asyncio.create_task(self._install(page))
            self.registration.add_done_callback(self._registration_finished)
        # A cancelled or timed-out caller must not leave a half-registered
        # route that a later caller registers again, so the task outlives it.
        # The registration itself makes no reads.
        try:
            async with asyncio.timeout(_REGISTRATION_TIMEOUT_SECONDS):
                await asyncio.shield(self.registration)
        except TimeoutError:
            # Reading unguarded would let LinkedIn mark the threads read.
            raise LinkedInScraperException(
                "Read-state protection did not register within "
                f"{_REGISTRATION_TIMEOUT_SECONDS:g}s, so messaging was not "
                "opened and nothing was marked read. Call close_session to "
                "restart the browser, then retry."
            ) from None

    def _registration_finished(self, task: asyncio.Task[None]) -> None:
        if task.cancelled() or task.exception() is not None:
            self.registration_failed = True
            # A failed task's traceback retains the page through this weak-map value.
            self.registration = None

    async def _install(self, page: Any) -> None:
        if not self.listening:
            page.on("request", self.request_listener)
            self.listening = True
        for pattern in _READ_STATE_WRITE_ROUTES:
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


async def register_read_state_routes(page: Any) -> None:
    """Route the mark-read endpoints on a page that has not navigated yet.

    The routes stay inert until ``without_read_state_writes`` latches them, so
    a later passive read finds them present and makes no protocol call.
    """
    await _guard_for(page).install(page)


@asynccontextmanager
async def without_read_state_writes(page: Any) -> AsyncIterator[None]:
    """Activate read protection without reconfiguring interception at exit."""
    state = _guard_for(page)
    state.depth += 1
    state.read_mode = True
    try:
        await state.install(page)
        yield
    finally:
        state.depth -= 1
        # Keep protection latched for late request events. Only a verified
        # compose operation can release it; no interception update is needed.


def allow_explicit_message_write(page: Any) -> None:
    """Release the read latch only when the existing writer has verified its target."""
    state = _GUARDS.get(page)
    if state is None:
        return
    if state.depth:
        raise LinkedInScraperException(
            "Cannot compose while a passive messaging read is active."
        )
    state.read_mode = False


def open_one_sales_send(page: Any) -> None:
    """Let exactly one Sales Navigator ``createMessage`` POST leave the page.

    Unlike ``allow_explicit_message_write`` the latch stays on: a mark-read,
    an archive or any later ``createMessage`` is still aborted. Close the
    window with ``close_sales_send`` whether or not the send happened.
    """
    state = _guard_for(page)
    if state.depth:
        raise LinkedInScraperException(
            "Cannot compose while a passive messaging read is active."
        )
    state.read_mode = True
    state.send_allowance = 1


def allowed_sales_send(page: Any, requests: list[Any]) -> Any | None:
    """The request among ``requests`` that the send window let through."""
    state = _GUARDS.get(page)
    if state is None:
        return None
    return next((request for request in requests if request in state.allowed), None)


def close_sales_send(page: Any) -> None:
    """End a send window: nothing more passes, and the latch is on."""
    state = _GUARDS.get(page)
    if state is None:
        return
    state.send_allowance = 0
    state.read_mode = True
