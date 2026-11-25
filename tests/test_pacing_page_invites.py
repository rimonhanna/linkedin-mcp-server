"""The page_invites pacing kind: its own daily cap and the working-hours gate."""

from datetime import date, datetime

import pytest

from linkedin_mcp_server.config.loaders import EnvironmentKeys
from linkedin_mcp_server.exceptions import ActionLimitError
from linkedin_mcp_server.pacing import (
    ACCOUNT_BUDGET_JOB,
    INVITES,
    PAGE_INVITES,
    Job,
    Schedule,
    kind_headroom,
    refuse_if_limited,
)

WED_10AM = datetime(2026, 8, 5, 10, 0)
WED_8PM = datetime(2026, 8, 5, 20, 0)


def _budget(schedule: Schedule | None = None) -> Job:
    return Job(
        name=ACCOUNT_BUDGET_JOB,
        started_on=date(2020, 1, 1),
        schedule=schedule or Schedule(),
    )


def test_twenty_page_invites_a_day_then_a_refusal(monkeypatch):
    monkeypatch.delenv(EnvironmentKeys.PAGE_INVITES_MAX, raising=False)
    budget = _budget()
    for _ in range(20):
        refuse_if_limited(budget, PAGE_INVITES, WED_10AM)
        budget.ledger.record_kind(PAGE_INVITES, WED_10AM)
    with pytest.raises(ActionLimitError) as excinfo:
        refuse_if_limited(budget, PAGE_INVITES, WED_10AM)
    assert excinfo.value.limit == 20
    assert excinfo.value.window == "24 h"
    assert kind_headroom(budget, PAGE_INVITES, WED_10AM)[0] == 0


def test_the_cap_is_its_own_and_follows_the_environment(monkeypatch):
    monkeypatch.setenv(EnvironmentKeys.PAGE_INVITES_MAX, "2")
    budget = _budget()
    for _ in range(5):
        budget.ledger.record_kind(INVITES, WED_10AM)
    assert kind_headroom(budget, PAGE_INVITES, WED_10AM)[0] == 2


def test_page_invites_wait_for_working_hours(monkeypatch):
    monkeypatch.delenv(EnvironmentKeys.WORKING_HOURS_DISABLED, raising=False)
    with pytest.raises(ActionLimitError) as excinfo:
        refuse_if_limited(_budget(Schedule.business_hours()), PAGE_INVITES, WED_8PM)
    assert excinfo.value.window == "working hours"
