"""The name-search lookup under a deadline, failed searches and a fickle app.

A send searches each person once and ticks them straight after, so it never
searches twice; a person the deadline leaves no time for is marked alone, and
a dry run predicts the same. Every value is synthetic.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from linkedin_mcp_server.scraping import page_invites as pi

from .test_page_invites import (
    COMPANY,
    IDS,
    _actions,
    _statuses,
    _urn,
    writes,  # noqa: F401  (fixture)
)
from .test_page_invites_name_search import _SearchPage


class _Clock:
    """``time.monotonic`` for the module, moved on by each search."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class _TimedPage(_SearchPage):
    def __init__(self, clock: _Clock, seconds: float, **kwargs: Any):
        super().__init__(**kwargs)
        self.clock = clock
        self.seconds = seconds

    def on_type(self, text: str) -> None:
        self.clock.now += self.seconds
        super().on_type(text)


class _FickleSearchPage(_SearchPage):
    """A search that never answers, answers in an unknown shape, drops the
    selection, or lists a row the dialog never shows."""

    def __init__(
        self,
        *,
        silent: tuple[str, ...] = (),
        malformed: tuple[str, ...] = (),
        lose_ticks: bool = False,
        unshown: tuple[str, ...] = (),
        stale: dict[str, list[str]] | None = None,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.silent = silent
        self.malformed = malformed
        self.lose_ticks = lose_ticks
        self.unshown = unshown
        self.stale = stale or {}

    def on_type(self, text: str) -> None:
        typed = self.box + text
        if self.lose_ticks:
            self.checked.clear()
        if typed in self.silent:
            self.box = typed
            self.searches.append(typed)
            self.shown = []
            return
        if typed in self.malformed:
            self.box = typed
            self.searches.append(typed)
            self._emit(
                "graphql?queryId=voyagerSearchDashReusableTypeahead.abc"
                f"&variables=(type:CONNECTIONS,keywords:{typed},start:0,count:40,"
                "query:())",
                {
                    "data": {
                        "data": {
                            "searchDashReusableTypeaheadByType": {
                                "elements": [{"title": {"text": typed}}]
                            }
                        }
                    },
                    "included": [],
                },
            )
            return
        super().on_type(text)
        if self.shown is not None:
            self.shown = [i for i in self.shown if i not in self.unshown]
        for prefix, ids in self.stale.items():
            # An earlier keystroke's answer, arriving after the full name's.
            self._emit(
                "graphql?queryId=voyagerSearchDashReusableTypeahead.abc"
                f"&variables=(type:CONNECTIONS,keywords:{prefix},start:0,count:40,"
                "query:())",
                {
                    "data": {
                        "data": {
                            "searchDashReusableTypeaheadByType": {
                                "elements": [
                                    {
                                        "title": {"text": prefix},
                                        "target": {"*profile": _urn(i)},
                                    }
                                    for i in ids
                                ]
                            }
                        }
                    },
                    "included": [],
                },
            )

    async def evaluate(self, program: str, arg: Any = None) -> Any:
        if "page-invite-press" in program and len(self.checked) != arg:
            # The app's Invite counts its whole selection, shown or not.
            self.events.append("press")
            return "count_mismatch"
        return await super().evaluate(program, arg)


async def _invite(
    page: _SearchPage,
    people: list[str],
    names: list[str | None],
    *,
    confirm: bool,
    deadline: float | None = None,
) -> dict:
    actions, _ = _actions(page)
    return await actions.invite_to_follow(
        COMPANY, people, confirm_send=confirm, names=names, deadline=deadline
    )


FIVE = {f"Person {n}": [IDS[10 + n]] for n in range(5)}
FIVE_PEOPLE = [IDS[10 + n] for n in range(5)]
FIVE_NAMES: list[str | None] = [f"Person {n}" for n in range(5)]


def _deadline_for_three(clock: _Clock) -> float:
    # The send's reserve, three searches of 8 s, and less than a fourth's cost.
    return clock.now + pi._SEND_BUDGET_SECONDS + 3 * 8 + 7.0


async def test_a_five_person_send_sends_three_and_marks_the_two_it_skipped(
    writes,  # noqa: F811
    monkeypatch,
):
    clock = _Clock()
    monkeypatch.setattr(pi, "time", clock)
    page = _TimedPage(clock, 8.0, directory=FIVE, rows=3)
    result = await _invite(
        page, FIVE_PEOPLE, FIVE_NAMES, confirm=True, deadline=_deadline_for_three(clock)
    )

    assert page.searches == ["Person 0", "Person 1", "Person 2"], (
        "each person is searched once, and no search starts past the deadline"
    )
    assert result["status"] == "sent"
    assert page.sent == [FIVE_PEOPLE[:3]]
    assert writes == ["page_invites"] * 3
    assert _statuses(result) == {
        **dict.fromkeys(FIVE_PEOPLE[:3], "invited"),
        **dict.fromkeys(FIVE_PEOPLE[3:], "not_found"),
    }
    assert [p.get("detail") for p in result["people"][3:]] == [
        "name_search_not_run (deadline)"
    ] * 2


async def test_the_dry_run_predicts_the_same_cut(writes, monkeypatch):  # noqa: F811
    clock = _Clock()
    monkeypatch.setattr(pi, "time", clock)
    page = _TimedPage(clock, 8.0, directory=FIVE, rows=3)
    result = await _invite(
        page,
        FIVE_PEOPLE,
        FIVE_NAMES,
        confirm=False,
        deadline=_deadline_for_three(clock),
    )

    assert page.sent == []
    assert _statuses(result) == {
        **dict.fromkeys(FIVE_PEOPLE[:3], "would_invite"),
        **dict.fromkeys(FIVE_PEOPLE[3:], "not_found"),
    }


async def test_a_search_that_never_answers_fails_only_that_person(
    writes,  # noqa: F811
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(pi, "_SEARCH_OVERHEAD_SECONDS", 0.3)
    page = _FickleSearchPage(directory=FIVE, silent=("Person 0",), rows=3)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger=pi.__name__):
        result = await _invite(page, FIVE_PEOPLE[:2], FIVE_NAMES[:2], confirm=False)

    assert time.monotonic() - started < 6, (
        "the search's wait must be held to its budget"
    )
    assert _statuses(result) == {FIVE_PEOPLE[0]: "not_found", IDS[11]: "would_invite"}
    assert result["people"][0]["detail"].startswith("name_search_failed (")
    assert "name search for person 0 failed" in caplog.text


async def test_a_search_in_an_unknown_shape_fails_only_that_person(
    writes,  # noqa: F811
    caplog,
):
    page = _FickleSearchPage(directory=FIVE, malformed=("Person 0",), rows=3)
    with caplog.at_level(logging.WARNING, logger=pi.__name__):
        result = await _invite(page, FIVE_PEOPLE[:2], FIVE_NAMES[:2], confirm=True)

    assert _statuses(result) == {FIVE_PEOPLE[0]: "not_found", IDS[11]: "invited"}
    assert result["people"][0]["detail"].startswith(
        "name_search_failed (LinkedIn returned the page's connection search in a shape"
    )
    assert page.sent == [[IDS[11]]]
    assert "name search for person 0 failed" in caplog.text


async def test_an_earlier_keystrokes_answer_arriving_last_is_not_taken(writes):  # noqa: F811
    page = _FickleSearchPage(
        directory={"Person 0": [IDS[10]]}, stale={"Pers": [IDS[14]]}, rows=3
    )
    result = await _invite(page, [IDS[10]], ["Person 0"], confirm=False)

    assert _statuses(result) == {IDS[10]: "would_invite"}


async def test_ticks_lost_across_searches_mean_nothing_is_pressed(writes):  # noqa: F811
    page = _FickleSearchPage(directory=FIVE, lose_ticks=True, rows=3)
    result = await _invite(page, FIVE_PEOPLE[:2], FIVE_NAMES[:2], confirm=True)

    assert result["status"] == "invite_control_not_found"
    assert "count_mismatch" in result["message"]
    assert page.sent == []
    assert writes == []
    assert result["retry_safe"] is True


async def test_a_searched_row_the_dialog_does_not_show_fails_the_selection(
    writes,  # noqa: F811
):
    page = _FickleSearchPage(directory=FIVE, unshown=(IDS[11],), rows=3)
    result = await _invite(page, FIVE_PEOPLE[:2], FIVE_NAMES[:2], confirm=True)

    assert result["status"] == "selection_failed"
    assert page.sent == []
    assert writes == []


async def test_a_username_given_with_a_name_is_not_searched(writes):  # noqa: F811
    page = _SearchPage({"Example Name": [IDS[10]]}, rows=3)
    people = ["https://www.linkedin.com/in/some-person/", "other-person"]
    result = await _invite(
        page, people, ["Example Name", "Example Name"], confirm=False
    )

    assert page.searches == []
    assert _statuses(result) == dict.fromkeys(people, "not_invitable")
