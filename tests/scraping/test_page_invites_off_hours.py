"""Inviting to follow a page outside working hours, against the real pacing.

A dry run sends nothing, so it still plans everyone under the real caps and
reports the schedule apart; a send is refused and writes nothing. The schedule
reads as this server's own, never as a LinkedIn limit or a cap. Every value is
synthetic.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from linkedin_mcp_server.config.loaders import EnvironmentKeys
from linkedin_mcp_server.pacing import (
    ACCOUNT_BUDGET_JOB,
    PAGE_INVITES,
    Job,
    Schedule,
)
from linkedin_mcp_server.scraping import page_invites as pi

from .test_page_invites import (
    IDS,
    _DialogPage,
    _invite,
    _statuses,
    writes,  # noqa: F401  (fixture)
)

# Bound before the `writes` fixture replaces it with a fixed headroom.
_REAL_PACING_HEADROOM = pi._pacing_headroom

WED_10AM = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)
WED_8PM = datetime(2026, 8, 5, 20, 0, tzinfo=timezone.utc)
RESUME = "2026-08-06T09:00+00:00"


@pytest.fixture
def budget(writes, monkeypatch):  # noqa: F811
    """The account budget on business hours, read by the real pacing."""
    monkeypatch.delenv(EnvironmentKeys.WORKING_HOURS_DISABLED, raising=False)
    monkeypatch.delenv(EnvironmentKeys.PAGE_INVITES_MAX, raising=False)
    job = Job(
        name=ACCOUNT_BUDGET_JOB,
        started_on=date(2020, 1, 1),
        schedule=Schedule.business_hours(),
    )
    monkeypatch.setattr(pi, "_pacing_headroom", _REAL_PACING_HEADROOM)
    monkeypatch.setattr(pi, "cooldown_resume_at", lambda _store, _now: None)
    monkeypatch.setattr(pi, "load_account_budget", lambda _store, _now: job)
    return job


def _at(monkeypatch, now: datetime) -> None:
    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            return now

    monkeypatch.setattr(pi, "datetime", _Clock)


async def test_an_off_hours_dry_run_plans_everyone_and_says_when_sends_resume(
    budget,
    writes,  # noqa: F811
    monkeypatch,
):
    _at(monkeypatch, WED_8PM)
    page = _DialogPage(rows=3)
    result = await _invite(page, IDS[:3], confirm=False)

    assert page.sent == []
    assert writes == []
    assert result["status"] == "dry_run"
    assert _statuses(result) == dict.fromkeys(IDS[:3], "would_invite"), (
        "an off-hours dry run must plan as if within working hours"
    )
    assert "pacing" not in result
    assert result["schedule"] == (
        "outside_working_hours: page invites are not sent outside working hours "
        "(this server's schedule, not a LinkedIn limit); page invites resume at "
        f"{RESUME}."
    )


async def test_an_off_hours_dry_run_still_holds_people_to_the_daily_cap(
    budget,
    writes,  # noqa: F811
    monkeypatch,
):
    for _ in range(19):
        budget.ledger.record_kind(PAGE_INVITES, WED_10AM)
    _at(monkeypatch, WED_8PM)
    result = await _invite(_DialogPage(rows=3), IDS[:3], confirm=False)

    assert _statuses(result) == {
        IDS[0]: "would_invite",
        IDS[1]: "pacing_cap",
        IDS[2]: "pacing_cap",
    }
    assert result["schedule"].startswith("outside_working_hours: ")


async def test_an_off_hours_send_is_refused_and_writes_nothing(
    budget,
    writes,  # noqa: F811
    monkeypatch,
):
    _at(monkeypatch, WED_8PM)
    page = _DialogPage(rows=3)
    result = await _invite(page, IDS[:2], confirm=True)

    assert page.sent == []
    assert page.checked == []
    assert writes == []
    assert result["status"] == "nothing_to_invite"
    assert _statuses(result) == dict.fromkeys(IDS[:2], "pacing_cap")
    assert result["pacing"].startswith("outside_working_hours: page invites are not")
    assert "not a LinkedIn limit" in result["pacing"]
    assert "schedule" not in result


async def test_a_real_cap_keeps_the_action_limit_wording(
    budget,
    writes,  # noqa: F811
    monkeypatch,
):
    for _ in range(20):
        budget.ledger.record_kind(PAGE_INVITES, WED_10AM)
    _at(monkeypatch, WED_10AM.replace(minute=30))
    result = await _invite(_DialogPage(rows=3), IDS[:1], confirm=False)

    assert _statuses(result) == {IDS[0]: "pacing_cap"}
    assert result["pacing"].startswith(
        "action_limit: limit_exceeded: page_invites limit of 20 per 24 h reached."
    )
    assert "working hours" not in result["pacing"]
    assert "schedule" not in result
