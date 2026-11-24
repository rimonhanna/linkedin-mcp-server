"""Sales Navigator inbox reads: the conversation list, one conversation, search.

Every read loads a Sales Navigator inbox URL and reads the responses the page
itself fetches. Any inbox view opens its top conversation on its own, and the
app marks a conversation read once its latest message is on screen, so every
read runs with the read-state guard on: every POST under the app's thread
resource (mark read included) and its message actions are aborted. The open
conversation's participant profile is still fetched, as the app always does.
Older messages are the one request of this module's own: the page's
older-messages call, replayed with the page's own headers.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlparse

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.pacing import note_throttle_signal
from linkedin_mcp_server.scraping.identifiers import (
    SALES_INBOX_URL,
    normalize_sales_lead_id,
    normalize_sales_thread_id,
    sales_inbox_thread_url,
    sales_inbox_url,
)
from linkedin_mcp_server.scraping.messaging_read_state import without_read_state_writes
from linkedin_mcp_server.scraping.restli import envelope_elements
from linkedin_mcp_server.scraping.sales_navigator import (
    _NO_SEAT_ADVICE,
    SalesNavigatorReader,
    _Refusal,
)
from linkedin_mcp_server.scraping.sales_navigator_api import (
    SalesNavigatorCapture,
    SnResponse,
)
from linkedin_mcp_server.scraping.sales_navigator_inbox_payload import (
    ACCESS,
    FILTERS,
    LIST_FINDER,
    MESSAGE_BATCH,
    MESSAGE_DECORATION,
    NAV_CHROME,
    SEARCH_FINDER,
    THREADS,
    InboxConversation,
    InboxFilter,
    InboxMessage,
    SalesConversation,
    ViewerCheck,
    build_conversation_rows,
    build_older_messages,
    build_thread,
    chrome_viewer,
    no_inbox_access,
    render_conversation,
    render_conversations,
    strip_delivered_at,
    viewer_of,
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
# One older-messages fetch is aborted in the page after this long, so a batch
# is started only while this much of the call's time is left.
_OLDER_FETCH_TIMEOUT = 10.0
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

_FETCH_JSON = """async ({ url, headers, timeoutMs }) => {
    // linkedin-mcp:sn-inbox-history
    try {
        const response = await fetch(url, {
            method: 'GET',
            credentials: 'same-origin',
            headers,
            signal: AbortSignal.timeout(timeoutMs),
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


def _keyword_key(text: str) -> str:
    return " ".join(text.split()).casefold()


def _is_search(keywords: str) -> Callable[[SnResponse], bool]:
    """A search page for exactly ``keywords``: never one for a partial word.

    The app may also answer an earlier value of the box, so the sent keyword
    (URL-decoded once) must equal the typed one, ignoring case and spacing.
    """
    wanted = _keyword_key(keywords)

    def match(response: SnResponse) -> bool:
        call = response.call
        return (
            response.ok
            and call.method == "GET"
            and call.resource == THREADS
            and call.entity_id is None
            and call.finder == SEARCH_FINDER
            and _keyword_key(unquote(call.params.get("keyword", ""))) == wanted
        )

    return match


def _search_miss(capture: SalesNavigatorCapture) -> LinkedInScraperException:
    others = [
        unquote(response.call.params.get("keyword", ""))
        for response in capture.responses
        if response.call.resource == THREADS and response.call.finder == SEARCH_FINDER
    ]
    # The keywords are the user's own query; they stay out of the warning.
    logger.debug("Sales Navigator search answered other keywords: %r", others)
    return LinkedInScraperException(
        "Sales Navigator did not return results for these keywords in time "
        f"({len(others)} search response(s) were for other keywords), so "
        "nothing was reported."
    )


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
    rows = envelope_elements(last.payload)
    return asked.isdigit() and rows is not None and len(rows) < int(asked)


def _viewer_unclear(check: ViewerCheck) -> LinkedInScraperException:
    if check == "chrome_names_no_participant":
        logger.warning(
            "The Sales Navigator page named a signed-in member who is no "
            "participant of the conversations read; nothing was reported"
        )
        return LinkedInScraperException(
            "The Sales Navigator page named no participant as you, so nothing "
            "was reported rather than mix up your messages with the other "
            "person's."
        )
    logger.warning("Sales Navigator viewer signals disagree (%s)", check)
    return LinkedInScraperException(
        "Sales Navigator's own data does not agree on which participant is "
        "you, so nothing was reported rather than mix up your messages with "
        "the other person's."
    )


def _lead_miss_note(reason: str) -> str:
    stopped = (
        f" The scan stopped early: {_STOP_REASONS[reason]}."
        if reason in _STOP_REASONS
        else ""
    )
    return (
        f"{stopped} Not finding one is no proof there is none: archived and "
        "older conversations are not searched. Pass a thread_id instead."
    )


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
    """Also keep the page's own thread and older-messages requests, for replay."""

    def __init__(self, page: Any):
        super().__init__(page, resources=frozenset({THREADS, NAV_CHROME, ACCESS}))
        self.thread_headers: dict[str, str] | None = None
        # The first older-messages request the page made: its URL and headers.
        self.older_template: tuple[str, dict[str, str]] | None = None
        # Set when the viewer was told apart without the chrome's member.
        self.chrome_unavailable = False
        self._started: set[str] = set()

    def _extra_listeners(self) -> tuple[tuple[str, Callable[..., None]], ...]:
        return (("request", self._on_request),)

    def _on_request(self, request: Any) -> None:
        call = parse_sn_call(request.url, request.method)
        if (
            call is None
            or call.method != "GET"
            or call.resource != THREADS
            or call.entity_id is None
            or call.sub_path not in (None, "messages")
        ):
            return
        kind = call.sub_path or "thread"
        if kind in self._started:
            return
        self._started.add(kind)

        async def read() -> None:
            try:
                raw = await request.all_headers()
            except Exception:
                self._started.discard(kind)
                return
            headers = {
                key: value
                for key, value in raw.items()
                if key.casefold() in _FORWARDED_HEADERS
            }
            if kind == "thread":
                self.thread_headers = headers
            else:
                self.older_template = (request.url, headers)

        self._spawn(read())

    def viewer(self) -> str | None:
        """Who the app's chrome says is signed in, when the page loaded it."""
        for response in self.responses:
            if response.ok and response.call.resource == NAV_CHROME:
                if (named := chrome_viewer(response.payload)) is not None:
                    return named
        return None

    def inbox_refused(self) -> bool:
        return any(
            response.ok
            and response.call.resource == ACCESS
            and no_inbox_access(response.payload)
            for response in self.responses
        )


class SalesNavigatorInboxReader:
    """Own every workflow whose subject is the Sales Navigator inbox."""

    def __init__(
        self,
        session: ScrapingSession,
        sales_navigator: SalesNavigatorReader,
    ):
        self._session = session
        # Its page opening is the shared seat and landing check.
        self._sales_navigator = sales_navigator

    def _guard(self):
        return without_read_state_writes(self._session.page)

    async def _open(
        self,
        capture: _InboxCapture,
        url: str,
        ready: Callable[[SnResponse], bool],
        refused: _Refusal | None = None,
    ) -> None:
        try:
            await self._sales_navigator._open(
                capture, url, expected_path=_INBOX_PATH, ready=ready, refused=refused
            )
        except LinkedInScraperException as exc:
            if capture.inbox_refused():
                raise self._no_inbox() from exc
            raise
        # The access check only explains a failed load: the inbox loaded.
        if capture.inbox_refused():
            logger.info(
                "Sales Navigator's access check says the contract has no inbox, "
                "but the inbox loaded; reading it"
            )

    @staticmethod
    def _no_inbox() -> LinkedInScraperException:
        return LinkedInScraperException(
            "Sales Navigator says this account's contract has no Sales "
            "Navigator inbox. " + _NO_SEAT_ADVICE
        )

    @staticmethod
    def _viewer(capture: _InboxCapture, payload: Any) -> str | None:
        chrome = capture.viewer()
        if chrome is None and not capture.chrome_unavailable:
            capture.chrome_unavailable = True
            logger.warning(
                "The Sales Navigator page named no recognised signed-in member; "
                "the viewer is told apart by the degree and every-thread checks "
                "alone"
            )
        viewer, failed = viewer_of(payload, chrome)
        if failed is not None:
            raise _viewer_unclear(failed)
        return viewer

    @classmethod
    def _rows(
        cls, capture: _InboxCapture, is_page: Callable[[SnResponse], bool]
    ) -> list[InboxConversation]:
        rows: list[InboxConversation] = []
        seen: set[str] = set()
        for response in capture.matching(is_page):
            built = build_conversation_rows(
                response.payload, cls._viewer(capture, response.payload)
            )
            if built is None:
                raise _unverified("the inbox")
            for row in built:
                if row["id"] not in seen:
                    seen.add(row["id"])
                    rows.append(row)
        return rows

    async def _load(
        self,
        capture: _InboxCapture,
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
            exhausted = _exhausted(capture.matching(is_page))
            # A short last page that is exactly the limit leaves nothing more.
            if len(rows) >= limit and not (exhausted and len(rows) == limit):
                return rows[:limit], "limit"
            if exhausted:
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
        capture: _InboxCapture,
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
    ) -> tuple[str, bool]:
        """The ``index``-th listed conversation whose participant is the lead.

        Also whether LinkedIn answered a mark-read during the scan: the inbox
        opens its top conversation on its own, whichever thread that is.
        """
        is_page = _is_list(LIST_FINDER, "filter", FILTERS["all"][1])

        def matches(rows: list[InboxConversation]) -> list[str]:
            return [
                row["id"]
                for row in rows
                if any(item["leadId"] == lead_id for item in row["participants"])
            ]

        async with _InboxCapture(self._session.page) as capture:
            await self._open(capture, SALES_INBOX_URL, is_page)
            rows, reason = await self._load(
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
                f"in the {len(rows)} most recent conversations of the inbox."
                + _lead_miss_note(reason)
            )
        if index >= len(found):
            raise LinkedInScraperException(
                f"index {index} out of range: only {len(found)} conversation(s) "
                f"with lead {lead_id} were found." + _lead_miss_note(reason)
            )
        return found[index], bool(_marked_read(capture.responses))

    @staticmethod
    def _older_request(
        capture: _InboxCapture, thread_call: SnResponse
    ) -> tuple[str, dict[str, str], int] | None:
        """Base URL, headers and batch size of the older-messages call.

        The page's own older-messages request when it made one for this
        thread, with every parameter but ``deliveredBefore`` kept; otherwise
        the decoration the page was measured sending, with the thread's
        headers.
        """
        base = (
            f"https://www.linkedin.com/sales-api/{THREADS}/"
            f"{thread_call.call.entity_id}/messages"
        )
        if capture.older_template is not None:
            url, headers = capture.older_template
            call = parse_sn_call(url, "GET")
            if call is not None and call.entity_id == thread_call.call.entity_id:
                params = {
                    key: value
                    for key, value in call.params.items()
                    if key != "deliveredBefore"
                }
                count = params.get("count", "")
                query = "&".join(f"{key}={value}" for key, value in params.items())
                return (
                    f"{base}?{query}" if query else base,
                    headers,
                    int(count) if count.isdigit() else MESSAGE_BATCH,
                )
        if capture.thread_headers is None:
            return None
        query = urlencode(
            {"count": MESSAGE_BATCH, "decoration": MESSAGE_DECORATION},
            quote_via=quote,
            safe="",
        )
        return f"{base}?{query}", capture.thread_headers, MESSAGE_BATCH

    async def _older(
        self,
        capture: _InboxCapture,
        thread_call: SnResponse,
        messages: list[InboxMessage],
        profiles: Any,
        viewer: str | None,
        deadline: float | None,
    ) -> tuple[list[InboxMessage], int, bool]:
        """Up to three older batches through the page's own older-messages call.

        A batch that could not finish before ``deadline`` is not started, and
        the history is then reported incomplete.
        """
        request = self._older_request(capture, thread_call)
        if request is None:
            return messages, 1, False
        base, headers, batch = request
        batches = 1
        for _ in range(_MAX_OLDER_BATCHES):
            if (
                deadline is not None
                and time.monotonic() + _OLDER_FETCH_TIMEOUT > deadline
            ):
                return messages, batches, False
            oldest = min(message.get("deliveredAt", 0) for message in messages)
            url = f"{base}{'&' if '?' in base else '?'}deliveredBefore={oldest}"
            answer = await self._session.page.evaluate(
                _FETCH_JSON,
                {
                    "url": url,
                    "headers": headers,
                    "timeoutMs": int(_OLDER_FETCH_TIMEOUT * 1000),
                },
            )
            status = answer.get("status") if isinstance(answer, dict) else None
            if status == 429:
                note_throttle_signal("http_429")
            if not isinstance(status, int) or not 200 <= status < 300:
                logger.warning("Older Sales Navigator messages answered %s", status)
                return messages, batches, False
            older = build_older_messages(answer.get("payload"), profiles, viewer)
            if older is None:
                raise _unverified("older messages")
            batches += 1
            known = {message["id"] for message in messages}
            fresh = [message for message in older if message["id"] not in known]
            messages = fresh + messages
            if len(older) < batch:
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
            scan_marked_read = False
            if thread_id:
                target = normalize_sales_thread_id(thread_id)
            else:
                target, scan_marked_read = await self._thread_for_lead(
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
                viewer = self._viewer(capture, response.payload)
                built = build_thread(response.payload, viewer)
                if built is None:
                    raise _unverified("the conversation")
                conversation, profiles = built
                if not conversation["history"]["complete"]:
                    messages, batches, complete = await self._older(
                        capture,
                        response,
                        conversation["messages"],
                        profiles,
                        viewer,
                        deadline,
                    )
                    conversation["messages"] = messages
                    conversation["history"] = {
                        "batches": batches,
                        "complete": complete,
                    }
                await capture.settle()
        strip_delivered_at(conversation["messages"])
        return self._conversation_result(url, conversation, capture, scan_marked_read)

    @staticmethod
    def _conversation_result(
        url: str,
        conversation: SalesConversation,
        capture: SalesNavigatorCapture,
        scan_marked_read: bool,
    ) -> dict[str, Any]:
        return {
            "url": url,
            "sections": {"conversation": render_conversation(conversation)},
            "conversation": conversation,
            "marked_read": conversation["id"] in _marked_read(capture.responses),
            "scan_marked_read": scan_marked_read,
        }

    async def _type_search(self, keywords: str) -> None:
        page = self._session.page
        for selector in _SEARCH_BOX_SELECTORS:
            box = page.locator(selector).first
            try:
                await box.click(timeout=5000)
            except PlaywrightTimeoutError:
                continue
            # One insert, one input event: typed key by key, the app searched
            # a partial word at a pause and never the full keywords.
            await page.keyboard.insert_text(keywords)
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
        is_page = _is_search(keywords)
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
                except LinkedInScraperException as exc:
                    raise _search_miss(capture) from exc
                finally:
                    await self._session.check_rate_limit()
                await capture.settle()
                rows, reason = await self._load(capture, is_page, limit, deadline)
        return self._list_result(
            SALES_INBOX_URL, "search_results", rows, reason, capture
        )
