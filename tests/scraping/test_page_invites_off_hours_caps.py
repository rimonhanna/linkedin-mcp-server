"""Page invites outside working hours when a cap also holds, or the schedule is off.

A dry run off hours reports a real cap as ``action_limit`` or ``hourly_cap``
and the schedule apart; ``WORKING_HOURS_DISABLED=1`` lets a send through with
no schedule note. Every value is synthetic.
"""

from __future__ import annotations

from datetime import timedelta

from linkedin_mcp_server.config.loaders import EnvironmentKeys
from linkedin_mcp_server.pacing import PAGE_INVITES

from .test_page_invites import IDS, _DialogPage, _invite, _statuses
from .test_page_invites import writes  # noqa: F401  (fixture)
from .test_page_invites_off_hours import WED_8PM, _at
from .test_page_invites_off_hours import budget  # noqa: F401  (fixture)


async def test_a_spent_daily_cap_off_hours_is_the_cap_plus_the_schedule(
    budget,  # noqa: F811
    writes,  # noqa: F811
    monkeypatch,
):
    for _ in range(20):
        budget.ledger.record_kind(PAGE_INVITES, WED_8PM)
    _at(monkeypatch, WED_8PM)
    result = await _invite(_DialogPage(rows=3), IDS[:2], confirm=False)

    assert _statuses(result) == dict.fromkeys(IDS[:2], "pacing_cap")
    assert result["pacing"].startswith(
        "action_limit: limit_exceeded: page_invites limit of 20 per 24 h reached."
    )
    assert result["schedule"].startswith("outside_working_hours: page invites")


async def test_the_hourly_cap_off_hours_is_reported_with_the_schedule(
    budget,  # noqa: F811
    writes,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setenv(EnvironmentKeys.HOURLY_ACTIONS_MAX, "1")
    budget.ledger.actions.append((WED_8PM - timedelta(minutes=10)).timestamp())
    _at(monkeypatch, WED_8PM)
    result = await _invite(_DialogPage(rows=3), IDS[:2], confirm=False)

    assert _statuses(result) == dict.fromkeys(IDS[:2], "pacing_cap")
    assert result["pacing"] == "hourly_cap"
    assert result["schedule"].startswith("outside_working_hours: page invites")


async def test_working_hours_disabled_lets_a_send_through_off_hours(
    budget,  # noqa: F811
    writes,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setenv(EnvironmentKeys.WORKING_HOURS_DISABLED, "1")
    _at(monkeypatch, WED_8PM)
    page = _DialogPage(rows=3)
    result = await _invite(page, IDS[:2], confirm=True)

    assert result["status"] == "sent"
    assert page.sent == [IDS[:2]]
    assert writes == [PAGE_INVITES, PAGE_INVITES]
    assert "schedule" not in result
    assert "pacing" not in result
