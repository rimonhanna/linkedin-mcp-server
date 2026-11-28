"""List the member's connections newest first, back to a date.

One page load of ``/mynetwork/invite-connect/connections/`` (its default sort
is "Recently added"), then the list's own infinite scroll: each further ten
rows are one ``pagination`` request the page sends itself, and each is charged
to the account budget like a page load before it is asked for
(``navigation_kind`` files the path as ``other``). Nothing is clicked and no
request is composed here.

Paging stops at the first of: a row connected before ``since`` (the list is
newest first), ``limit`` rows, the end of the list, the deadline, a pacing cap
or account cooldown, a rate limit, or a date this module cannot read. A
challenge or sign-in page raises, like every other read.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlparse

from linkedin_mcp_server.core.exceptions import (
    LinkedInScraperException,
    RateLimitError,
)
from linkedin_mcp_server.exceptions import ActionLimitError
from linkedin_mcp_server.pacing import (
    JobStore,
    cooldown_resume_at,
    note_throttle_signal,
)
from linkedin_mcp_server.scraping.contracts import pacing_stop
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.recent_connections_parse import (
    CONNECTIONS_PATH,
    CONNECTIONS_URL,
    PAGINATION_PATH,
    ROWS_JS,
    Connection,
    connection_from_row,
    connection_line,
)
from linkedin_mcp_server.scraping.session import ScrapingSession

logger = logging.getLogger(__name__)

MAX_CONNECTIONS = 500
# Rows are on the first poll after a load (measured); a slow one gets this.
_READY_TIMEOUT = 12.0
# One pagination answer landed in about two seconds of wheel scrolling.
_PAGE_TIMEOUT = 10.0
_POLL_SECONDS = 0.5
# About ten rows' height, so one pass asks for about one page.
_WHEEL_PIXELS = 600.0
_ROWS_PER_PAGE = 10
# A pass with no new rows and no answer from LinkedIn is retried this often
# before the page is called not loaded.
_EMPTY_PASSES = 2


class _Pagination:
    """The page's own pagination answers, by status."""

    def __init__(self) -> None:
        self.statuses: list[int] = []

    def __call__(self, response: Any) -> None:
        try:
            path = urlparse(response.url).path
            if path == PAGINATION_PATH:
                self.statuses.append(int(response.status))
        except Exception:
            logger.debug("Could not read a pagination response", exc_info=True)


class RecentConnectionsReader:
    """Read the connections page, newest first, back to a date."""

    def __init__(self, session: ScrapingSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    @property
    def _page(self) -> Any:
        return self._session.page

    async def _rows(self) -> list[Connection]:
        raw = await self._page.evaluate(ROWS_JS)
        rows = [connection_from_row(item) for item in raw or []]
        return [row for row in rows if row is not None]

    async def _wait_for_rows(self, have: int, timeout: float) -> list[Connection]:
        end = time.monotonic() + timeout
        rows = await self._rows()
        while len(rows) <= have and time.monotonic() < end:
            await self._session.delay(_POLL_SECONDS)
            rows = await self._rows()
        return rows

    async def _scroll(self) -> None:
        """Wheel the list about one page down, from near the middle of the view."""
        size = getattr(self._page, "viewport_size", None) or {}
        width, height = size.get("width") or 1280, size.get("height") or 720
        jittered = self._session.jittered
        await self._page.mouse.move(
            int(width // 2 + jittered(40.0) - 40),
            int(height // 2 + jittered(40.0) - 40),
        )
        for _ in range(2):
            await self._page.mouse.wheel(0, int(jittered(_WHEEL_PIXELS)))
            await self._session.pace(0.6)

    def _charge_page(self) -> str | None:
        """Charge one more page of rows; why not, when the budget refuses it."""
        try:
            resume_at = cooldown_resume_at(JobStore(), datetime.now(timezone.utc))
        except Exception:
            logger.debug("Could not read the account cooldown", exc_info=True)
            resume_at = None
        if resume_at is not None:
            return f"account_cooldown until {resume_at.isoformat(timespec='seconds')}"
        try:
            self._navigator._charge_navigation(CONNECTIONS_URL)
        except ActionLimitError as exc:
            return pacing_stop(exc)
        return None

    async def list_recent(
        self,
        since: date,
        *,
        limit: int = 100,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Connections made on or after ``since``, newest first, up to ``limit``."""
        limit = max(1, min(limit, MAX_CONNECTIONS))
        pagination = _Pagination()
        page = self._page
        errors: dict[str, Any] = {}
        pages = 1
        page.on("response", pagination)
        try:
            await self._navigator._navigate_to_page(CONNECTIONS_URL)
            await self._session.check_rate_limit()
            landed = urlparse(page.url).path
            if not landed.startswith(CONNECTIONS_PATH):
                raise LinkedInScraperException(
                    f"LinkedIn showed {landed} instead of the connections page, "
                    "so nothing was read."
                )
            rows = await self._wait_for_rows(0, _READY_TIMEOUT)
            empty_passes = 0
            while True:
                reason = _stop(rows, since, limit)
                if reason is not None:
                    break
                if (
                    deadline is not None
                    and time.monotonic() + _PAGE_TIMEOUT >= deadline
                ):
                    reason = "deadline"
                    break
                if not rows:
                    reason = "complete"
                    break
                refused = self._charge_page()
                if refused is not None:
                    reason = refused
                    break
                pages += 1
                answered = len(pagination.statuses)
                # A person reads a page of rows before asking for the next.
                await self._session.pace(1.4)
                await self._scroll()
                more = await self._wait_for_rows(len(rows), _PAGE_TIMEOUT)
                fresh = pagination.statuses[answered:]
                if 429 in fresh:
                    note_throttle_signal("http_429")
                    reason = "rate_limited"
                    errors["connections"] = {
                        "error_type": "rate_limit",
                        "error_message": "LinkedIn answered a page of connections "
                        "with HTTP 429 (too many requests); paging stopped. Wait "
                        "before calling again.",
                    }
                    break
                try:
                    await self._session.check_rate_limit()
                except RateLimitError as exc:
                    reason = "rate_limited"
                    errors["connections"] = {
                        "error_type": "rate_limit",
                        "error_message": str(exc),
                    }
                    break
                if len(more) > len(rows):
                    # One pass can land more than one page; each is a page load.
                    landed = -(-(len(more) - len(rows)) // _ROWS_PER_PAGE)
                    rows, empty_passes = more, 0
                    for _ in range(landed - 1):
                        try:
                            self._navigator._charge_navigation(CONNECTIONS_URL)
                        except ActionLimitError as exc:
                            reason = pacing_stop(exc)
                            break
                        pages += 1
                    if reason is not None:
                        break
                    continue
                if fresh and all(200 <= status < 300 for status in fresh):
                    # LinkedIn answered and added no row: the end of the list.
                    reason = "complete"
                    break
                empty_passes += 1
                if empty_passes >= _EMPTY_PASSES:
                    reason = "page_not_loaded"
                    break
        finally:
            page.remove_listener("response", pagination)
        return _result(rows, since, limit, reason, pages, errors)


def _stop(rows: list[Connection], since: date, limit: int) -> str | None:
    """Why paging is done after ``rows``; None to read the next page."""
    if not rows:
        return None
    last = rows[-1]["connectedOn"]
    if last is None:
        return "date_unreadable"
    if last < since.isoformat():
        return "since"
    if len(rows) >= limit:
        return "limit"
    return None


def _result(
    rows: list[Connection],
    since: date,
    limit: int,
    reason: str,
    pages: int,
    errors: dict[str, Any],
) -> dict[str, Any]:
    floor = since.isoformat()
    # Newest first, so the rows on or after ``since`` are a prefix; a row whose
    # date could not be read is kept, since it may be one of them.
    kept = [
        row for row in rows if row["connectedOn"] is None or row["connectedOn"] >= floor
    ]
    more = reason not in ("since", "complete") or len(kept) > limit
    kept = kept[:limit]
    result: dict[str, Any] = {
        "url": CONNECTIONS_URL,
        "since": floor,
        "sections": {"connections": "\n".join(connection_line(c) for c in kept)},
        "connections": kept,
        "paging": {
            "count": len(kept),
            "pages": pages,
            "more": more,
            "reason": reason,
        },
    }
    unreadable = sum(1 for row in kept if row["connectedOn"] is None)
    if unreadable:
        errors["connected_on"] = {
            "error_type": "date_unreadable",
            "error_message": f"{unreadable} of {len(kept)} connections show a "
            "connected date this tool cannot read (it reads en-US "
            '"Connected on <Month> <D>, <YYYY>" only); their connectedOn is '
            "null and connectedText holds what LinkedIn showed.",
        }
    if not rows and reason == "complete":
        errors.setdefault(
            "connections",
            {
                "error_type": "no_rows",
                "error_message": "The connections page showed no connection rows.",
            },
        )
    if errors:
        result["section_errors"] = errors
    return result
