"""Page-scoped routing that lets the invite dialog send only the planned batch.

Every non-GET request to ``voyagerRelationshipsDashInvitations`` is aborted
while a page-invite tool holds the page. Inside ``explicit_page_invite`` every
non-GET Voyager request is aborted, whatever its endpoint, except one Rest.li
``batch_create`` to that endpoint whose elements invite exactly the planned
profiles to follow exactly this page: the send endpoint is read from the app's
bundle, not measured, so an invitation the dialog sends anywhere else is
stopped too. Any other invitation the dialog might send (a profile ticked by
accident, a second click, another invitation type) never leaves the browser.

Outside those calls the route falls through untouched: ``connect_with_person``
reaches the same family of endpoints and is not this guard's business. The
route is registered at page creation, like the read-state and Campaign Manager
routes, so no route change happens mid-session.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse
from weakref import WeakKeyDictionary

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.scraping.page_invite_payload import (
    INVITATIONS,
    VOYAGER_API,
    invitation_batch,
    inviter_allowed,
)

_ROUTE = f"**{VOYAGER_API}**"
_INVITATIONS_PATH = f"{VOYAGER_API}{INVITATIONS}"
# See ``messaging_read_state`` for why a route registered mid-session is bounded.
_REGISTRATION_TIMEOUT_SECONDS = 15.0


@dataclass(slots=True)
class InviteWindow:
    company_id: str
    planned: tuple[str, ...]
    # Profile ids of each batch let through, in the order the app sent them.
    admitted: list[list[str]] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)


def _json_body(request: Any) -> Any:
    try:
        text = request.post_data
        return json.loads(text) if text else None
    except (TypeError, ValueError):
        return None


def _is_invitations(url: str) -> bool:
    return urlparse(url).path.startswith(_INVITATIONS_PATH)


def admission_refusal(
    window: InviteWindow, method: str, restli_method: str | None, url: str, body: Any
) -> str | None:
    """Why a request must not leave, or None when it is the planned batch."""
    if window.admitted:
        return "a batch was already sent"
    if not _is_invitations(url):
        return "not the invitations endpoint"
    if method != "POST" or (restli_method or "").casefold() != "batch_create":
        return "not a batch create"
    if not inviter_allowed(url, window.company_id):
        return "inviter is not this page"
    invited = invitation_batch(body)
    if invited is None:
        return "not an invitation to follow a page"
    if len(invited) != len(window.planned) or set(invited) != set(window.planned):
        return "invitees differ from the plan"
    return None


class _PageGuard:
    def __init__(self) -> None:
        self.holders = 0
        self.window: InviteWindow | None = None
        self.refused_outside: list[str] = []
        self.registration: asyncio.Task[None] | None = None
        self.registration_failed = False
        self.route_handler = self.handle

    async def handle(self, route: Any) -> None:
        request = route.request
        method = request.method.upper()
        if method == "GET" or not self.holders:
            await route.fallback()
            return
        window = self.window
        if window is None:
            if not _is_invitations(request.url):
                await route.fallback()
                return
            self.refused_outside.append(method)
            await route.abort()
            return
        headers = {
            key.casefold(): value
            for key, value in (await request.all_headers()).items()
        }
        try:
            reason = admission_refusal(
                window,
                method,
                headers.get("x-restli-method"),
                request.url,
                _json_body(request),
            )
        except Exception:
            reason = "admission error"
        # The window can close while the headers are read.
        if reason is None and self.window is window:
            # Recorded before it leaves, so a failed send still counts as sent.
            window.admitted.append(list(window.planned))
            await route.fallback()
            return
        window.refused.append(reason or "window closed")
        await route.abort()

    async def install(self, page: Any) -> None:
        if self.registration_failed:
            raise LinkedInScraperException(
                "Page invitation protection registration failed, so nothing was "
                "opened or sent. Call close_session to restart the browser, then "
                "retry."
            )
        if self.registration is None:
            self.registration = asyncio.create_task(page.route(_ROUTE, self.handle))
            self.registration.add_done_callback(self._registration_finished)
        try:
            async with asyncio.timeout(_REGISTRATION_TIMEOUT_SECONDS):
                await asyncio.shield(self.registration)
        except TimeoutError:
            raise LinkedInScraperException(
                "Page invitation protection did not register within "
                f"{_REGISTRATION_TIMEOUT_SECONDS:g}s, so nothing was opened or "
                "sent. Call close_session to restart the browser, then retry."
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


async def register_page_invite_routes(page: Any) -> None:
    """Route the Voyager API on a page that has not navigated yet."""
    await _guard_for(page).install(page)


@asynccontextmanager
async def without_page_invites(page: Any) -> AsyncIterator[None]:
    """Abort every invitation the page sends while the block runs."""
    state = _guard_for(page)
    await state.install(page)
    state.holders += 1
    try:
        yield
    finally:
        state.holders -= 1


@asynccontextmanager
async def explicit_page_invite(
    page: Any, *, company_id: str, planned: list[str]
) -> AsyncIterator[InviteWindow]:
    """Admit the one batch inviting exactly ``planned`` to follow ``company_id``.

    Only inside ``without_page_invites``, so every other invitation stays
    aborted before and after the window.
    """
    state = _guard_for(page)
    if not state.holders:
        raise LinkedInScraperException(
            "A page invitation was attempted outside its protection."
        )
    if state.window is not None:
        raise LinkedInScraperException(
            "A page invitation is already in progress on this page."
        )
    window = InviteWindow(company_id=company_id, planned=tuple(planned))
    state.window = window
    try:
        yield window
    finally:
        state.window = None
