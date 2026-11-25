"""Inviting a person the dialog's candidate list does not show.

The dialog lists only about the oldest 100 connections. A person missing from
it is looked up once through the dialog's own name search, and a result is
taken only when its id is theirs. The fake dialog replaces its shown rows with
each search's results and keeps the selection across searches, as the app's
picker does. Every value is synthetic.
"""

from __future__ import annotations

from typing import Any

from .test_page_invites import (
    IDS,
    _DialogPage,
    _statuses,
    _urn,
    _actions,
    COMPANY,
    writes,  # noqa: F401  (fixture)
)


class _SearchLocator:
    def __init__(self, page: _SearchPage):
        self._page = page

    async def click(self) -> None:
        self._page.events.append("search_box.click")

    async def fill(self, value: str) -> None:
        self._page.events.append(f"fill:{value}")
        self._page.box = value


class _SearchPage(_DialogPage):
    """The dialog with a name search over ``directory`` (name -> ids)."""

    def __init__(self, directory: dict[str, list[str]], **kwargs: Any):
        super().__init__(**kwargs)
        self.directory = directory
        self.box = ""
        self.shown: list[str] | None = None
        self.searches: list[str] = []

    def locator(self, selector: str) -> Any:
        return _SearchLocator(self)

    def _visible(self) -> list[str]:
        return self.shown if self.shown is not None else self.rows[: self.loaded]

    async def evaluate(self, program: str, arg: Any = None) -> Any:
        if "page-invite-state" in program:
            visible = self._visible()
            checked = [c for c in self.checked if c in visible]
            return {
                "open": True,
                "rows": list(visible),
                "checked": checked,
                "checkedTotal": len(checked),
            }
        if "page-invite-tick" in program:
            self.events.append(f"tick:{arg}")
            if arg not in self._visible() or arg in self.checked:
                return "not_found"
            self.checked.append(arg)
            return "ticked"
        return await super().evaluate(program, arg)

    def on_type(self, text: str) -> None:
        self.box += text
        self.searches.append(self.box)
        ids = self.directory.get(self.box, [])
        self.shown = ids
        self._emit(
            "graphql?queryId=voyagerSearchDashReusableTypeahead.abc"
            f"&variables=(type:CONNECTIONS,keywords:{self.box},start:0,count:40,"
            "query:())",
            {
                "data": {
                    "data": {
                        "searchDashReusableTypeaheadByType": {
                            "elements": [
                                {
                                    "title": {"text": self.box},
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
        self._emit_statuses(ids)


async def _invite(
    page: _SearchPage, people: list[str], names: list[str | None], *, confirm: bool
) -> dict:
    actions, _ = _actions(page)
    return await actions.invite_to_follow(
        COMPANY, people, confirm_send=confirm, names=names
    )


async def test_a_person_found_only_by_the_name_search_would_be_invited(writes):  # noqa: F811
    page = _SearchPage({"Example Later": [IDS[10]]}, rows=3)
    result = await _invite(page, [IDS[10]], ["Example Later"], confirm=False)

    assert page.searches == ["Example Later"]
    assert page.sent == []
    assert writes == []
    assert _statuses(result) == {IDS[10]: "would_invite"}, (
        "a person the name search returned with their own id must be planned"
    )
    assert result["people"][0]["profileId"] == IDS[10]
    assert result["credits"]["needed"] == 1


async def test_without_a_name_the_person_is_not_searched_for(writes):  # noqa: F811
    page = _SearchPage({"Example Later": [IDS[10]]}, rows=3)
    result = await _invite(page, [IDS[10]], [None], confirm=False)

    assert page.searches == []
    assert _statuses(result) == {IDS[10]: "not_invitable"}
    assert result["people"][0]["detail"] == "not_a_candidate"


async def test_a_person_the_search_does_not_return_is_not_found_by_it(writes):  # noqa: F811
    page = _SearchPage({"Example Missing": [IDS[14]]}, rows=3)
    result = await _invite(page, [IDS[13]], ["Example Missing"], confirm=False)

    assert page.searches == ["Example Missing"]
    assert _statuses(result) == {IDS[13]: "not_found"}
    assert result["people"][0]["detail"] == "name_search_no_match"
    assert result["credits"]["needed"] == 0


async def test_a_shared_name_is_decided_by_id_and_never_by_name(writes):  # noqa: F811
    page = _SearchPage({"Sam Same": [IDS[11], IDS[12]]}, rows=3)
    result = await _invite(
        page,
        [IDS[12], "urn:li:member:123456789"],
        ["Sam Same", "Sam Same"],
        confirm=False,
    )

    assert _statuses(result) == {
        IDS[12]: "would_invite",
        "urn:li:member:123456789": "not_found",
    }
    assert result["people"][1]["detail"] == "name_search_no_match"
    assert "profileId" not in result["people"][1]


async def test_a_search_returning_one_id_twice_ticks_no_one(writes):  # noqa: F811
    page = _SearchPage({"Sam Twice": [IDS[15], IDS[15]]}, rows=3)
    result = await _invite(page, [IDS[15]], ["Sam Twice"], confirm=True)

    assert page.sent == []
    assert not any(e.startswith("tick:") for e in page.events)
    assert _statuses(result) == {IDS[15]: "not_invitable"}
    assert result["people"][0]["detail"] == "ambiguous_identifier"


async def test_a_send_ticks_only_the_verified_people(writes):  # noqa: F811
    page = _SearchPage(
        {
            "Example Later": [IDS[10]],
            "Sam Same": [IDS[11], IDS[12]],
            "Example Missing": [IDS[14]],
        },
        rows=3,
    )
    people = [IDS[0], IDS[10], IDS[12], IDS[13]]
    result = await _invite(
        page,
        people,
        [None, "Example Later", "Sam Same", "Example Missing"],
        confirm=True,
    )

    ticks = [e.removeprefix("tick:") for e in page.events if e.startswith("tick:")]
    assert sorted(ticks) == sorted([IDS[0], IDS[10], IDS[12]]), (
        "only people confirmed by id may be ticked"
    )
    assert IDS[11] not in ticks and IDS[14] not in ticks
    assert len(page.sent) == 1
    assert sorted(page.sent[0]) == sorted([IDS[0], IDS[10], IDS[12]])
    assert writes == ["page_invites"] * 3
    assert result["status"] == "sent"
    assert _statuses(result) == {
        IDS[0]: "invited",
        IDS[10]: "invited",
        IDS[12]: "invited",
        IDS[13]: "not_found",
    }
