"""Policy trace of list_recent_connections: one load, one scroll, stop at since."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from linkedin_mcp_server.scraping import recent_connections as recent_module
from linkedin_mcp_server.scraping.recent_connections_parse import PAGINATION_PATH

from .policy_scenarios import _COMMON_ALLOWED, _extractor, _page, boundaries
from .support.policy_trace import FakeClock, TraceRecorder


def _row(index: int, day: str) -> dict[str, Any]:
    return {
        "publicIdentifier": f"example-person-{index}",
        "profileIds": [f"ACoAAExample{index:02d}"],
        "label": f"Example Person {index}\n\nExample headline",
        "loose": [f"Connected on {day}"],
    }


async def recent_connections_scenario() -> dict[str, Any]:
    name = "list_recent_connections__since"
    page = _page(TraceRecorder(name, _COMMON_ALLOWED))
    first = [
        _row(i, "November 27, 2026" if i < 5 else "November 26, 2026")
        for i in range(10)
    ]
    second = first + [
        _row(i, "November 26, 2026" if i < 13 else "November 25, 2026")
        for i in range(10, 20)
    ]
    answer = SimpleNamespace(
        url=f"https://www.linkedin.com{PAGINATION_PATH}?sduiid=example", status=200
    )
    page.script("evaluate:recent_connections_rows", first, second)
    page.script("mouse.wheel", None, lambda: page.emit("response", answer))
    recorder = page.recorder
    extractor = _extractor(page)
    async with boundaries(recorder, FakeClock(recorder)):
        with (
            # The cooldown is the home directory's; a scripted page has none.
            patch.object(recent_module, "cooldown_resume_at", lambda *_a: None),
            recorder.context("list_recent_connections", name),
        ):
            result = await extractor.list_recent_connections(
                date(2026, 11, 26), limit=100
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "list_recent_connections",
            "arguments": {"since": "2026-11-26", "limit": 100},
        },
        result,
    )
