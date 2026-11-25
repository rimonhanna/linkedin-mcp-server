"""Every write refused by the schedule names it as this server's, not LinkedIn's.

For a tool that raises, the message is the only category a client sees, so it
must open with ``outside_working_hours`` rather than ``limit_exceeded``.
"""

from datetime import date, datetime

import pytest
from fastmcp.exceptions import ToolError

from linkedin_mcp_server.config.loaders import EnvironmentKeys
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.exceptions import ActionLimitError
from linkedin_mcp_server.pacing import (
    ACCOUNT_BUDGET_JOB,
    CAMPAIGN_WRITES,
    INVITES,
    MESSAGES,
    PAGE_INVITES,
    SALES_LIST_WRITES,
    SALES_NAVIGATOR_MESSAGES,
    WITHDRAWALS,
    Job,
    Schedule,
    refuse_if_limited,
)

WED_8PM = datetime(2026, 8, 5, 20, 0)
WRITE_KINDS = [
    INVITES,
    MESSAGES,
    WITHDRAWALS,
    CAMPAIGN_WRITES,
    SALES_NAVIGATOR_MESSAGES,
    PAGE_INVITES,
    SALES_LIST_WRITES,
]


def _refusal(kind: str, monkeypatch) -> ActionLimitError:
    monkeypatch.delenv(EnvironmentKeys.WORKING_HOURS_DISABLED, raising=False)
    budget = Job(
        name=ACCOUNT_BUDGET_JOB,
        started_on=date(2020, 1, 1),
        schedule=Schedule.business_hours(),
    )
    with pytest.raises(ActionLimitError) as excinfo:
        refuse_if_limited(budget, kind, WED_8PM)
    return excinfo.value


@pytest.mark.parametrize("kind", WRITE_KINDS)
def test_every_write_kind_names_the_schedule(kind, monkeypatch):
    refusal = _refusal(kind, monkeypatch)
    words = kind.replace("_", " ")

    assert refusal.error_type == "limit_exceeded"
    assert str(refusal) == (
        f"outside_working_hours: {words} are not sent outside working hours "
        f"(this server's schedule, not a LinkedIn limit); {words} resume at "
        "2026-08-06T09:00."
    )


@pytest.mark.parametrize("kind", WRITE_KINDS)
def test_working_hours_disabled_refuses_no_write_kind_off_hours(kind, monkeypatch):
    monkeypatch.setenv(EnvironmentKeys.WORKING_HOURS_DISABLED, "1")
    budget = Job(
        name=ACCOUNT_BUDGET_JOB,
        started_on=date(2020, 1, 1),
        schedule=Schedule.business_hours(),
    )
    refuse_if_limited(budget, kind, WED_8PM)


@pytest.mark.parametrize(
    ("tool", "kind"),
    [("connect_with_person", INVITES), ("send_message", MESSAGES)],
)
def test_a_tool_that_raises_shows_the_schedule_marker(tool, kind, monkeypatch):
    with pytest.raises(ToolError) as excinfo:
        raise_tool_error(_refusal(kind, monkeypatch), tool)

    message = str(excinfo.value)
    assert message.startswith("outside_working_hours: ")
    assert "not a LinkedIn limit" in message
    assert "limit_exceeded" not in message


def test_a_withdrawal_run_stopped_by_the_schedule_keeps_no_failure_trace(
    monkeypatch,
):
    from linkedin_mcp_server.scraping.contracts import pacing_stop
    from linkedin_mcp_server.tools.person import (
        _WITHDRAW_SUCCEEDED,
        _withdraw_stop_kind,
    )

    reason = pacing_stop(_refusal(WITHDRAWALS, monkeypatch))
    assert _withdraw_stop_kind(reason) == "outside_working_hours"
    assert _withdraw_stop_kind(reason) in _WITHDRAW_SUCCEEDED
