"""Sales Navigator inbox reads over a scripted fake page (synthetic data)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
    LinkedInScraperException,
)
from linkedin_mcp_server.scraping import sales_navigator_inbox as inbox_module
from linkedin_mcp_server.scraping.identifiers import (
    SALES_INBOX_URL,
    sales_inbox_thread_url,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.sales_navigator import SalesNavigatorReader
from linkedin_mcp_server.scraping.sales_navigator_inbox import (
    SalesNavigatorInboxReader,
)
from linkedin_mcp_server.scraping.session import ScrapingSession
from scraping.support.navigation import with_document_identity

from .test_sales_navigator import _boot, _Browser, _response

VIEWER = "urn:li:fs_salesProfile:(ACwAAA0000,NAME_SEARCH,v0)"
HEADERS = {
    "csrf-token": "ajax:0000000000000000000",
    "x-restli-protocol-version": "2.0.0",
    "cookie": "never-forwarded",
}


def _lead(n: int) -> str:
    return f"urn:li:fs_salesProfile:(ACwAAA{n:04d},NAME_SEARCH,t{n})"


def _profiles(*leads: int) -> list[dict[str, Any]]:
    return [
        {"entityUrn": VIEWER, "fullName": "Example Viewer", "degree": 0},
        *(
            {
                "entityUrn": _lead(n),
                "objectUrn": f"urn:li:member:1000000{n:02d}",
                "fullName": f"Example Person {n}",
                "degree": 2,
            }
            for n in leads
        ),
    ]


def _message(message_id: str, delivered: int, author: str) -> dict[str, Any]:
    return {
        "id": message_id,
        "type": "MESSAGE",
        "author": author,
        "body": f"Body {message_id}",
        "deliveredAt": delivered,
    }


def _thread(n: int, *, lead: int | None = None, unread: int = 0) -> dict[str, Any]:
    lead = n if lead is None else lead
    return {
        "id": f"2-THREAD{n:04d}==",
        "archived": False,
        "unreadMessageCount": unread,
        "totalMessageCount": 3,
        "nextPageStartsAt": 1_790_000_000_000 - n,
        "participants": [VIEWER, _lead(lead)],
        "messages": [_message(f"m{n}", 1_790_000_000_000 - n, _lead(lead))],
    }


def _list(
    threads: list[dict[str, Any]],
    *,
    finder: str = "filter",
    value: str = "INBOX",
    count: int = 20,
    leads: tuple[int, ...] | None = None,
) -> SimpleNamespace:
    key = "filter" if finder == "filter" else "keyword"
    if leads is None:
        leads = tuple(int(t["participants"][1].split("ACwAAA")[1][:4]) for t in threads)
    return _response(
        f"salesApiMessagingThreads?q={finder}&{key}={value}&count={count}"
        "&pageStartsAt=1790000000000&decoration=x",
        {"data": {"elements": threads, "paging": {}}, "included": _profiles(*leads)},
    )


def _thread_response(record: dict[str, Any], *leads: int) -> SimpleNamespace:
    encoded = record["id"].replace("=", "%3D")
    response = _response(
        f"salesApiMessagingThreads/{encoded}?count=1&messageCount=10&decoration=x",
        {"data": record, "included": _profiles(*leads)},
    )
    response.request.all_headers = AsyncMock(return_value=HEADERS)
    return response


class _Inbox(_Browser):
    """The fake browser, plus the list scroller, the search box and fetch."""

    def __init__(self, page: Any) -> None:
        super().__init__(page)
        self.scrolls: list[list[SimpleNamespace]] = []
        self.searched: dict[str, list[SimpleNamespace]] = {}
        self.history: list[dict[str, Any]] = []
        self.fetched: list[dict[str, Any]] = []
        self.typed: list[str] = []
        self.scrollable = True
        page.evaluate = with_document_identity(
            page, AsyncMock(side_effect=self._evaluate)
        )
        box = MagicMock()
        box.first = box
        box.click = AsyncMock()
        page.locator = MagicMock(return_value=box)
        page.keyboard.press = AsyncMock()

    def emit(self, response: SimpleNamespace) -> None:
        # The guard's own listener holds requests weakly; a POST here is one
        # that already got past it, so only GETs are announced.
        listeners = self.page.listeners.get("request", [])
        for callback in list(listeners if response.request.method == "GET" else []):
            callback(response.request)
        for callback in list(self.page.listeners.get("response", [])):
            callback(response)

    async def navigate(self, url: str) -> None:
        self.events.append(f"load {url}")
        self.page.url = self.landings.get(url, url)
        for response in self.loads.get(url, []):
            self.emit(response)

    async def _evaluate(self, program: str, arg: Any = None) -> Any:
        if "linkedin-mcp:sn-inbox-scroll" in program:
            self.events.append("scroll")
            if not self.scrollable:
                return False
            for response in self.scrolls.pop(0) if self.scrolls else []:
                self.emit(response)
            return True
        if "linkedin-mcp:sn-inbox-history" in program:
            self.fetched.append(arg)
            return self.history.pop(0)
        raise AssertionError(f"unexpected program {program[:60]!r}")

    async def type(self, page: Any, text: str, **_: Any) -> None:
        self.typed.append(text)
        for response in self.searched.get(text, []):
            self.emit(response)

    @contextmanager
    def running(self) -> Iterator[None]:
        with (
            super().running(),
            patch.object(ScrapingSession, "delay", new_callable=AsyncMock),
            patch.object(inbox_module, "_PAGE_WAIT", 0.05),
            patch.object(inbox_module, "_SEARCH_WAIT", 0.2),
            patch.object(inbox_module, "human_type", self.type),
        ):
            yield


async def _run(
    page: Any, browser: _Inbox, call: Callable[[SalesNavigatorInboxReader], Any]
) -> Any:
    session = ScrapingSession(page)
    navigator = PageNavigator(session)
    reader = SalesNavigatorInboxReader(
        session, navigator, SalesNavigatorReader(session, navigator, browser.facets)
    )
    with browser.running():
        return await call(reader)


# --- get_inbox ----------------------------------------------------------------


async def test_inbox_reads_the_first_page_without_scrolling(mock_page):
    browser = _Inbox(mock_page)
    browser.loads[SALES_INBOX_URL] = [
        *_boot(),
        _list([_thread(1, unread=2), _thread(2)]),
    ]

    result = await _run(mock_page, browser, lambda r: r.get_inbox(limit=2))

    assert browser.navigated == [SALES_INBOX_URL]
    assert "scroll" not in browser.events
    assert [row["id"] for row in result["conversations"]] == [
        "2-THREAD0001==",
        "2-THREAD0002==",
    ]
    assert result["conversations"][0]["participants"] == [
        {
            "leadId": "ACwAAA0001",
            "memberUrn": "urn:li:member:100000001",
            "name": "Example Person 1",
            "degree": 2,
        }
    ]
    assert result["conversations"][0]["unread"] is True
    assert result["paging"] == {"count": 2, "more": True, "reason": "limit"}
    assert result["marked_read"] is False
    assert "section_errors" not in result
    assert len(result["sections"]["inbox"].splitlines()) == 2


async def test_inbox_scrolls_for_the_next_page_and_stops_at_a_short_page(mock_page):
    browser = _Inbox(mock_page)
    browser.loads[SALES_INBOX_URL] = [
        *_boot(),
        _list([_thread(n) for n in range(1, 21)]),
    ]
    # The second page repeats the last row of the first, and is short.
    browser.scrolls = [[_list([_thread(n) for n in range(20, 26)])]]

    result = await _run(mock_page, browser, lambda r: r.get_inbox(limit=100))

    assert browser.events.count("scroll") == 1
    ids = [row["id"] for row in result["conversations"]]
    assert len(ids) == 25 == len(set(ids))
    assert result["paging"] == {"count": 25, "more": False, "reason": "complete"}
    assert "section_errors" not in result


async def test_inbox_reports_a_stalled_list_as_a_partial_read(mock_page):
    browser = _Inbox(mock_page)
    browser.loads[SALES_INBOX_URL] = [
        *_boot(),
        _list([_thread(n) for n in range(1, 21)]),
    ]

    result = await _run(mock_page, browser, lambda r: r.get_inbox(limit=40))

    assert browser.events.count("scroll") == 2
    assert result["paging"] == {"count": 20, "more": True, "reason": "no_new_rows"}
    assert result["section_errors"]["inbox"] == {
        "error_type": "page_limit",
        "error_message": "20 conversations were read; Sales Navigator loaded no "
        "more conversations after two scrolls, so the rest were not read.",
    }


async def test_inbox_stops_before_a_scroll_past_the_deadline(mock_page):
    browser = _Inbox(mock_page)
    browser.loads[SALES_INBOX_URL] = [
        *_boot(),
        _list([_thread(n) for n in range(1, 21)]),
    ]

    result = await _run(
        mock_page, browser, lambda r: r.get_inbox(limit=40, deadline=0.0)
    )

    assert "scroll" not in browser.events
    assert result["paging"]["reason"] == "time_budget"
    assert "ran out of time" in result["section_errors"]["inbox"]["error_message"]


async def test_inbox_filter_reads_only_its_own_view(mock_page):
    browser = _Inbox(mock_page)
    url = f"{SALES_INBOX_URL}?filter=PENDING"
    browser.loads[url] = [
        *_boot(),
        # The app's other view (a stale "all" call) is not this filter's data.
        _list([_thread(9)]),
        _list([_thread(1)], value="INMAIL_PENDING"),
    ]

    result = await _run(
        mock_page, browser, lambda r: r.get_inbox(limit=5, filter="inmail_pending")
    )

    assert browser.navigated == [url]
    assert [row["id"] for row in result["conversations"]] == ["2-THREAD0001=="]
    assert result["paging"]["reason"] == "complete"


async def test_inbox_refuses_a_row_it_cannot_read(mock_page):
    browser = _Inbox(mock_page)
    broken = _thread(2)
    del broken["unreadMessageCount"]
    browser.loads[SALES_INBOX_URL] = [*_boot(), _list([_thread(1), broken])]

    with pytest.raises(LinkedInScraperException, match="does not recognise"):
        await _run(mock_page, browser, lambda r: r.get_inbox(limit=5))


async def test_inbox_off_the_inbox_path_is_the_no_seat_message(mock_page):
    browser = _Inbox(mock_page)
    browser.landings[SALES_INBOX_URL] = "https://www.linkedin.com/premium/products/"

    with pytest.raises(LinkedInScraperException, match="no Sales Navigator seat"):
        await _run(mock_page, browser, lambda r: r.get_inbox(limit=5))


async def test_inbox_reports_a_mark_read_linkedin_confirmed(mock_page):
    browser = _Inbox(mock_page)
    browser.loads[SALES_INBOX_URL] = [
        *_boot(),
        _list([_thread(1, unread=1)], count=20),
        _response(
            "salesApiMessagingThreads/2-THREAD0001%3D%3D?action=markAsRead",
            method="POST",
        ),
    ]

    result = await _run(mock_page, browser, lambda r: r.get_inbox(limit=5))

    assert result["marked_read"] is True


# --- get_conversation ---------------------------------------------------------


async def test_conversation_by_thread_id_opens_its_route(mock_page):
    browser = _Inbox(mock_page)
    record = _thread(1, unread=1)
    record["messages"] = [
        _message("m2", 2_000, VIEWER),
        _message("m1", 1_000, _lead(1)),
    ]
    url = sales_inbox_thread_url(record["id"])
    browser.loads[url] = [*_boot(), _thread_response(record, 1)]

    result = await _run(
        mock_page,
        browser,
        lambda r: r.get_conversation(thread_id=f"/sales/inbox/{record['id']}"),
    )

    assert browser.navigated == [url]
    conversation = result["conversation"]
    assert conversation["id"] == record["id"]
    assert [m["id"] for m in conversation["messages"]] == ["m1", "m2"]
    assert [m["fromViewer"] for m in conversation["messages"]] == [False, True]
    assert all("deliveredAt" not in m for m in conversation["messages"])
    assert conversation["unread"] is True
    assert conversation["history"] == {"batches": 1, "complete": True}
    assert browser.fetched == []
    assert result["marked_read"] is False
    assert result["sections"]["conversation"].startswith(
        "Conversation with Example Person 1"
    )


async def test_conversation_reads_older_batches_with_the_pages_headers(mock_page):
    browser = _Inbox(mock_page)
    record = _thread(1)
    record["messages"] = [_message(f"n{i}", 10_000 + i, _lead(1)) for i in range(10)]
    url = sales_inbox_thread_url(record["id"])
    browser.loads[url] = [*_boot(), _thread_response(record, 1)]
    browser.history = [
        {
            "status": 200,
            "payload": {
                "data": {
                    "elements": [
                        _message(f"o{i}", 5_000 + i, _lead(1)) for i in range(10)
                    ]
                }
            },
        },
        {
            "status": 200,
            "payload": {"data": {"elements": [_message("p0", 1_000, VIEWER)]}},
        },
    ]

    result = await _run(
        mock_page, browser, lambda r: r.get_conversation(thread_id=record["id"])
    )

    messages = result["conversation"]["messages"]
    assert len(messages) == 21
    assert messages[0]["id"] == "p0"
    assert result["conversation"]["history"] == {"batches": 3, "complete": True}
    first, second = browser.fetched
    assert first["headers"] == {
        "csrf-token": HEADERS["csrf-token"],
        "x-restli-protocol-version": "2.0.0",
    }
    query = parse_qs(urlparse(first["url"]).query)
    assert urlparse(first["url"]).path == (
        "/sales-api/salesApiMessagingThreads/2-THREAD0001%3D%3D/messages"
    )
    assert query["deliveredBefore"] == ["10000"]
    assert query["count"] == ["10"]
    assert parse_qs(urlparse(second["url"]).query)["deliveredBefore"] == ["5000"]


async def test_conversation_keeps_what_it_read_when_older_messages_fail(mock_page):
    browser = _Inbox(mock_page)
    record = _thread(1)
    record["messages"] = [_message(f"n{i}", 10_000 + i, _lead(1)) for i in range(10)]
    url = sales_inbox_thread_url(record["id"])
    browser.loads[url] = [*_boot(), _thread_response(record, 1)]
    browser.history = [{"status": 403, "payload": None}]

    result = await _run(
        mock_page, browser, lambda r: r.get_conversation(thread_id=record["id"])
    )

    assert len(result["conversation"]["messages"]) == 10
    assert result["conversation"]["history"] == {"batches": 1, "complete": False}


async def test_conversation_for_an_unknown_thread_is_refused(mock_page):
    browser = _Inbox(mock_page)
    url = sales_inbox_thread_url("2-GONE")
    browser.loads[url] = [
        *_boot(),
        _response(
            "salesApiMessagingThreads/2-GONE?count=1&messageCount=10", status=404
        ),
    ]

    with pytest.raises(InvalidReferenceError, match="refused that thread_id"):
        await _run(mock_page, browser, lambda r: r.get_conversation(thread_id="2-GONE"))


async def test_conversation_reports_its_own_thread_marked_read(mock_page):
    browser = _Inbox(mock_page)
    record = _thread(1, unread=1)
    url = sales_inbox_thread_url(record["id"])
    browser.loads[url] = [
        *_boot(),
        _thread_response(record, 1),
        _response(
            "salesApiMessagingThreads/2-THREAD0001%3D%3D?action=markAsRead",
            method="POST",
        ),
    ]

    result = await _run(
        mock_page, browser, lambda r: r.get_conversation(thread_id=record["id"])
    )

    assert result["marked_read"] is True


async def test_conversation_by_lead_matches_the_lead_id_only(mock_page):
    browser = _Inbox(mock_page)
    # Thread 1 is with another lead who has the same display name.
    other = _thread(1, lead=7)
    mine = [_thread(2, lead=5), _thread(3, lead=5)]
    listing = _list([other, *mine], leads=(5, 7))
    listing.json.return_value["included"][2]["fullName"] = "Example Person 5"
    browser.loads[SALES_INBOX_URL] = [*_boot(), listing]
    target = mine[1]
    url = sales_inbox_thread_url(target["id"])
    browser.loads[url] = [*_boot(), _thread_response(target, 5)]

    result = await _run(
        mock_page,
        browser,
        lambda r: r.get_conversation(
            lead_id="urn:li:fs_salesProfile:(ACwAAA0005,NAME_SEARCH,x)", index=1
        ),
    )

    assert browser.navigated == [SALES_INBOX_URL, url]
    assert result["conversation"]["id"] == target["id"]


async def test_conversation_by_lead_refuses_an_index_past_the_matches(mock_page):
    browser = _Inbox(mock_page)
    browser.loads[SALES_INBOX_URL] = [*_boot(), _list([_thread(2, lead=5)])]

    with pytest.raises(LinkedInScraperException, match="index 3 out of range"):
        await _run(
            mock_page,
            browser,
            lambda r: r.get_conversation(lead_id="ACwAAA0005", index=3),
        )
    assert browser.navigated == [SALES_INBOX_URL]


async def test_conversation_by_lead_refuses_a_lead_with_no_thread(mock_page):
    browser = _Inbox(mock_page)
    browser.loads[SALES_INBOX_URL] = [*_boot(), _list([_thread(2)])]

    with pytest.raises(LinkedInScraperException, match="No Sales Navigator"):
        await _run(
            mock_page, browser, lambda r: r.get_conversation(lead_id="ACwAAA0042")
        )
    assert browser.navigated == [SALES_INBOX_URL]


# --- search_conversations -----------------------------------------------------


async def test_search_types_into_the_inbox_box_and_reads_the_full_keyword(mock_page):
    browser = _Inbox(mock_page)
    browser.loads[SALES_INBOX_URL] = [*_boot(), _list([_thread(9)])]
    browser.searched["example words"] = [
        # A debounced keystroke answers first, for part of the words.
        _list([_thread(8)], finder="keyword", value="example"),
        _list([_thread(1), _thread(2)], finder="keyword", value="example%20words"),
    ]

    result = await _run(
        mock_page,
        browser,
        lambda r: r.search_conversations("  example   words ", limit=5),
    )

    assert browser.typed == ["example words"]
    mock_page.locator.assert_called_with("#left-rail-inbox-search")
    mock_page.keyboard.press.assert_not_awaited()
    assert [row["id"] for row in result["conversations"]] == [
        "2-THREAD0001==",
        "2-THREAD0002==",
    ]
    assert result["url"] == SALES_INBOX_URL
    assert result["paging"]["reason"] == "complete"
    assert "search_results" in result["sections"]


async def test_search_with_no_answer_for_the_keywords_fails(mock_page):
    browser = _Inbox(mock_page)
    browser.loads[SALES_INBOX_URL] = [*_boot(), _list([_thread(9)])]

    with pytest.raises(LinkedInScraperException, match="did not return"):
        await _run(mock_page, browser, lambda r: r.search_conversations("example"))
