"""A person the call's deadline leaves no time to search for is not_checked.

Never looked up is not the same as looked up and absent: the person keeps the
deadline detail, is never ticked, and the call says how many are left to retry.
Every value is synthetic.
"""

from __future__ import annotations

from linkedin_mcp_server.scraping import page_invites as pi

from .test_page_invites import (
    COMPANY,
    _actions,
    _statuses,
    writes,  # noqa: F401  (fixture)
)
from .test_page_invites_name_search_limits import (
    FIVE,
    FIVE_NAMES,
    FIVE_PEOPLE,
    _Clock,
    _deadline_for_three,
    _TimedPage,
)


async def _invite(page: _TimedPage, clock: _Clock, *, confirm: bool) -> dict:
    actions, _ = _actions(page)
    return await actions.invite_to_follow(
        COMPANY,
        FIVE_PEOPLE,
        confirm_send=confirm,
        names=FIVE_NAMES,
        deadline=_deadline_for_three(clock),
    )


async def test_a_send_cut_by_the_deadline_reports_the_rest_not_checked(
    writes,  # noqa: F811
    monkeypatch,
):
    clock = _Clock()
    monkeypatch.setattr(pi, "time", clock)
    page = _TimedPage(clock, 8.0, directory=FIVE, rows=3)
    result = await _invite(page, clock, confirm=True)

    assert _statuses(result) == {
        **dict.fromkeys(FIVE_PEOPLE[:3], "invited"),
        **dict.fromkeys(FIVE_PEOPLE[3:], "not_checked"),
    }, "a person never searched for must not read as not_found"
    assert [p["detail"] for p in result["people"][3:]] == [
        "name_search_not_run (deadline)"
    ] * 2
    assert page.sent == [FIVE_PEOPLE[:3]]
    assert not set(page.checked) & set(FIVE_PEOPLE[3:]), (
        "a person not checked must never be ticked"
    )
    assert result["not_checked"] == 2
    assert result["message"].endswith(
        "2 people were not checked in this call; call again with them."
    )


async def test_the_dry_run_counts_the_same_people_not_checked(
    writes,  # noqa: F811
    monkeypatch,
):
    clock = _Clock()
    monkeypatch.setattr(pi, "time", clock)
    page = _TimedPage(clock, 8.0, directory=FIVE, rows=3)
    result = await _invite(page, clock, confirm=False)

    assert result["status"] == "dry_run"
    assert [p["status"] for p in result["people"][3:]] == ["not_checked"] * 2
    assert result["not_checked"] == 2
    assert "2 people were not checked in this call" in result["message"]


async def test_a_call_that_checks_everyone_adds_no_note(writes):  # noqa: F811
    page = _TimedPage(_Clock(), 0.0, directory=FIVE, rows=3)
    actions, _ = _actions(page)
    result = await actions.invite_to_follow(
        COMPANY, FIVE_PEOPLE[:2], confirm_send=False, names=FIVE_NAMES[:2]
    )

    assert "not_checked" not in result
    assert "not checked" not in result["message"]
