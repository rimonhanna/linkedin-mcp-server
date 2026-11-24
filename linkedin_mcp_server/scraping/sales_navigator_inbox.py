"""Sales Navigator inbox reads: the conversation list, one conversation, search.

Every read loads a Sales Navigator inbox URL and reads the responses the page
itself fetches. Any inbox view opens its top conversation on its own, and the
app marks a conversation read once its latest message is on screen, so every
read runs with the read-state guard on: the app's POSTs under its thread
resource (mark read, seen receipts) and its message actions are aborted. The
open conversation's participant profile is still fetched, as the app always
does. Older messages are the one request of this module's own: the page's
older-messages call, replayed with the page's own headers.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, unquote, urlparse

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.core.humanize import human_type
from linkedin_mcp_server.pacing import note_throttle_signal
from linkedin_mcp_server.scraping.identifiers import (
    SALES_INBOX_URL,
    normalize_sales_lead_id,
    normalize_sales_thread_id,
    sales_inbox_thread_url,
    sales_inbox_url,
)
from linkedin_mcp_server.scraping.messaging_read_state import without_read_state_writes
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.sales_navigator import (
    SalesNavigatorReader,
    _Refusal,
)
from linkedin_mcp_server.scraping.sales_navigator_api import (
    SalesNavigatorCapture,
    SnResponse,
)
from linkedin_mcp_server.scraping.sales_navigator_inbox_payload import (
    FILTERS,
    LIST_FINDER,
    MESSAGE_BATCH,
    MESSAGE_DECORATION,
    SEARCH_FINDER,
    THREADS,
    InboxConversation,
    InboxFilter,
    InboxMessage,
    SalesConversation,
    build_conversation_rows,
    build_older_messages,
    build_thread,
    render_conversation,
    render_conversations,
    strip_delivered_at,
)
from linkedin_mcp_server.scraping.sales_navigator_payload import parse_sn_call
from linkedin_mcp_server.scraping.session import ScrapingSession

logger = logging.getLogger(__name__)

_INBOX_PATH = urlparse(SALES_INBOX_URL).path
_MAX_LIMIT = 1000
# How far down the inbox a lead's conversation is looked for (ten scrolls).
_LEAD_SCAN_LIMIT = 200
_MAX_SCROLLS = 60
_SCROLL_PAUSE = 1.2
# How long one scroll waits for the next list page before it counts as stalled.
_PAGE_WAIT = 6.0
_SEARCH_WAIT = 12.0
_MAX_OLDER_BATCHES = 3
_MARK_READ = "markAsRead"
_FORWARDED_HEADERS = frozenset(
    {
        "accept",
        "csrf-token",
        "x-li-identity",
        "x-li-lang",
        "x-li-page-instance",
        "x-li-track",
        "x-restli-protocol-version",
    }
)
# The inbox search box. The id is the app's own name for it; the structural
# alternative is a search input inside <main> (the header's global search is a
# combobox outside it and searches people, so it is never typed into).
_SEARCH_BOX_SELECTORS = ("#left-rail-inbox-search", 'main input[type="search"]')

# The conversation list scrolls inside its own container, and only a scroll
# there makes the app load the next page. No URL pages it, so the container is
# found by structure: the nearest scrollable ancestor of a conversation link.
_SCROLL_LIST = """() => {
    // linkedin-mcp:sn-inbox-scroll
    const links = [...document.querySelectorAll('main a[href*="/sales/inbox/"]')];
    const scrollable = (element) => {
        const style = window.getComputedStyle(element);
        return (
            (style.overflowY === 'auto' || style.overflowY === 'scroll') &&
            element.scrollHeight > element.clientHeight + 20
        );
    };
    for (let i = links.length - 1; i >= 0; i -= 1) {
        for (let el = links[i].parentElement; el && el !== document.body; el = el.parentElement) {
            if (scrollable(el)) {
                el.scrollTop = el.scrollHeight;
                return true;
            }
        }
    }
    return false;
}"""

_FETCH_JSON = """async ({ url, headers }) => {
    // linkedin-mcp:sn-inbox-history
    try {
        const response = await fetch(url, {
            method: 'GET',
            credentials: 'include',
            headers,
        });
        let payload = null;
        try { payload = await response.json(); } catch (_) {}
        return { status: response.status, payload };
    } catch (_) {
        return { status: 0, payload: null };
    }
}"""


def _error(error_type: str, message: str) -> dict[str, str]:
    return {"error_type": error_type, "error_message": message}


def _unverified(what: str) -> LinkedInScraperException:
    return LinkedInScraperException(
        f"Sales Navigator returned {what} in a shape this tool does not "
        "recognise, so nothing was reported."
    )


def _is_list(finder: str, key: str, value: str) -> Callable[[SnResponse], bool]:
    def match(response: SnResponse) -> bool:
        call = response.call
        return (
            response.ok
            and call.method == "GET"
            and call.resource == THREADS
            and call.entity_id is None
            and call.finder == finder
            and " ".join(unquote(call.params.get(key, "")).split()) == value
        )

    return match


def _is_thread(thread_id: str) -> Callable[[SnResponse], bool]:
    def match(response: SnResponse) -> bool:
        call = response.call
        return (
            response.ok
            and call.method == "GET"
            and call.resource == THREADS
            and call.sub_path is None
            and call.entity_id is not None
            and unquote(call.entity_id) == thread_id
        )

    return match


def _marked_read(responses: list[SnResponse]) -> list[str]:
    """Threads LinkedIn answered a mark-read for: the request got through."""
    return [
        unquote(response.call.entity_id or "")
        for response in responses
        if response.ok
        and response.call.method == "POST"
        and response.call.resource == THREADS
        and response.call.params.get("action") == _MARK_READ
    ]


def _exhausted(responses: list[SnResponse]) -> bool:
    """The last list page came back short of what it asked for."""
    if not responses:
        return False
    last = responses[-1]
    asked = last.call.params.get("count", "")
    rows = build_conversation_rows(last.payload)
    return asked.isdigit() and rows is not None and len(rows) < int(asked)


def _stop_error(read: int, why: str) -> dict[str, str]:
    return _error(
        "page_limit",
        f"{read} conversations were read; {why}, so the rest were not read.",
    )


_STOP_REASONS = {
    "time_budget": "this call ran out of time",
    "no_new_rows": "Sales Navigator loaded no more conversations after two scrolls",
    "not_scrollable": "the conversation list could not be scrolled",
    "max_scrolls": f"the read stopped after {_MAX_SCROLLS} scrolls",
}


class _InboxCapture(SalesNavigatorCapture):
    """Also keep the headers of the page's own thread request, for replay."""

    def __init__(self, page: Any):
        super().__init__(page, resources=frozenset({THREADS}))
        self.thread_headers: dict[str, str] | None = None
        self._headers_started = False

    def _extra_listeners(self) -> tuple[tuple[str, Callable[..., None]], ...]:
        return (("request", self._on_request),)

    def _on_request(self, request: Any) -> None:
        call = parse_sn_call(request.url, request.method)
        if (
            self._headers_started
            or call is None
            or call.method != "GET"
            or call.resource != THREADS
            or call.entity_id is None
            or call.sub_path is not None
        ):
            return
        self._headers_started = True

        async def read() -> None:
            try:
                raw = await request.all_headers()
            except Exception:
                self._headers_started = False
                return
            self.thread_headers = {
                key: value
                for key, value in raw.items()
                if key.casefold() in _FORWARDED_HEADERS
            }

        self._spawn(read())


class SalesNavigatorInboxReader:
    """Own every workflow whose subject is the Sales Navigator inbox."""

    def __init__(
        self,
        session: ScrapingSession,
        navigator: PageNavigator,
        sales_navigator: SalesNavigatorReader,
    ):
        self._session = session
        self._navigator = navigator
        # Its page opening is the shared seat and landing check.
        self._sales_navigator = sales_navigator

    def _guard(self):
        return without_read_state_writes(self._session.page)

    async def _open(
        self,
        capture: SalesNavigatorCapture,
        url: str,
        ready: Callable[[SnResponse], bool],
        refused: _Refusal | None = None,
    ) -> None:
        await self._sales_navigator._open(
            capture, url, expected_path=_INBOX_PATH, ready=ready, refused=refused
        )

    @staticmethod
    def _rows(
        capture: SalesNavigatorCapture, is_page: Callable[[SnResponse], bool]
    ) -> list[InboxConversation]:
        rows: list[InboxConversation] = []
        seen: set[str] = set()
        for response in capture.matching(is_page):
            built = build_conversation_rows(response.payload)
            if built is None:
                raise _unverified("the inbox")
            for row in built:
                if row["id"] not in seen:
                    seen.add(row["id"])
                    rows.append(row)
        return rows

    async def _load(
        self,
        capture: SalesNavigatorCapture,
        is_page: Callable[[SnResponse], bool],
        limit: int,
        deadline: float | None,
        until: Callable[[list[InboxConversation]], bool] | None = None,
    ) -> tuple[list[InboxConversation], str]:
        """Scroll the list until ``limit`` rows (or ``until``); why it stopped.

        Every scroll is the page loading its own next page. Before each one the
        read stops if the slowest round so far would pass ``deadline``.
        """
        rows = self._rows(capture, is_page)
        stalled = 0
        slowest = 0.0
        for _ in range(_MAX_SCROLLS):
            if until is not None and until(rows):
                return rows, "found"
            if len(rows) >= limit:
                return rows[:limit], "limit"
            if _exhausted(capture.matching(is_page)):
                return rows, "complete"
            if stalled >= 2:
                return rows, "no_new_rows"
            if deadline is not None and time.monotonic() + slowest > deadline:
                return rows, "time_budget"
            began = time.monotonic()
            seen = len(capture.responses)
            if not await self._session.page.evaluate(_SCROLL_LIST):
                return rows, "not_scrollable"
            await self._session.delay(self._session.jittered(_SCROLL_PAUSE))
            try:
                await capture.wait_for(
                    is_page, after=seen, timeout=_PAGE_WAIT, throttle=False
                )
            except LinkedInScraperException:
                pass
            await capture.settle()
            await self._session.check_rate_limit()
            grown = self._rows(capture, is_page)
            stalled = 0 if len(grown) > len(rows) else stalled + 1
            rows = grown
            slowest = max(slowest, time.monotonic() - began)
        if until is not None and until(rows):
            return rows, "found"
        return rows[:limit], "max_scrolls"

    @staticmethod
    def _list_result(
        url: str,
        name: str,
        rows: list[InboxConversation],
        reason: str,
        capture: SalesNavigatorCapture,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"url": url, "sections": {}}
        if text := render_conversations(rows):
            result["sections"][name] = text
        result["conversations"] = rows
        result["paging"] = {
            "count": len(rows),
            "more": reason != "complete",
            "reason": reason,
        }
        if reason in _STOP_REASONS:
            result["section_errors"] = {
                name: _stop_error(len(rows), _STOP_REASONS[reason])
            }
        result["marked_read"] = bool(_marked_read(capture.responses))
        return result

    async def get_inbox(
        self,
        limit: int = 20,
        filter: InboxFilter = "all",
        *,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """The inbox's most recent conversations in one view, up to ``limit``."""
        if not 1 <= limit <= _MAX_LIMIT:
            raise LinkedInScraperException(
                f"limit must be between 1 and {_MAX_LIMIT} (got {limit})."
            )
        url_filter, api_filter = FILTERS[filter]
        url = sales_inbox_url(url_filter)
        is_page = _is_list(LIST_FINDER, "filter", api_filter)
        async with self._guard():
            async with _InboxCapture(self._session.page) as capture:
                await self._open(capture, url, is_page)
                rows, reason = await self._load(capture, is_page, limit, deadline)
        return self._list_result(url, "inbox", rows, reason, capture)

    async def _thread_for_lead(
        self, lead_id: str, index: int, deadline: float | None
    ) -> str:
        """The ``index``-th listed conversation whose participant is the lead."""
        is_page = _is_list(LIST_FINDER, "filter", FILTERS["all"][1])

        def matches(rows: list[InboxConversation]) -> list[str]:
            return [
                row["id"]
                for row in rows
                if any(item["leadId"] == lead_id for item in row["participants"])
            ]

        async with _InboxCapture(self._session.page) as capture:
            await self._open(capture, SALES_INBOX_URL, is_page)
            rows, _reason = await self._load(
                capture,
                is_page,
                _LEAD_SCAN_LIMIT,
                deadline,
                until=lambda loaded: len(matches(loaded)) > index,
            )
        found = matches(rows)
        if not found:
            raise LinkedInScraperException(
                f"No Sales Navigator conversation with lead {lead_id} was found "
                f"in the {len(rows)} most recent conversations of the inbox. Pass "
                "a thread_id instead."
            )
        if index >= len(found):
            raise LinkedInScraperException(
                f"index {index} out of range: only {len(found)} conversation(s) "
                f"with lead {lead_id} were found. Pass a thread_id instead."
            )
        return found[index]

    async def _older(
        self,
        capture: _InboxCapture,
        thread_call: SnResponse,
        messages: list[InboxMessage],
        profiles: Any,
    ) -> tuple[list[InboxMessage], int, bool]:
        """Up to three older batches through the page's own older-messages call."""
        headers = capture.thread_headers
        if headers is None:
            return messages, 1, False
        base = (
            f"https://www.linkedin.com/sales-api/{THREADS}/"
            f"{thread_call.call.entity_id}/messages"
        )
        batches = 1
        for _ in range(_MAX_OLDER_BATCHES):
            oldest = min(message.get("deliveredAt", 0) for message in messages)
            url = (
                f"{base}?count={MESSAGE_BATCH}&deliveredBefore={oldest}"
                f"&decoration={quote(MESSAGE_DECORATION, safe='')}"
            )
            answer = await self._session.page.evaluate(
                _FETCH_JSON, {"url": url, "headers": headers}
            )
            status = answer.get("status") if isinstance(answer, dict) else None
            if status == 429:
                note_throttle_signal("http_429")
            if not isinstance(status, int) or not 200 <= status < 300:
                logger.warning("Older Sales Navigator messages answered %s", status)
                return messages, batches, False
            older = build_older_messages(answer.get("payload"), profiles)
            if older is None:
                raise _unverified("older messages")
            batches += 1
            known = {message["id"] for message in messages}
            fresh = [message for message in older if message["id"] not in known]
            messages = fresh + messages
            if len(older) < MESSAGE_BATCH:
                return messages, batches, True
            if not fresh:
                return messages, batches, False
        return messages, batches, False

    async def get_conversation(
        self,
        thread_id: str | None = None,
        lead_id: str | None = None,
        index: int = 0,
        *,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """One conversation's messages, oldest first, by thread id or lead id."""
        if not thread_id and not lead_id:
            raise LinkedInScraperException("Provide thread_id or lead_id.")
        if index < 0:
            raise LinkedInScraperException(f"index must be non-negative (got {index}).")
        async with self._guard():
            if thread_id:
                target = normalize_sales_thread_id(thread_id)
            else:
                target = await self._thread_for_lead(
                    normalize_sales_lead_id(lead_id or ""), index, deadline
                )
            url = sales_inbox_thread_url(target)
            is_thread = _is_thread(target)
            async with _InboxCapture(self._session.page) as capture:
                # The app sends the id as encodeURIComponent does.
                refused = _Refusal(
                    "thread_id",
                    frozenset({THREADS}),
                    quote(target, safe="-_.!~*'()"),
                )
                await self._open(capture, url, is_thread, refused)
                response = capture.matching(is_thread)[0]
                built = build_thread(response.payload)
                if built is None:
                    raise _unverified("the conversation")
                conversation, profiles = built
                if not conversation["history"]["complete"]:
                    messages, batches, complete = await self._older(
                        capture, response, conversation["messages"], profiles
                    )
                    conversation["messages"] = messages
                    conversation["history"] = {
                        "batches": batches,
                        "complete": complete,
                    }
                await capture.settle()
        strip_delivered_at(conversation["messages"])
        return self._conversation_result(url, conversation, capture)

    @staticmethod
    def _conversation_result(
        url: str, conversation: SalesConversation, capture: SalesNavigatorCapture
    ) -> dict[str, Any]:
        return {
            "url": url,
            "sections": {"conversation": render_conversation(conversation)},
            "conversation": conversation,
            "marked_read": conversation["id"] in _marked_read(capture.responses),
        }

    async def _type_search(self, keywords: str) -> None:
        page = self._session.page
        for selector in _SEARCH_BOX_SELECTORS:
            box = page.locator(selector).first
            try:
                await box.click(timeout=5000)
            except PlaywrightTimeoutError:
                continue
            await human_type(page, keywords)
            return
        raise LinkedInScraperException(
            "The Sales Navigator inbox search box was not found on the page; "
            "the inbox layout may have changed."
        )

    async def search_conversations(
        self, keywords: str, limit: int = 20, *, deadline: float | None = None
    ) -> dict[str, Any]:
        """Conversations the inbox search finds for ``keywords``, up to ``limit``."""
        keywords = " ".join(keywords.split())
        if not any(char.isalnum() for char in keywords):
            raise LinkedInScraperException(
                "keywords must contain at least one letter or digit"
            )
        is_inbox = _is_list(LIST_FINDER, "filter", FILTERS["all"][1])
        is_page = _is_list(SEARCH_FINDER, "keyword", keywords)
        async with self._guard():
            async with _InboxCapture(self._session.page) as capture:
                await self._open(capture, SALES_INBOX_URL, is_inbox)
                await self._type_search(keywords)
                try:
                    await capture.wait_for(
                        is_page,
                        timeout=_SEARCH_WAIT,
                        throttle=False,
                        wanted=(THREADS,),
                    )
                finally:
                    await self._session.check_rate_limit()
                await capture.settle()
                rows, reason = await self._load(capture, is_page, limit, deadline)
        return self._list_result(
            SALES_INBOX_URL, "search_results", rows, reason, capture
        )
