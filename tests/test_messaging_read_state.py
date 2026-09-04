"""Behavioral regression coverage for messaging read-state preservation."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor


@pytest.fixture
def extractor():
    """Build an extractor with only browser I/O replaced."""
    page = MagicMock()
    page.url = "https://www.linkedin.com/messaging/"
    page.wait_for_selector = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.keyboard.press = AsyncMock()
    return LinkedInExtractor(page)


@pytest.mark.parametrize("unread", [True, False])
async def test_enumeration_restores_only_originally_unread(extractor, unread):
    """Restore unread rows without mutating already-read rows."""
    extractor._page.evaluate = AsyncMock(
        side_effect=[
            [
                {
                    "ariaLabel": "Select conversation with Ada",
                    "rowKey": "row-2",
                    "wasUnread": unread,
                }
            ],
            "thread-a",
        ]
    )
    with patch.object(
        extractor, "_restore_selected_conversation_unread", new_callable=AsyncMock
    ) as restore:
        refs = await extractor._extract_conversation_thread_refs(10, "inbox")
    assert refs == [
        {
            "kind": "conversation",
            "url": "/messaging/thread/thread-a/",
            "context": "inbox",
            "text": "Ada",
        }
    ]
    assert restore.await_count == int(unread)
    if unread:
        restore.assert_awaited_once_with("thread-a")


async def test_unresolved_url_is_repaired_before_leaving_row(extractor):
    """Repair an unresolved selection before another row can be visited."""
    extractor._page.evaluate = AsyncMock(
        side_effect=[
            [
                {
                    "ariaLabel": "Select conversation with Ada",
                    "rowKey": "row-2",
                    "wasUnread": True,
                }
            ],
            None,
        ]
    )
    with patch.object(
        extractor, "_restore_unresolved_conversation_row", new_callable=AsyncMock
    ) as restore:
        await extractor._extract_conversation_thread_refs(10, "inbox")
    restore.assert_awaited_once_with("row-2")


@pytest.mark.parametrize(
    "error", [RuntimeError("extract failed"), asyncio.CancelledError()]
)
async def test_guard_repairs_selected_thread_on_failure(extractor, error):
    """Run cleanup when extraction raises or is cancelled."""
    extractor._page.url = "https://www.linkedin.com/messaging/thread/thread-a/"
    extractor._conversation_restore_pending.add("thread-a")
    with patch.object(
        extractor, "_restore_selected_conversation_unread", new_callable=AsyncMock
    ) as restore:
        with pytest.raises(type(error)):
            async with extractor._preserve_messaging_read_state():
                raise error
    restore.assert_awaited_once_with("thread-a")


async def test_unknown_direct_thread_fails_before_opening(extractor):
    """Refuse direct navigation without evidence of the prior read state."""
    with (
        patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock) as nav,
        patch.object(extractor, "_wait_for_main_text", new_callable=AsyncMock),
        patch.object(
            extractor, "_scroll_main_scrollable_region", new_callable=AsyncMock
        ),
        patch.object(
            extractor,
            "_extract_conversation_thread_refs",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
            new_callable=AsyncMock,
        ),
        patch(
            "linkedin_mcp_server.scraping.extractor.handle_modal_close",
            new_callable=AsyncMock,
        ),
    ):
        with pytest.raises(Exception, match="prior read state"):
            await extractor.get_conversation(thread_id="unknown")
    assert all("/thread/" not in call.args[0] for call in nav.await_args_list)


async def test_restore_refuses_a_thread_with_the_same_prefix(extractor):
    """Require an exact thread identity before opening any action menu."""
    extractor._page.url = "https://www.linkedin.com/messaging/thread/thread-ab/"
    with patch.object(
        extractor, "_current_conversation_unread_state", new_callable=AsyncMock
    ) as inspect:
        with pytest.raises(Exception, match="Could not restore"):
            await extractor._restore_selected_conversation_unread("thread-a")
    inspect.assert_not_awaited()


async def test_anyio_timeout_does_not_interrupt_restoration(extractor):
    """Complete cleanup within a cancelled AnyIO tool scope."""
    repaired = []

    async def restore(thread_id):
        await anyio.sleep(0)
        repaired.append(thread_id)

    extractor._page.url = "https://www.linkedin.com/messaging/thread/thread-a/"
    with patch.object(
        extractor, "_restore_selected_conversation_unread", side_effect=restore
    ):
        with anyio.CancelScope() as scope:
            async with extractor._preserve_messaging_read_state():
                extractor._conversation_restore_pending.add("thread-a")
                scope.cancel()
                await anyio.sleep(0)
    assert repaired == ["thread-a"]


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("get_inbox", {}),
        ("search_conversations", {"keywords": "test"}),
        ("get_conversation", {"thread_id": "thread-a"}),
    ],
)
async def test_every_messaging_reader_enters_protection(extractor, method, args):
    """Exercise guard entry instead of relying on a capability marker alone."""

    class Entered(Exception):
        pass

    @asynccontextmanager
    async def guard():
        raise Entered()
        yield

    with patch.object(extractor, "_preserve_messaging_read_state", guard):
        with pytest.raises(Entered):
            await getattr(extractor, method)(**args)
