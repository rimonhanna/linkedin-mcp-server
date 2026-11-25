"""Sales Navigator sends have a daily cap of their own beside regular messages."""

from datetime import date, datetime, timezone

import pytest

from linkedin_mcp_server.config.loaders import EnvironmentKeys
from linkedin_mcp_server.exceptions import ActionLimitError
from linkedin_mcp_server.pacing import (
    ACCOUNT_BUDGET_JOB,
    MESSAGES,
    SALES_NAVIGATOR_MESSAGES,
    WINDOW_SECONDS,
    Job,
    JobStore,
    Schedule,
    kind_caps,
    load_account_budget,
    record_action,
    refuse_action,
    refuse_if_limited,
)

NOW = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKING_HOURS_DISABLED", "true")
    return JobStore(tmp_path)


def test_default_cap_is_fifty_a_day():
    assert kind_caps(SALES_NAVIGATOR_MESSAGES) == ((50, WINDOW_SECONDS),)


def test_cap_follows_its_own_variable(monkeypatch):
    monkeypatch.setenv(EnvironmentKeys.SALES_NAVIGATOR_MESSAGES_MAX, "7")
    monkeypatch.setenv(EnvironmentKeys.MESSAGES_MAX, "3")

    assert kind_caps(SALES_NAVIGATOR_MESSAGES) == ((7, WINDOW_SECONDS),)


def test_sales_navigator_sends_do_not_use_up_regular_messages(store, monkeypatch):
    monkeypatch.setenv(EnvironmentKeys.SALES_NAVIGATOR_MESSAGES_MAX, "2")
    for _ in range(2):
        record_action(store, SALES_NAVIGATOR_MESSAGES, NOW)

    with pytest.raises(ActionLimitError) as refused:
        refuse_action(store, SALES_NAVIGATOR_MESSAGES, NOW)
    assert refused.value.kind == SALES_NAVIGATOR_MESSAGES
    refuse_action(store, MESSAGES, NOW)


def test_regular_messages_do_not_use_up_sales_navigator_sends(store, monkeypatch):
    monkeypatch.setenv(EnvironmentKeys.MESSAGES_MAX, "1")
    record_action(store, MESSAGES, NOW)

    refuse_action(store, SALES_NAVIGATOR_MESSAGES, NOW)
    assert (
        load_account_budget(store, NOW).ledger.spent_kind(
            SALES_NAVIGATOR_MESSAGES, NOW, WINDOW_SECONDS
        )
        == 0
    )


def test_sales_navigator_sends_wait_for_working_hours():
    budget = Job(
        name=ACCOUNT_BUDGET_JOB,
        started_on=date(2020, 1, 1),
        schedule=Schedule.business_hours(),
    )
    wednesday_8pm = datetime(2026, 8, 5, 20, 0)

    with pytest.raises(ActionLimitError) as refused:
        refuse_if_limited(budget, SALES_NAVIGATOR_MESSAGES, wednesday_8pm)
    assert refused.value.window == "working hours"
    assert refused.value.limit == 0
