"""Messaging inbox, thread and conversation-search workflows."""

from __future__ import annotations

from datetime import datetime
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

import logging

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.humanize import human_type
from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
    LinkedInScraperException,
)
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.identifiers import (
    messaging_thread_url,
    normalize_person_identifier,
    normalize_thread_id,
    person_profile_url,
)
from linkedin_mcp_server.scraping.link_metadata import (
    Reference,
    build_references,
    dedupe_references,
)
from linkedin_mcp_server.scraping.messaging_api import MessagingApiCapture
from linkedin_mcp_server.scraping.messaging_payload import (
    build_conversation_references,
    conversation_rows_text,
    build_message_references,
    build_structured_conversation,
    conversation_matches_username,
    conversation_thread_path,
    find_conversation_by_thread_id,
    format_message_elements,
)
from linkedin_mcp_server.scraping.messaging_read_state import without_read_state_writes
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.profile_page import ProfilePageReader
from linkedin_mcp_server.scraping.session import ScrapingSession
from linkedin_mcp_server.scraping.text import strip_linkedin_noise

if TYPE_CHECKING:
    from linkedin_mcp_server.callbacks import ProgressCallback

logger = logging.getLogger(__name__)

_NEW_THREAD_PATH = "/messaging/thread/new/"

# The search control is an input whose aria-label or placeholder says
# "Search messages" (English UI).
_SEARCH_BOX_SELECTOR = (
    'input[aria-label*="Search messages" i], input[placeholder*="Search messages" i]'
)

_INBOX_MAX_SCROLLS = 150
_INBOX_MAX_LIMIT = 1000
# Under the tool timeout, past which FastMCP discards whatever was loaded.
_INBOX_SCROLL_BUDGET = 480.0
# get_conversation runs under the default tool timeout (180 s), not get_inbox's.
_THREAD_SCROLL_BUDGET = 120.0
_INBOX_SCROLL_PAUSE = 1.2
# Unverified against the live site; the scroll falls back to the largest region.
_INBOX_LIST_SELECTOR = ".msg-conversations-container__conversations-list"


class ConversationReader:
    """Own every workflow whose subject is a LinkedIn messaging thread.

    LinkedIn's messaging UI emits stable conversation URLs, participant profile
    URLs, read flags and message entities in its authenticated data responses.
    Reading those responses avoids selecting sidebar rows or opening thread
    routes, which preserves unread state and eliminates layout-dependent clicks.
    """

    def __init__(
        self,
        session: ScrapingSession,
        navigator: PageNavigator,
        content: PageContentReader,
        profile_page: ProfilePageReader,
    ):
        self._session = session
        self._navigator = navigator
        self._content = content
        self._profile_page = profile_page

    @staticmethod
    def _single_section_result(
        url: str,
        section_name: str,
        text: str,
        references: list[Reference] | None = None,
    ) -> dict[str, Any]:
        """Build a standard single-section scraping response."""
        result: dict[str, Any] = {"url": url, "sections": {}}
        if text:
            result["sections"][section_name] = text
            if references:
                result["references"] = {section_name: references}
        return result

    async def _wait_for_main_text(
        self,
        *,
        minimum_length: int = 100,
        timeout: int = 10000,
        log_context: str,
    ) -> None:
        """Wait for main content to populate enough text to scrape."""
        try:
            await self._session.page.wait_for_function(
                """({ minimumLength }) => {
                    const main = document.querySelector('main');
                    if (!main) return false;
                    return main.innerText.length > minimumLength;
                }""",
                arg={"minimumLength": minimum_length},
                timeout=timeout,
            )
        except PlaywrightTimeoutError:
            logger.debug("%s content did not appear", log_context)

    async def _scroll_main_scrollable_region(
        self,
        *,
        position: Literal["top", "bottom"],
        attempts: int,
        pause_time: float = 0.5,
        selector: str | None = None,
    ) -> None:
        """Scroll the preferred region, else the largest scrollable one in main."""
        for _ in range(attempts):
            await self._session.page.evaluate(
                """({ position, selector }) => {
                    const main = document.querySelector('main');
                    if (!main) return false;

                    const isScrollable = element => {
                        const style = window.getComputedStyle(element);
                        return (
                            (style.overflowY === 'auto' || style.overflowY === 'scroll') &&
                            element.scrollHeight > element.clientHeight + 20
                        );
                    };

                    const preferred = selector ? document.querySelector(selector) : null;
                    const candidates = [main, ...main.querySelectorAll('*')].filter(isScrollable);
                    const target = (preferred && isScrollable(preferred) ? preferred : null) || candidates.sort(
                        (left, right) => right.scrollHeight - left.scrollHeight
                    )[0] || main;
                    target.scrollTop = position === 'top' ? 0 : target.scrollHeight;
                    return true;
                }""",
                {"position": position, "selector": selector},
            )
            await self._session.delay(pause_time)

    async def _load_inbox_to_limit(
        self,
        capture: MessagingApiCapture,
        offset: int,
        limit: int,
        callbacks: ProgressCallback | None = None,
        until: Callable[[list[dict[str, Any]]], bool] | None = None,
        budget: float = _INBOX_SCROLL_BUDGET,
    ) -> None:
        """Scroll the sidebar list, waiting for each next page, until limit is met.

        ``until`` ends the scroll early once it accepts the loaded conversations.
        """
        stalled = 0
        deadline = self._session.monotonic() + budget
        stop_reason = f"{_INBOX_MAX_SCROLLS} scrolls spent"
        loaded = 0
        reported = 0
        for _ in range(_INBOX_MAX_SCROLLS):
            conversations = self._unique_conversations(
                capture.conversations_since(offset)
            )
            loaded = len(conversations)
            if until is not None and until(conversations):
                return
            if callbacks and loaded != reported:
                reported = loaded
                await callbacks.on_progress(
                    f"Loaded {min(loaded, limit)} of {limit} conversations",
                    min(loaded, limit) * 95 // limit,
                )
            if loaded >= limit:
                return
            if stalled >= 2:
                stop_reason = "no new conversations after 2 scrolls"
                break
            if self._session.monotonic() >= deadline:
                stop_reason = f"{budget:.0f}s scroll budget spent"
                break
            seen = len(capture.payloads)
            await self._scroll_main_scrollable_region(
                position="bottom",
                attempts=1,
                pause_time=self._session.jittered(_INBOX_SCROLL_PAUSE),
                selector=_INBOX_LIST_SELECTOR,
            )
            if await capture.wait_for_next_payload(after=seen):
                await capture.settle()
            grown = (
                len(self._unique_conversations(capture.conversations_since(offset)))
                > loaded
            )
            stalled = 0 if grown else stalled + 1
        else:
            loaded = len(
                self._unique_conversations(capture.conversations_since(offset))
            )
        logger.info(
            "Inbox stopped at %d of %d conversations: %s", loaded, limit, stop_reason
        )

    async def _load_messaging_page(
        self,
        capture: MessagingApiCapture,
        url: str,
        *,
        log_context: str,
        scroll_attempts: int = 0,
        throttle: bool = True,
    ) -> int:
        """Navigate once and wait for the matching passive conversation payload."""
        offset = len(capture.payloads)
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        await self._wait_for_main_text(log_context=log_context)
        await self._session.dismiss_modal()
        if scroll_attempts:
            await self._scroll_main_scrollable_region(
                position="bottom", attempts=scroll_attempts, pause_time=0.5
            )
        await capture.wait_for_payload(after=offset, throttle=throttle)
        await capture.settle()
        return offset

    def _without_read_state_writes(self):
        return without_read_state_writes(self._session.page)

    @staticmethod
    def _unique_conversations(
        conversations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in conversations:
            path = conversation_thread_path(item)
            if path is None or path in seen:
                continue
            seen.add(path)
            unique.append(item)
        return unique

    async def _conversation_for_username(
        self,
        capture: MessagingApiCapture,
        linkedin_username: str,
        index: int,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Resolve a username through exact participant profile URLs."""
        if index < 0:
            raise LinkedInScraperException(f"index must be non-negative (got {index}).")
        username = normalize_person_identifier(linkedin_username)
        await self._navigator._navigate_to_page(person_profile_url(username, "/"))
        await self._session.check_rate_limit()
        try:
            await self._session.page.wait_for_selector("main")
        except PlaywrightTimeoutError:
            logger.debug("Profile page did not load while resolving a conversation")
        await self._session.dismiss_modal()
        display_name = await self._profile_page._read_profile_display_name()
        if not display_name:
            raise LinkedInScraperException(
                f"Could not resolve a display name for {username}."
            )

        await self._load_messaging_page(
            capture,
            "https://www.linkedin.com/messaging/",
            log_context="Messaging search",
            throttle=False,
        )
        offset = await self._run_search(capture, display_name)
        candidates = self._unique_conversations(capture.conversations_since(offset))
        matches = [
            item for item in candidates if conversation_matches_username(item, username)
        ]
        if not matches or not capture.has_message_template:
            capture.end_search()
            offset = await self._load_messaging_page(
                capture,
                "https://www.linkedin.com/messaging/",
                log_context="Messaging inbox",
                scroll_attempts=2 if not matches else 0,
            )
            inbox_candidates = self._unique_conversations(
                capture.conversations_since(offset)
            )
            if not matches:
                matches = [
                    item
                    for item in inbox_candidates
                    if conversation_matches_username(item, username)
                ]
            candidates = self._unique_conversations(candidates + inbox_candidates)
        if not matches:
            raise LinkedInScraperException(
                f"Could not find a conversation for {username}."
                " Pass a known thread_id instead."
            )
        if index >= len(matches):
            raise LinkedInScraperException(
                f"index {index} out of range: only {len(matches)} thread(s) "
                f"exist for {username}. Pass a known thread_id instead."
            )
        return matches[index], candidates

    async def get_inbox(
        self, limit: int = 20, callbacks: ProgressCallback | None = None
    ) -> dict[str, Any]:
        """List recent conversations without selecting any sidebar row."""
        async with self._without_read_state_writes():
            url = "https://www.linkedin.com/messaging/"
            async with MessagingApiCapture(self._session.page) as capture:
                offset = await self._load_messaging_page(
                    capture,
                    url,
                    log_context="Messaging inbox",
                    scroll_attempts=1,
                )
                await self._load_inbox_to_limit(capture, offset, limit, callbacks)
                await self._session.check_rate_limit()
                raw_result = await self._content._extract_root_content(["main"])
                payloads = capture.payloads[offset:]

            raw = raw_result["text"]
            cleaned = strip_linkedin_noise(raw) if raw else ""
            references: list[Reference] = (
                build_references(raw_result["references"], "inbox") if cleaned else []
            )
            conversation_refs = build_conversation_references(
                payloads, limit=limit, context="inbox"
            )
            if conversation_refs:
                cleaned = (
                    conversation_rows_text(
                        payloads, {ref["url"] for ref in conversation_refs}
                    )
                    or cleaned
                )
                # Text and references cover the same rows: a thread seen only in
                # the DOM has no row, so it is dropped here.
                references = dedupe_references(
                    conversation_refs
                    + [ref for ref in references if ref["kind"] != "conversation"]
                )
            return self._single_section_result(
                url, "inbox", cleaned, references=references
            )

    async def get_conversation(
        self,
        linkedin_username: str | None = None,
        thread_id: str | None = None,
        index: int = 0,
    ) -> dict[str, Any]:
        """Read a conversation through LinkedIn's passive message response."""
        if not linkedin_username and not thread_id:
            raise InvalidReferenceError(
                "Provide at least one of linkedin_username or thread_id"
            )

        async with self._without_read_state_writes():
            async with MessagingApiCapture(self._session.page) as capture:
                if thread_id:
                    normalized_thread_id = normalize_thread_id(thread_id)
                    offset = await self._load_messaging_page(
                        capture,
                        "https://www.linkedin.com/messaging/",
                        log_context="Messaging inbox",
                        scroll_attempts=1,
                    )
                    conversations = self._unique_conversations(
                        capture.conversations_since(offset)
                    )
                    target = find_conversation_by_thread_id(
                        conversations, normalized_thread_id
                    )
                    if target is None:
                        # Opening the thread route would mark it read, so scroll
                        # the list until the thread's row has loaded instead.
                        await self._load_inbox_to_limit(
                            capture,
                            offset,
                            _INBOX_MAX_LIMIT,
                            budget=_THREAD_SCROLL_BUDGET,
                            until=lambda loaded: (
                                find_conversation_by_thread_id(
                                    loaded, normalized_thread_id
                                )
                                is not None
                            ),
                        )
                        conversations = self._unique_conversations(
                            capture.conversations_since(offset)
                        )
                        target = find_conversation_by_thread_id(
                            conversations, normalized_thread_id
                        )
                    if target is None:
                        raise LinkedInScraperException(
                            "Could not map that thread ID to a passive conversation response."
                        )
                else:
                    target, conversations = await self._conversation_for_username(
                        capture, linkedin_username or "", index
                    )
                    path = conversation_thread_path(target)
                    if path is None:
                        raise LinkedInScraperException(
                            "The matched conversation has no stable thread URL."
                        )
                    normalized_thread_id = normalize_thread_id(
                        path.removeprefix("/messaging/thread/").removesuffix("/")
                    )

                messages = await capture.fetch_message_history(
                    target,
                    conversations=conversations,
                )

            cleaned = format_message_elements(
                messages, timezone=datetime.now().astimezone().tzinfo
            )
            references = (
                build_message_references(messages, conversation=target)
                if cleaned
                else []
            )
            result = self._single_section_result(
                messaging_thread_url(normalized_thread_id, "/"),
                "conversation",
                cleaned,
                references=references,
            )
            result["conversation"] = build_structured_conversation(
                target, messages, history=capture.history
            )
            return result

    async def search_conversations(
        self, keywords: str, limit: int = 20
    ) -> dict[str, Any]:
        """Search messages through the page's search box, selecting no result row."""
        keywords = " ".join(keywords.split())
        async with self._without_read_state_writes():
            async with MessagingApiCapture(self._session.page) as capture:
                await self._load_messaging_page(
                    capture,
                    "https://www.linkedin.com/messaging/",
                    log_context="Messaging search",
                    throttle=False,
                )
                offset = await self._run_search(capture, keywords)
                payloads = capture.payloads[offset:][::-1]
                logger.debug(
                    "Search accepted %d responses holding %d conversations",
                    len(payloads),
                    len(capture.conversations_since(offset)),
                )

            references = [
                ref
                for ref in build_conversation_references(
                    payloads, limit=None, context="search_results"
                )
                if not ref["url"].startswith(_NEW_THREAD_PATH)
            ][:limit]
            return self._single_section_result(
                self._session.page.url,
                "search_results",
                conversation_rows_text(payloads, {ref["url"] for ref in references}),
                references=references,
            )

    async def _run_search(self, capture: MessagingApiCapture, keywords: str) -> int:
        """Type into the page's search box and wait for the answering responses."""
        offset = capture.begin_search(keywords)
        await self._submit_search(keywords)
        try:
            await capture.wait_for_payload(after=offset, throttle=False)
        finally:
            await self._session.check_rate_limit()
        await capture.settle()
        if not capture.conversations_since(offset):
            # A debounced keystroke can answer first with nothing.
            await capture.wait_for_next_payload(
                after=len(capture.payloads), timeout=3.0
            )
            await capture.settle()
        return offset

    async def _submit_search(self, keywords: str) -> None:
        page = self._session.page
        box = page.locator(_SEARCH_BOX_SELECTOR).first
        try:
            await box.click(timeout=5000)
        except PlaywrightTimeoutError as exc:
            raise LinkedInScraperException(
                "The messaging search box was not found on the page; "
                "LinkedIn's messaging layout may have changed."
            ) from exc
        await human_type(page, keywords)
        await page.keyboard.press("Enter")
