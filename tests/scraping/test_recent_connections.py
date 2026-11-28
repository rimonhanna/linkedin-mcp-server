"""Paging the connections page back to a date, against a scripted page."""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.exceptions import ActionLimitError
from linkedin_mcp_server.scraping import recent_connections as module
from linkedin_mcp_server.scraping.recent_connections import RecentConnectionsReader
from linkedin_mcp_server.scraping.recent_connections_parse import (
    CONNECTIONS_URL,
    PAGINATION_PATH,
    ROWS_JS,
)

NEWEST = date(2026, 11, 27)
THROTTLE = MagicMock()


def _row(index: int, day: date | None, *, text: str | None = None) -> dict[str, Any]:
    if text is None and day is not None:
        text = f"Connected on November {day.day}, {day.year}"
    return {
        "publicIdentifier": f"fake-{index}",
        "profileIds": [f"ACoAAFAKE{index:031d}"],
        "label": f"Fake Person {index}\n\nFake headline {index}",
        "loose": [text] if text else [],
    }


def _rows(count: int, *, per_day: int = 4) -> list[dict[str, Any]]:
    """``count`` rows newest first, ``per_day`` connections a day."""
    return [_row(i, NEWEST - timedelta(days=i // per_day)) for i in range(count)]


class FakePage:
    """Ten rows per page; each wheel pass answers one pagination request."""

    def __init__(
        self, rows: list[dict[str, Any]], *, statuses: list[int] | None = None
    ):
        self.all_rows = rows
        self.shown = min(10, len(rows))
        self.statuses = list(statuses or [])
        self.url = CONNECTIONS_URL
        self.listeners: list[Any] = []
        self.scrolls = 0
        self.per_answer = 10
        self.mouse = SimpleNamespace(
            move=AsyncMock(), wheel=AsyncMock(side_effect=self._wheel)
        )
        self._wheels = 0

    def on(self, event: str, handler: Any) -> None:
        assert event == "response"
        self.listeners.append(handler)

    def remove_listener(self, event: str, handler: Any) -> None:
        self.listeners.remove(handler)

    async def evaluate(self, script: str, *args: Any) -> Any:
        if script == ROWS_JS:
            return self.all_rows[: self.shown]
        return [1280, 720]

    async def _wheel(self, *_args: Any) -> None:
        self._wheels += 1
        if self._wheels % 2:
            return
        self.scrolls += 1
        status = self.statuses.pop(0) if self.statuses else 200
        if status == 200:
            self.shown = min(self.shown + self.per_answer, len(self.all_rows))
        response = SimpleNamespace(
            url=f"https://www.linkedin.com{PAGINATION_PATH}?x=1", status=status
        )
        for handler in list(self.listeners):
            handler(response)


def _reader(page: FakePage, *, charge: Any = None):
    session = SimpleNamespace(
        page=page,
        delay=AsyncMock(),
        pace=AsyncMock(),
        jittered=lambda seconds: seconds,
        check_rate_limit=AsyncMock(),
    )
    navigator = SimpleNamespace(
        _navigate_to_page=AsyncMock(), _charge_navigation=MagicMock(side_effect=charge)
    )
    return RecentConnectionsReader(cast(Any, session), cast(Any, navigator)), navigator


@pytest.fixture(autouse=True)
def _no_ledger(monkeypatch):
    """Keep the account ledger on disk out of every case."""
    monkeypatch.setattr(module, "cooldown_resume_at", lambda *_a: None)
    monkeypatch.setattr(module, "note_throttle_signal", THROTTLE)
    THROTTLE.reset_mock()
    monkeypatch.setattr(module, "_PAGE_TIMEOUT", 0.05)
    monkeypatch.setattr(module, "_READY_TIMEOUT", 0.05)


async def test_paging_stops_at_the_first_connection_older_than_since():
    page = FakePage(_rows(60))
    reader, navigator = _reader(page)

    # Four a day: rows 0-11 are 27-25 Nov, row 12 is the first of 24 Nov.
    result = await reader.list_recent(date(2026, 11, 25), limit=100)

    assert [c["publicIdentifier"] for c in result["connections"]] == [
        f"fake-{i}" for i in range(12)
    ]
    assert result["paging"] == {
        "count": 12,
        "pages": 2,
        "more": False,
        "reason": "since",
    }
    assert page.scrolls == 1
    navigator._navigate_to_page.assert_awaited_once_with(CONNECTIONS_URL)
    # Every page past the first is charged as a page load before it is asked for.
    navigator._charge_navigation.assert_called_once_with(CONNECTIONS_URL)
    assert not page.listeners


async def test_one_charge_per_page_read():
    page = FakePage(_rows(60, per_day=100))
    reader, navigator = _reader(page)

    result = await reader.list_recent(date(2026, 11, 1), limit=35)

    assert result["paging"]["reason"] == "limit"
    assert result["paging"]["more"] is True
    assert result["paging"]["count"] == 35
    assert page.scrolls == 3
    assert navigator._charge_navigation.call_count == 3
    assert result["paging"]["pages"] == 4


async def test_a_429_stops_paging_and_is_recorded():
    page = FakePage(_rows(60, per_day=100), statuses=[429])
    reader, _ = _reader(page)

    result = await reader.list_recent(date(2026, 11, 1))

    assert result["paging"]["reason"] == "rate_limited"
    assert result["section_errors"]["connections"]["error_type"] == "rate_limit"
    assert result["paging"]["count"] == 10
    assert page.scrolls == 1
    THROTTLE.assert_called_once_with("http_429")


async def test_a_cooldown_stops_before_the_next_page_is_asked_for(monkeypatch):
    until = datetime(2026, 11, 27, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(module, "cooldown_resume_at", lambda *_a: until)
    page = FakePage(_rows(60, per_day=100))
    reader, navigator = _reader(page)

    result = await reader.list_recent(date(2026, 11, 1))

    assert (
        result["paging"]["reason"] == "account_cooldown until 2026-11-27T15:00:00+00:00"
    )
    assert page.scrolls == 0
    navigator._charge_navigation.assert_not_called()


async def test_a_pacing_cap_stops_before_the_next_page_is_asked_for():
    refusal = ActionLimitError(
        "other", limit=150, window="24 h", resume_at=datetime(2026, 11, 28, 9, 0)
    )
    page = FakePage(_rows(60, per_day=100))
    reader, _ = _reader(page, charge=refusal)

    result = await reader.list_recent(date(2026, 11, 1))

    assert result["paging"]["reason"].startswith("action_limit: ")
    assert result["paging"]["count"] == 10
    assert page.scrolls == 0


async def test_the_end_of_the_list_is_complete():
    page = FakePage(_rows(14, per_day=100))
    reader, _ = _reader(page)

    result = await reader.list_recent(date(2026, 11, 1))

    assert result["paging"] == {
        "count": 14,
        "pages": 3,
        "more": False,
        "reason": "complete",
    }


async def test_too_little_time_left_reads_no_further_page():
    page = FakePage(_rows(60, per_day=100))
    reader, navigator = _reader(page)

    result = await reader.list_recent(date(2026, 11, 1), deadline=time.monotonic())

    assert result["paging"]["reason"] == "deadline"
    assert result["paging"]["more"] is True
    navigator._charge_navigation.assert_not_called()


async def test_an_unreadable_date_stops_after_the_first_page():
    rows = [_row(i, None, text="Verbonden op 26 november 2026") for i in range(30)]
    page = FakePage(rows)
    reader, navigator = _reader(page)

    result = await reader.list_recent(date(2026, 11, 1))

    assert result["paging"]["reason"] == "date_unreadable"
    assert result["paging"]["count"] == 10
    assert result["section_errors"]["connected_on"]["error_type"] == "date_unreadable"
    assert all(
        c["connectedText"].startswith("Verbonden") for c in result["connections"]
    )
    navigator._charge_navigation.assert_not_called()


async def test_a_page_that_loads_nothing_new_is_reported_not_loaded():
    page = FakePage(_rows(60, per_day=100))
    page.mouse.wheel = AsyncMock()  # the list never answers
    reader, navigator = _reader(page)

    result = await reader.list_recent(date(2026, 11, 1))

    assert result["paging"]["reason"] == "page_not_loaded"
    assert result["paging"]["count"] == 10
    assert navigator._charge_navigation.call_count == module._EMPTY_PASSES


async def test_a_pass_that_lands_two_pages_is_charged_for_both():
    page = FakePage(_rows(60, per_day=100))
    page.per_answer = 20
    reader, navigator = _reader(page)

    result = await reader.list_recent(date(2026, 11, 1), limit=30)

    assert page.scrolls == 1
    assert navigator._charge_navigation.call_count == 2
    assert result["paging"]["pages"] == 3
