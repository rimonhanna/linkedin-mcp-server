"""Tests for passive messaging conversation workflows."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from patchright.async_api import Route

from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
    LinkedInScraperException,
)
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.conversations import (
    ConversationReader,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.profile_page import ProfilePageReader
from linkedin_mcp_server.scraping.session import ScrapingSession


async def _no_message_target() -> SimpleNamespace:
    raise AssertionError("the conversation reader never reads a message target")


def _reader(page: Any) -> ConversationReader:
    session = ScrapingSession(page)
    return ConversationReader(
        session,
        PageNavigator(session),
        PageContentReader(session),
        ProfilePageReader(session, _no_message_target),
    )


def _member(username: str, first: str, last: str) -> dict[str, Any]:
    return {
        "participantType": {
            "member": {
                "firstName": {"text": first},
                "lastName": {"text": last},
                "profileUrl": f"https://www.linkedin.com/in/{username}/",
            }
        }
    }


def _conversation(
    thread_id: str,
    *,
    title: str = "Ada Lovelace",
    username: str = "ada",
    read: bool = True,
) -> dict[str, Any]:
    return {
        "backendUrn": f"urn:li:messagingThread:{thread_id}",
        "entityUrn": f"urn:li:msg_conversation:({thread_id})",
        "conversationUrl": f"https://www.linkedin.com/messaging/thread/{thread_id}/",
        "conversationParticipants": [
            _member(username, title.split()[0], title.split()[-1]),
            _member("viewer", "Current", "User"),
        ],
        "title": {"text": title},
        "read": read,
        "unreadCount": 0 if read else 1,
    }


def _inbox(*items: dict[str, Any], search: bool = False) -> dict[str, Any]:
    return {
        "_search": search,
        "data": {
            "messengerConversationsByCategoryQuery": {
                "elements": list(items),
                "metadata": {"nextCursor": "private"},
            }
        },
    }


def _messages(*bodies: str) -> dict[str, Any]:
    target_urn = "urn:li:msg_conversation:(2-target)"
    return {
        "data": {
            "messengerMessagesBySyncToken": {
                "elements": [
                    {
                        "backendConversationUrn": target_urn,
                        "deliveredAt": index,
                        "sender": _member("ada", "Ada", "Lovelace"),
                        "body": {"text": body},
                    }
                    for index, body in enumerate(bodies)
                ]
            }
        }
    }


class FakeCapture:
    def __init__(
        self,
        payloads: list[dict[str, Any]],
        *,
        messages: dict[str, Any] | None = None,
        has_message_template: bool = True,
    ) -> None:
        self.payloads: list[dict[str, Any]] = []
        self._queued = list(payloads)
        self.messages = messages or _messages("Hello")
        self.history = {
            "scope": "bounded_thread",
            "exhaustion": "unknown",
            "stopReason": "short_batch",
            "pagesFetched": 1,
            "messageCount": len(
                self.messages["data"]["messengerMessagesBySyncToken"]["elements"]
            ),
            "pageSize": 20,
            "maxOlderPages": 3,
        }
        self.has_message_template = has_message_template
        self.search_keywords: str | None = None
        self.dropped = 0
        self.fetch_calls: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

    def begin_search(self, keywords: str) -> int:
        self.search_keywords = keywords
        return len(self.payloads)

    def end_search(self) -> None:
        self.search_keywords = None

    async def __aenter__(self) -> FakeCapture:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def _deliver(self) -> None:
        # Like the real capture, a latched search filter drops inbox responses.
        payload = self._queued.pop(0)
        if self.search_keywords is None or payload.get("_search"):
            self.payloads.append(payload)
        else:
            self.dropped += 1

    async def wait_for_payload(
        self, *, after: int, timeout: float = 12.0, throttle: bool = True
    ) -> None:
        del timeout, throttle
        if len(self.payloads) <= after and self._queued:
            self._deliver()
        if len(self.payloads) <= after and self.dropped:
            raise LinkedInScraperException("only inbox responses arrived in a search")

    async def wait_for_next_payload(self, *, after: int, timeout: float = 4.0) -> bool:
        del timeout
        if len(self.payloads) <= after and self._queued:
            self._deliver()
        return len(self.payloads) > after

    async def settle(self, timeout: float = 1.0) -> None:
        del timeout

    def conversations_since(self, offset: int = 0) -> list[dict[str, Any]]:
        return [
            item
            for payload in self.payloads[offset:]
            for item in payload["data"]["messengerConversationsByCategoryQuery"][
                "elements"
            ]
        ]

    async def fetch_message_history(
        self,
        target: dict[str, Any],
        *,
        conversations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        self.fetch_calls.append((target, conversations))
        return self.messages["data"]["messengerMessagesBySyncToken"]["elements"]


@pytest.fixture(autouse=True)
def session_boundaries():
    with (
        patch.object(ScrapingSession, "check_rate_limit", new_callable=AsyncMock),
        patch.object(ScrapingSession, "dismiss_modal", new_callable=AsyncMock),
        patch.object(ScrapingSession, "delay", new_callable=AsyncMock),
    ):
        yield


def _root(text: str, references: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"source": "root", "text": text, "references": references or []}


class TestGetInbox:
    async def test_mixed_read_states_produce_refs_without_row_clicks(self, mock_page):
        unread = _conversation("2-unread", read=False)
        read = _conversation("2-read", title="Grace Hopper", username="grace")
        capture = FakeCapture([_inbox(unread, read)])
        reader = _reader(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.conversations.MessagingApiCapture",
                return_value=capture,
            ),
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("Ada Lovelace\nGrace Hopper"),
            ),
        ):
            result = await reader.get_inbox(limit=20)

        assert [ref["url"] for ref in result["references"]["inbox"]] == [
            "/messaging/thread/2-unread/",
            "/messaging/thread/2-read/",
        ]
        assert unread["read"] is False
        assert read["read"] is True
        mock_page.locator.return_value.click.assert_not_called()

    async def test_text_covers_every_payload_row_with_path(self, mock_page):
        rows = []
        for i in range(3):
            row = _conversation(f"2-{i}", title=f"Person{i} Name")
            row["lastActivityAt"] = 1_700_000_000_000
            row["latestMessage"] = {"body": {"text": f"hello {i}"}}
            rows.append(row)
        reader = _reader(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.conversations.MessagingApiCapture",
                return_value=FakeCapture([_inbox(*rows)]),
            ),
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("only the rendered rows"),
            ),
        ):
            result = await reader.get_inbox(limit=2)

        text = result["sections"]["inbox"]
        assert "only the rendered rows" not in text
        assert "hello 1" in text
        assert "/messaging/thread/1/" not in text
        assert "/messaging/thread/2-1/" in text
        assert "2-2" not in text

    async def test_text_and_references_cover_the_same_rows(self, mock_page):
        long_body = "x" * 600
        row = _conversation("2-0", title="Person Name")
        row["latestMessage"] = {"body": {"text": long_body}}
        reader = _reader(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.conversations.MessagingApiCapture",
                return_value=FakeCapture([_inbox(row)]),
            ),
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value={
                    **_root("rendered rows"),
                    "references": [
                        {
                            "href": "/messaging/thread/2-dom-only/",
                            "text": "Other Person",
                        }
                    ],
                },
            ),
        ):
            result = await reader.get_inbox(limit=5)

        urls = [r["url"] for r in result["references"]["inbox"]]
        assert "/messaging/thread/2-dom-only/" not in urls
        assert "/messaging/thread/2-0/" in urls
        text = result["sections"]["inbox"]
        assert long_body not in text
        assert "x" * 500 + "..." in text

    async def test_empty_page_omits_optional_sections_and_references(self, mock_page):
        capture = FakeCapture([_inbox()])
        reader = _reader(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.conversations.MessagingApiCapture",
                return_value=capture,
            ),
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root(""),
            ),
        ):
            result = await reader.get_inbox(limit=5)

        assert result == {
            "url": "https://www.linkedin.com/messaging/",
            "sections": {},
        }


class TestInboxPaging:
    async def _run(self, mock_page, pages, limit, root=None):
        capture = FakeCapture(pages)
        reader = _reader(mock_page)
        scroll = AsyncMock()
        with (
            patch(
                "linkedin_mcp_server.scraping.conversations.MessagingApiCapture",
                return_value=capture,
            ),
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(reader, "_scroll_main_scrollable_region", scroll),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=root or _root("inbox"),
            ),
        ):
            result = await reader.get_inbox(limit=limit)
        return result, scroll, capture

    @staticmethod
    def _page(start: int, count: int) -> dict[str, Any]:
        return _inbox(*[_conversation(f"2-{i}") for i in range(start, start + count)])

    async def test_paging_stops_at_limit(self, mock_page):
        pages = [self._page(i * 5, 5) for i in range(6)]
        result, scroll, capture = await self._run(mock_page, pages, 12)

        assert scroll.await_count == 3
        assert len(capture.payloads) == 3
        assert len(result["references"]["inbox"]) == 12

    async def test_stops_after_two_scrolls_without_growth(self, mock_page):
        result, scroll, _ = await self._run(mock_page, [self._page(0, 5)], 40)

        assert scroll.await_count == 3
        assert len(result["references"]["inbox"]) == 5

    async def test_dom_thread_anchors_are_capped_at_limit(self, mock_page):
        anchors = [
            {
                "href": f"https://www.linkedin.com/messaging/thread/2-dom{i}/",
                "text": f"P{i}",
            }
            for i in range(10)
        ]
        result, _, _ = await self._run(
            mock_page,
            [self._page(0, 3)],
            1,
            root=_root("inbox", anchors),
        )

        conversations = [
            r for r in result["references"]["inbox"] if r["kind"] == "conversation"
        ]
        assert len(conversations) == 1

    async def test_scroll_budget_stops_quietly(self, mock_page):
        pages = [self._page(i * 5, 5) for i in range(6)]
        clock = iter([0.0, 0.0, 1000.0, 1000.0, 1000.0])
        with patch.object(
            ScrapingSession, "monotonic", side_effect=lambda: next(clock)
        ):
            result, scroll, _ = await self._run(mock_page, pages, 40)

        assert scroll.await_count == 2
        assert len(result["references"]["inbox"]) == 10

    async def test_rate_limit_is_checked_after_paging(self, mock_page):
        with patch.object(
            ScrapingSession, "check_rate_limit", new_callable=AsyncMock
        ) as check:
            await self._run(mock_page, [self._page(0, 2)], 40)

        assert check.await_count >= 2

    async def test_next_page_timeout_returns_loaded_without_strike(self, mock_page):
        from linkedin_mcp_server.scraping.messaging_api import MessagingApiCapture

        capture = MessagingApiCapture(mock_page)
        with patch(
            "linkedin_mcp_server.scraping.messaging_api.note_throttle_signal"
        ) as strike:
            assert await capture.wait_for_next_payload(after=0, timeout=0.01) is False
        strike.assert_not_called()


class TestReadStateWrites:
    @pytest.mark.parametrize("navigation_fails", [False, True])
    async def test_inbox_routes_read_flag_writes_around_navigation(
        self, mock_page, navigation_fails
    ):
        calls: list[tuple[Any, ...]] = []

        async def route(pattern: str, handler: Any) -> None:
            calls.append(("route", pattern, handler))

        async def unroute(pattern: str, handler: Any) -> None:
            calls.append(("unroute", pattern, handler))

        async def nav(url: str) -> None:
            calls.append(("navigate", url))
            if navigation_fails:
                raise LinkedInScraperException("navigation failed")

        mock_page.route = AsyncMock(side_effect=route)
        mock_page.unroute = AsyncMock(side_effect=unroute)
        capture = FakeCapture([_inbox(_conversation("2-a"))])
        reader = _reader(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.conversations.MessagingApiCapture",
                return_value=capture,
            ),
            patch.object(
                PageNavigator, "_navigate_to_page", AsyncMock(side_effect=nav)
            ),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("Ada Lovelace"),
            ),
        ):
            if navigation_fails:
                with pytest.raises(LinkedInScraperException):
                    await reader.get_inbox(limit=5)
            else:
                await reader.get_inbox(limit=5)

        handler = calls[0][2]
        conversations = "**/voyager/api/voyagerMessagingDashMessengerConversations*"
        badge = "**/voyager/api/voyagerMessagingDashMessagingBadge*"
        assert calls == [
            ("route", conversations, handler),
            ("route", badge, handler),
            ("navigate", "https://www.linkedin.com/messaging/"),
        ], "read-flag routes remain page-scoped after navigation"

    @pytest.mark.parametrize(
        ("method", "aborted"), [("POST", True), ("GET", False), ("OPTIONS", False)]
    )
    async def test_handler_aborts_only_post(self, mock_page, method, aborted):
        route = SimpleNamespace(
            request=SimpleNamespace(method=method),
            abort=AsyncMock(),
            fallback=AsyncMock(),
        )

        async with _reader(mock_page)._without_read_state_writes():
            handler = mock_page.route.await_args_list[0].args[1]
            await handler(cast(Route, route))

        assert route.abort.await_count == (1 if aborted else 0), (
            f"{method} read-flag request abort count"
        )
        assert route.fallback.await_count == (0 if aborted else 1), (
            f"{method} read-flag request continue count"
        )


class TestGetConversation:
    async def test_no_selector_is_an_invalid_reference(self, mock_page):
        """The caller named nobody, so this is a correctable argument, not a
        defect: `InvalidReferenceError` keeps issue-report diagnostics off it."""
        with pytest.raises(InvalidReferenceError, match="at least one"):
            await _reader(mock_page).get_conversation()

    async def test_direct_id_uses_one_inbox_navigation_and_no_thread_navigation(
        self, mock_page
    ):
        source = _conversation("2-source")
        target = _conversation("2-target", read=False)
        capture = FakeCapture(
            [_inbox(source, target)], messages=_messages("Hi", "Hello")
        )
        reader = _reader(mock_page)
        nav = AsyncMock()
        scroll = AsyncMock()
        with (
            patch(
                "linkedin_mcp_server.scraping.conversations.MessagingApiCapture",
                return_value=capture,
            ),
            patch.object(PageNavigator, "_navigate_to_page", nav),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(reader, "_scroll_main_scrollable_region", scroll),
        ):
            result = await reader.get_conversation(thread_id="2-target")

        nav.assert_awaited_once_with("https://www.linkedin.com/messaging/")
        scroll.assert_awaited_once_with(position="bottom", attempts=1, pause_time=0.5)
        assert result["url"] == "https://www.linkedin.com/messaging/thread/2-target/"
        conversation = result["sections"]["conversation"]
        assert conversation.count("sent the following message") == 2
        assert conversation.index("Hi") < conversation.index("Hello")
        assert capture.fetch_calls[0][0] is target
        assert target["read"] is False

    async def test_direct_id_preserves_message_references(self, mock_page):
        target = _conversation("2-target", read=False)
        messages = _messages("Attachment follows")
        messages["data"]["messengerMessagesBySyncToken"]["elements"][0][
            "attachments"
        ] = [{"url": "https://example.com/private-file"}]
        capture = FakeCapture([_inbox(target)], messages=messages)
        reader = _reader(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.conversations.MessagingApiCapture",
                return_value=capture,
            ),
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
        ):
            result = await reader.get_conversation(thread_id="2-target")

        refs = result["references"]["conversation"]
        assert any(ref["url"] == "/in/ada/" for ref in refs)
        assert any(ref["url"] == "https://example.com/private-file" for ref in refs)

    async def test_direct_id_outside_passive_results_fails_instead_of_guessing(
        self, mock_page
    ):
        capture = FakeCapture([_inbox(_conversation("2-source"))])
        reader = _reader(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.conversations.MessagingApiCapture",
                return_value=capture,
            ),
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ) as scroll,
        ):
            with pytest.raises(LinkedInScraperException, match="Could not map"):
                await reader.get_conversation(thread_id="2-older")

        assert capture.fetch_calls == []
        # The initial load scroll, then the list scrolls until it stops growing.
        assert scroll.await_count == 1 + 2

    async def test_username_uses_profile_identity_not_duplicate_display_name(
        self, mock_page
    ):
        namesake = _conversation("2-wrong", title="Alex Smith", username="other-alex")
        target = _conversation(
            "2-target", title="Alex Smith", username="target-alex", read=False
        )
        capture = FakeCapture([_inbox(), _inbox(namesake, target, search=True)])
        reader = _reader(mock_page)
        nav = AsyncMock()
        with (
            patch(
                "linkedin_mcp_server.scraping.conversations.MessagingApiCapture",
                return_value=capture,
            ),
            patch.object(PageNavigator, "_navigate_to_page", nav),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(reader, "_submit_search", new_callable=AsyncMock),
            patch.object(
                ProfilePageReader,
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value="Alex Smith",
            ),
        ):
            result = await reader.get_conversation(linkedin_username="target-alex")

        assert [call.args[0] for call in nav.await_args_list] == [
            "https://www.linkedin.com/in/target-alex/",
            "https://www.linkedin.com/messaging/",
        ]
        assert capture.fetch_calls[0][0] is target
        assert result["url"].endswith("/messaging/thread/2-target/")

    async def test_username_loads_inbox_when_search_emits_no_message_template(
        self, mock_page
    ):
        target = _conversation("2-target", username="ada", read=False)
        source = _conversation("2-source", username="grace")
        capture = FakeCapture(
            [_inbox(), _inbox(target, search=True), _inbox(source)],
            has_message_template=False,
        )
        reader = _reader(mock_page)
        nav = AsyncMock()
        with (
            patch(
                "linkedin_mcp_server.scraping.conversations.MessagingApiCapture",
                return_value=capture,
            ),
            patch.object(PageNavigator, "_navigate_to_page", nav),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(reader, "_submit_search", new_callable=AsyncMock),
            patch.object(
                ProfilePageReader,
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value="Ada Lovelace",
            ),
        ):
            await reader.get_conversation(linkedin_username="ada")

        assert [call.args[0] for call in nav.await_args_list] == [
            "https://www.linkedin.com/in/ada/",
            "https://www.linkedin.com/messaging/",
            "https://www.linkedin.com/messaging/",
        ]
        assert capture.fetch_calls[0][0] is target
        assert source in capture.fetch_calls[0][1]

    async def test_username_index_keeps_provider_order(self, mock_page):
        first = _conversation("2-first", username="ada")
        second = _conversation("2-second", username="ada")
        capture = FakeCapture([_inbox(), _inbox(first, second, search=True)])
        reader = _reader(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.conversations.MessagingApiCapture",
                return_value=capture,
            ),
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(reader, "_submit_search", new_callable=AsyncMock),
            patch.object(
                ProfilePageReader,
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value="Ada Lovelace",
            ),
        ):
            await reader.get_conversation(linkedin_username="ada", index=1)

        assert capture.fetch_calls[0][0] is second

    async def test_username_without_a_match_refuses_instead_of_guessing(
        self, mock_page
    ):
        stranger = _conversation("2-stranger", title="Grace Hopper", username="grace")
        capture = FakeCapture([_inbox(), _inbox(stranger, search=True)])
        reader = _reader(mock_page)
        nav = AsyncMock()
        with (
            patch(
                "linkedin_mcp_server.scraping.conversations.MessagingApiCapture",
                return_value=capture,
            ),
            patch.object(PageNavigator, "_navigate_to_page", nav),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(reader, "_submit_search", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                ProfilePageReader,
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value="Ada Lovelace",
            ),
        ):
            with pytest.raises(LinkedInScraperException) as error:
                await reader.get_conversation(linkedin_username="ada")

        assert "ada" in str(error.value)
        assert "thread_id" in str(error.value)
        assert capture.fetch_calls == []

    async def test_username_index_past_its_threads_refuses_instead_of_guessing(
        self, mock_page
    ):
        first = _conversation("2-first", username="ada")
        # A non-matching neighbour: the refusal counts the participant's own
        # threads, not every thread the inbox handed over.
        stranger = _conversation("2-second", title="Grace Hopper", username="grace")
        capture = FakeCapture([_inbox(), _inbox(first, stranger, search=True)])
        reader = _reader(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.conversations.MessagingApiCapture",
                return_value=capture,
            ),
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(reader, "_submit_search", new_callable=AsyncMock),
            patch.object(
                ProfilePageReader,
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value="Ada Lovelace",
            ),
        ):
            with pytest.raises(LinkedInScraperException) as error:
                await reader.get_conversation(linkedin_username="ada", index=1)

        assert "only 1 thread(s) exist for ada" in str(error.value)
        assert "thread_id" in str(error.value)
        assert capture.fetch_calls == []

    async def test_no_identifier_is_refused_before_page_work(self, mock_page):
        reader = _reader(mock_page)
        with patch.object(
            PageNavigator, "_navigate_to_page", new_callable=AsyncMock
        ) as nav:
            with pytest.raises(LinkedInScraperException, match="at least one"):
                await reader.get_conversation()
        nav.assert_not_awaited()

    async def test_invalid_thread_id_is_refused_before_navigation(self, mock_page):
        capture = FakeCapture([])
        reader = _reader(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.conversations.MessagingApiCapture",
                return_value=capture,
            ),
            patch.object(
                PageNavigator, "_navigate_to_page", new_callable=AsyncMock
            ) as nav,
        ):
            with pytest.raises(InvalidReferenceError):
                await reader.get_conversation(thread_id="../../feed")
        nav.assert_not_awaited()

    @pytest.mark.parametrize(
        "thread_id",
        [
            "new",
            "/messaging/thread/new/",
            "https://www.linkedin.com/messaging/thread/new/?recipient=ACoAAB",
            "/messaging/thread/%6Eew/",
        ],
    )
    async def test_compose_thread_is_rejected_before_navigation(
        self, mock_page, thread_id
    ):
        reader = _reader(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.conversations.MessagingApiCapture",
                return_value=FakeCapture([]),
            ),
            patch.object(
                PageNavigator, "_navigate_to_page", new_callable=AsyncMock
            ) as nav,
        ):
            with pytest.raises(InvalidReferenceError, match="new-message composer"):
                await reader.get_conversation(thread_id=thread_id)
        nav.assert_not_awaited()


class TestSearchConversations:
    async def _search(self, mock_page, capture, submit=None):
        reader = _reader(mock_page)
        submit = submit or AsyncMock()
        with (
            patch(
                "linkedin_mcp_server.scraping.conversations.MessagingApiCapture",
                return_value=capture,
            ),
            patch.object(PageNavigator, "_navigate_to_page", AsyncMock()),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(reader, "_submit_search", submit),
        ):
            return await reader.search_conversations("acme", limit=10), submit

    async def test_search_returns_only_the_search_payload_rows(self, mock_page):
        recent = _conversation("2-recent")
        hit = _conversation("2-acme", read=False)
        junk = _conversation("new")
        capture = FakeCapture([_inbox(recent), _inbox(hit, junk, search=True)])

        result, submit = await self._search(mock_page, capture)

        submit.assert_awaited_once_with("acme")
        urls = [r["url"] for r in result["references"]["search_results"]]
        assert urls == ["/messaging/thread/2-acme/"]
        assert "Ada Lovelace" in result["sections"]["search_results"]
        assert "2-recent" not in result["sections"]["search_results"]
        assert hit["read"] is False

    async def test_references_are_kept_when_a_row_has_no_text(self, mock_page):
        bare = {"conversationUrl": "https://www.linkedin.com/messaging/thread/2-bare/"}
        capture = FakeCapture([_inbox(), _inbox(bare, search=True)])

        result, _ = await self._search(mock_page, capture)

        assert result["sections"]["search_results"] == "/messaging/thread/2-bare/"
        assert result["references"]["search_results"][0]["url"] == (
            "/messaging/thread/2-bare/"
        )

    async def test_search_is_capped_at_the_limit(self, mock_page):
        rows = [_conversation(f"2-{i}") for i in range(3)]
        capture = FakeCapture([_inbox(), _inbox(*rows, search=True)])
        reader = _reader(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.conversations.MessagingApiCapture",
                return_value=capture,
            ),
            patch.object(PageNavigator, "_navigate_to_page", AsyncMock()),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(reader, "_submit_search", AsyncMock()),
        ):
            result = await reader.search_conversations("acme", limit=2)

        assert len(result["references"]["search_results"]) == 2

    async def test_an_empty_first_response_does_not_hide_the_populated_one(
        self, mock_page
    ):
        hit = _conversation("2-acme")
        capture = FakeCapture(
            [
                _inbox(_conversation("2-recent")),
                _inbox(search=True),
                _inbox(hit, search=True),
            ]
        )

        result, _ = await self._search(mock_page, capture)

        urls = [r["url"] for r in result["references"]["search_results"]]
        assert urls == ["/messaging/thread/2-acme/"]

    async def test_zero_results_is_an_explicit_empty_result(self, mock_page):
        capture = FakeCapture([_inbox(_conversation("2-recent")), _inbox(search=True)])

        result, _ = await self._search(mock_page, capture)

        assert result["sections"] == {}
        assert "references" not in result


class TestInboxFollowups:
    async def test_unread_row_carries_an_unread_marker_and_read_row_does_not(
        self, mock_page
    ):
        unread = _conversation("2-unread", title="Ada Lovelace", read=False)
        read = _conversation("2-read", title="Grace Hopper", username="grace")
        reader = _reader(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.conversations.MessagingApiCapture",
                return_value=FakeCapture([_inbox(unread, read)]),
            ),
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("rendered"),
            ),
        ):
            result = await reader.get_inbox(limit=2)

        first, second = result["sections"]["inbox"].split("\n\n")
        assert first.splitlines() == [
            "Ada Lovelace",
            "Unread",
            "/messaging/thread/2-unread/",
        ]
        assert "Unread" not in second

    async def test_progress_reports_loaded_over_limit_while_scrolling(self, mock_page):
        pages = [
            _inbox(*[_conversation(f"2-{i}") for i in range(n, n + 5)])
            for n in (0, 5, 10)
        ]
        reader = _reader(mock_page)
        callbacks = AsyncMock()
        with (
            patch(
                "linkedin_mcp_server.scraping.conversations.MessagingApiCapture",
                return_value=FakeCapture(pages),
            ),
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("inbox"),
            ),
        ):
            await reader.get_inbox(limit=10, callbacks=callbacks)

        assert [call.args for call in callbacks.on_progress.await_args_list] == [
            ("Loaded 5 of 10 conversations", 47),
            ("Loaded 10 of 10 conversations", 95),
        ]


class TestUsernameLookupUsesTheSearchBox:
    async def test_the_profile_name_is_typed_not_put_in_a_search_url(self, mock_page):
        target = _conversation("2-target", title="Alex Smith", username="target-alex")
        capture = FakeCapture([_inbox(), _inbox(target, search=True)])
        reader = _reader(mock_page)
        nav = AsyncMock()
        submit = AsyncMock()
        with (
            patch(
                "linkedin_mcp_server.scraping.conversations.MessagingApiCapture",
                return_value=capture,
            ),
            patch.object(PageNavigator, "_navigate_to_page", nav),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(reader, "_submit_search", submit),
            patch.object(
                ProfilePageReader,
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value="Alex Smith",
            ),
        ):
            await reader.get_conversation(linkedin_username="target-alex")

        submit.assert_awaited_once_with("Alex Smith")
        assert all("searchTerm" not in call.args[0] for call in nav.await_args_list)


class TestThreadIdBeyondTheFirstPage:
    async def test_scrolls_until_the_thread_row_loads_without_opening_it(
        self, mock_page
    ):
        pages = [_inbox(*[_conversation(f"2-{i}") for i in range(5)])]
        pages += [_inbox(_conversation("2-old", username="old"))]
        capture = FakeCapture(pages)
        reader = _reader(mock_page)
        nav = AsyncMock()
        with (
            patch(
                "linkedin_mcp_server.scraping.conversations.MessagingApiCapture",
                return_value=capture,
            ),
            patch.object(PageNavigator, "_navigate_to_page", nav),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
        ):
            result = await reader.get_conversation(thread_id="2-old")

        nav.assert_awaited_once_with("https://www.linkedin.com/messaging/")
        assert capture.fetch_calls[0][0]["backendUrn"].endswith("2-old")
        assert len(capture.fetch_calls[0][1]) == 6
        assert result["url"].endswith("/messaging/thread/2-old/")


class TestConversationReferencesExposeReadState:
    async def test_inbox_references_carry_read(self, mock_page):
        reader = _reader(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.conversations.MessagingApiCapture",
                return_value=FakeCapture(
                    [
                        _inbox(
                            _conversation("2-u", read=False),
                            _conversation("2-r", username="grace"),
                        )
                    ]
                ),
            ),
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("rendered"),
            ),
        ):
            result = await reader.get_inbox(limit=2)

        assert [ref["read"] for ref in result["references"]["inbox"]] == [False, True]


class TestReviewFollowups:
    async def test_read_row_with_a_stale_unread_count_has_no_marker(self, mock_page):
        row = _conversation("2-r", title="Ada Lovelace")
        row["unreadCount"] = 3
        reader = _reader(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.conversations.MessagingApiCapture",
                return_value=FakeCapture([_inbox(row)]),
            ),
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("rendered"),
            ),
        ):
            result = await reader.get_inbox(limit=1)

        assert "Unread" not in result["sections"]["inbox"]

    async def test_thread_id_scroll_budget_fits_the_default_tool_timeout(
        self, mock_page
    ):
        from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS

        reader = _reader(mock_page)
        load = AsyncMock()
        capture = FakeCapture([_inbox(_conversation("2-a"))])
        with (
            patch(
                "linkedin_mcp_server.scraping.conversations.MessagingApiCapture",
                return_value=capture,
            ),
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(reader, "_load_inbox_to_limit", load),
        ):
            with pytest.raises(LinkedInScraperException):
                await reader.get_conversation(thread_id="2-missing")

        assert load.await_args is not None
        assert load.await_args.kwargs["budget"] < DEFAULT_TOOL_TIMEOUT_SECONDS
