"""The sales_list_writes pacing kind: its own daily cap and the working-hours gate."""

from datetime import date, datetime

import pytest

from linkedin_mcp_server.config.loaders import EnvironmentKeys
from linkedin_mcp_server.exceptions import ActionLimitError
from linkedin_mcp_server.pacing import (
    ACCOUNT_BUDGET_JOB,
    CAMPAIGN_WRITES,
    SALES_LIST_WRITES,
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


def test_thirty_list_writes_a_day_then_a_refusal(monkeypatch):
    monkeypatch.delenv(EnvironmentKeys.SALES_LIST_WRITES_MAX, raising=False)
    budget = _budget()
    for _ in range(30):
        refuse_if_limited(budget, SALES_LIST_WRITES, WED_10AM)
        budget.ledger.record_kind(SALES_LIST_WRITES, WED_10AM)
    with pytest.raises(ActionLimitError) as excinfo:
        refuse_if_limited(budget, SALES_LIST_WRITES, WED_10AM)
    assert excinfo.value.limit == 30
    assert excinfo.value.window == "24 h"
    assert kind_headroom(budget, SALES_LIST_WRITES, WED_10AM)[0] == 0


def test_the_cap_is_its_own_and_follows_the_environment(monkeypatch):
    monkeypatch.setenv(EnvironmentKeys.SALES_LIST_WRITES_MAX, "2")
    budget = _budget()
    for _ in range(5):
        budget.ledger.record_kind(CAMPAIGN_WRITES, WED_10AM)
    assert kind_headroom(budget, SALES_LIST_WRITES, WED_10AM)[0] == 2


def test_list_writes_wait_for_working_hours(monkeypatch):
    monkeypatch.delenv(EnvironmentKeys.WORKING_HOURS_DISABLED, raising=False)
    with pytest.raises(ActionLimitError) as excinfo:
        refuse_if_limited(
            _budget(Schedule.business_hours()), SALES_LIST_WRITES, WED_8PM
        )
    assert excinfo.value.window == "working hours"
