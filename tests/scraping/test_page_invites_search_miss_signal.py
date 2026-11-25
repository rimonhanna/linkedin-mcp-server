"""A name search that times out counts against the account only when nothing
answered it.

The dialog's search fires one typeahead request per keystroke. A wait that
times out while LinkedIn answered, only for other text than was typed, is a
mismatch on this side: it is logged and records no throttle signal. A search
nothing answered still counts the half signal every missed payload does. A
search that answered without the person is name_search_no_match, apart from
one that failed. Every value is synthetic.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from linkedin_mcp_server.scraping import page_invites as pi
from linkedin_mcp_server.scraping.page_invite_api import PageInviteCapture

from .test_page_invites import IDS, _statuses, _urn
from .test_page_invites import writes  # noqa: F401  (fixture)
from .test_page_invites_name_search import _invite, _SearchPage
from .test_page_invites_name_search_limits import _FickleSearchPage


class _OtherKeywordsPage(_SearchPage):
    """A dialog whose search answers for the text written its own way."""

    def on_type(self, text: str) -> None:
        self.box += text
        self.searches.append(self.box)
        echoed = self.box.upper().replace(" ", "_")
        self._emit(
            "graphql?queryId=voyagerSearchDashReusableTypeahead.abc"
            f"&variables=(type:CONNECTIONS,keywords:{echoed},start:0,count:40,"
            "query:())",
            {
                "data": {
                    "data": {
                        "searchDashReusableTypeaheadByType": {
                            "elements": [
                                {
                                    "title": {"text": echoed},
                                    "target": {"*profile": _urn(i)},
                                }
                                for i in self.directory.get(self.box, [])
                            ]
                        }
                    }
                },
                "included": [],
            },
        )


@pytest.fixture
def signals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    recorded: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        PageInviteCapture,
        "_note_throttle",
        lambda _self, signal, **kwargs: recorded.append((signal, kwargs)),
    )
    monkeypatch.setattr(pi, "_SEARCH_OVERHEAD_SECONDS", 0.3)
    return recorded


async def test_an_answer_for_other_keywords_is_logged_and_no_throttle(
    writes,  # noqa: F811
    signals,
    caplog,
):
    page = _OtherKeywordsPage({"Alex Example": [IDS[10]]}, rows=3)
    with caplog.at_level(logging.INFO, logger=pi.__name__):
        result = await _invite(page, [IDS[10]], ["Alex Example"], confirm=False)

    assert signals == [], "LinkedIn answered: a keyword mismatch is no throttle"
    assert _statuses(result) == {IDS[10]: "not_found"}
    assert result["people"][0]["detail"].startswith("name_search_failed (")
    assert (
        "answered other keywords than typed ('Alex Example'): 'ALEX_EXAMPLE'"
        in caplog.text
    )


async def test_a_search_nothing_answered_counts_a_half_signal(
    writes,  # noqa: F811
    signals,
):
    page = _FickleSearchPage(directory={}, silent=("Alex Example",), rows=3)
    result = await _invite(page, [IDS[10]], ["Alex Example"], confirm=False)

    assert signals == [("payload_timeout", {"half": True})]
    assert _statuses(result) == {IDS[10]: "not_found"}
    assert result["people"][0]["detail"].startswith("name_search_failed (")


async def test_a_search_that_answered_without_the_person_is_no_match(
    writes,  # noqa: F811
    signals,
):
    page = _FickleSearchPage(
        directory={"Alex Example": [IDS[14]]}, silent=("Sam Sample",), rows=3
    )
    result = await _invite(
        page, [IDS[10], IDS[11]], ["Alex Example", "Sam Sample"], confirm=False
    )

    assert [p["detail"].split(" ")[0] for p in result["people"]] == [
        "name_search_no_match",
        "name_search_failed",
    ], "a search that answered is told apart from one that failed"
    assert _statuses(result) == {IDS[10]: "not_found", IDS[11]: "not_found"}
    assert signals == [("payload_timeout", {"half": True})]


class _KeystrokesPage(_SearchPage):
    """A dialog whose search answers each earlier keystroke; the last one
    is answered with ``status``, or not at all when it is None."""

    def __init__(self, status: int | None, **kwargs: Any):
        super().__init__(**kwargs)
        self.status = status

    def _answer(self, typed: str, status: int) -> None:
        self._emit(
            "graphql?queryId=voyagerSearchDashReusableTypeahead.abc"
            f"&variables=(type:CONNECTIONS,keywords:{typed},start:0,count:40,"
            "query:())",
            {"data": {"data": {"searchDashReusableTypeaheadByType": {"elements": []}}}},
            status=status,
        )

    def on_type(self, text: str) -> None:
        self.box += text
        self.searches.append(self.box)
        for end in range(1, len(self.box)):
            self._answer(self.box[:end], 200)
        if self.status is not None:
            self._answer(self.box, self.status)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (None, [("payload_timeout", {"half": True})]),
        (503, [("payload_timeout", {"half": True})]),
        (429, [("http_429", {}), ("payload_timeout", {"half": True})]),
    ],
)
async def test_earlier_keystrokes_answered_do_not_excuse_the_last(
    writes,  # noqa: F811
    signals,
    status,
    expected,
):
    page = _KeystrokesPage(status, directory={}, rows=3)
    result = await _invite(page, [IDS[10]], ["Alex Example"], confirm=False)

    assert signals == expected, (
        "an answer only for earlier keystrokes is no answer to the search"
    )
    assert result["people"][0]["detail"].startswith("name_search_failed (")
