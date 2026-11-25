"""The read-state guard covers the Sales Navigator inbox's writes, and only those."""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from patchright._impl._glob import glob_to_regex_pattern

from linkedin_mcp_server.scraping.messaging_read_state import (
    _READ_STATE_WRITE_ROUTES,
    register_read_state_routes,
    without_read_state_writes,
)

SN = "https://www.linkedin.com/sales-api/"
WRITES = [
    "salesApiMessagingThreads/2-ZXhhbXBsZQ%3D%3D?action=markAsRead",
    "salesApiMessagingThreads/2-ZXhhbXBsZQ%3D%3D?action=markAsUnread",
    "salesApiMessagingThreads/2-ZXhhbXBsZQ%3D%3D",
    "salesApiMessagingThreads/2-ZXhhbXBsZQ%3D%3D/seenReceipts",
    "salesApiMessageActions?action=createMessage",
]
# The app's own POSTs that have nothing to do with read state.
BENIGN = [
    "salesApiWebAppBoot?action=recordBoot",
    "salesApiCoach?action=checkPreconditions",
]


class _Request:
    def __init__(self, url: str, method: str = "POST") -> None:
        self.url = url
        self.method = method


def _routed(url: str) -> list[str]:
    return [
        pattern
        for pattern in _READ_STATE_WRITE_ROUTES
        if re.match(glob_to_regex_pattern(pattern), url)
    ]


@pytest.mark.parametrize("path", WRITES)
def test_every_inbox_write_is_routed(path):
    assert _routed(SN + path)


@pytest.mark.parametrize("path", BENIGN)
def test_the_apps_other_posts_are_not_routed(path):
    assert _routed(SN + path) == []


async def _verdict(handler, request: _Request) -> str:
    route = SimpleNamespace(request=request, abort=AsyncMock(), fallback=AsyncMock())
    await handler(route)
    return "aborted" if route.abort.await_count else "passed"


async def test_a_read_aborts_the_mark_read_but_not_the_thread_get(mock_page):
    await register_read_state_routes(mock_page)
    handlers = {call.args[0]: call.args[1] for call in mock_page.route.await_args_list}
    handler = handlers["**/sales-api/salesApiMessagingThreads**"]
    mark_read = _Request(SN + WRITES[0])
    thread_get = _Request(SN + "salesApiMessagingThreads/2-x?count=1", method="GET")

    # Inert until a read latches it.
    assert await _verdict(handler, mark_read) == "passed"
    async with without_read_state_writes(mock_page):
        assert await _verdict(handler, mark_read) == "aborted"
        assert await _verdict(handler, thread_get) == "passed"
        assert (
            await _verdict(
                handlers["**/sales-api/salesApiMessageActions**"],
                _Request(SN + WRITES[-1]),
            )
            == "aborted"
        )
    # The latch outlives the read: a late mark-read is still aborted.
    assert await _verdict(handler, mark_read) == "aborted"
