"""Tests for withdrawing old sent invitations.

``page.evaluate`` is a double that answers by script, so the JS never runs;
what is held here is the decision logic: which rows are old enough, which are
never touched, and that a dry run clicks nothing.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_mcp_server.scraping import invitation_withdrawal as iw
from linkedin_mcp_server.scraping.connection_actions import _SENT_INVITATIONS_URL
from linkedin_mcp_server.scraping.invitation_withdrawal import (
    InvitationWithdrawer,
    age_text,
    normalize_exclusions,
    parse_age_days,
    plan_withdrawals,
    username_from_href,
)


def _row(username: str, age: str | None, name: str = "Someone") -> dict[str, str]:
    text = f"{name}\nHeadline\n{age}\nWithdraw" if age else f"{name}\nHeadline"
    return {"href": f"/in/{username}/", "name": name, "text": text}


class TestParseAge:
    @pytest.mark.parametrize(
        ("text", "days"),
        [
            ("Sent today", 0),
            ("Sent yesterday", 1),
            ("Sent 3 hours ago", 0),
            ("Sent 11 hours ago", 0),
            ("Sent 1 day ago", 1),
            ("Sent 2 days ago", 2),
            ("Sent 3 weeks ago", 21),
            ("Sent 1 week ago", 7),
            ("Sent 2 weeks ago", 14),
            ("Sent 2 months ago", 60),
            ("Sent 4 months ago", 120),
            ("Sent 1 month ago", 30),
            ("Sent a month ago", 30),
            ("Sent 5 months ago", 150),
            ("Sent 1 year ago", 365),
            ("Jane\nEngineer\nSent 2 weeks ago\nWithdraw", 14),
        ],
    )
    def test_parses(self, text, days):
        assert parse_age_days(text) == days

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "Withdraw",
            "Gesendet vor 3 Wochen",
            "Sent recently",
            "3 weeks",
            # Inside another line (a headline or note), not the age line.
            "Jane\nI sent 3 weeks ago a note to everyone\nWithdraw",
            "Jane\nSent 3 weeks ago to me by a recruiter\nWithdraw",
            # Two age lines: which is the invitation's own is a guess.
            "Jane\nSent 3 weeks ago\nSent 1 day ago\nWithdraw",
        ],
    )
    def test_unparseable_is_none(self, text):
        assert parse_age_days(text) is None

    def test_age_text_is_the_matched_phrase(self):
        assert age_text("Jane\nSent 3 weeks ago\nWithdraw") == "Sent 3 weeks ago"


class TestIdentifiers:
    def test_username_from_href(self):
        assert (
            username_from_href("https://www.linkedin.com/in/Jane-Doe/?x=1")
            == "jane-doe"
        )
        assert username_from_href("/company/acme/") == ""

    def test_exclusions_accept_usernames_and_urls(self):
        assert normalize_exclusions(
            ["Jane-Doe", "https://www.linkedin.com/in/john-smith/"]
        ) == {"jane-doe", "john-smith"}
        assert normalize_exclusions(None) == set()


class TestUnicodeExclusions:
    def test_percent_encoded_exclusion_matches_a_unicode_href(self):
        raw = "alix-françois-poncet-523043135"
        rows = [
            {
                "href": f"https://www.linkedin.com/in/{raw}/",
                "name": "Alix",
                "text": "Sent 2 months ago",
            }
        ]
        excluded = normalize_exclusions(["alix-fran%c3%a7ois-poncet-523043135"])
        plan = plan_withdrawals(rows, older_than_days=7, excluded=excluded, limit=20)
        assert plan["chosen"] == []
        assert plan["skipped_excluded"] == [raw]

    def test_encoded_href_matches_a_unicode_exclusion(self):
        rows = [
            {
                "href": "https://www.linkedin.com/in/Alix-Fran%C3%A7ois-Poncet/",
                "name": "Alix",
                "text": "Sent 2 months ago",
            }
        ]
        excluded = normalize_exclusions(["alix-françois-poncet"])
        plan = plan_withdrawals(rows, older_than_days=7, excluded=excluded, limit=20)
        assert plan["chosen"] == []


class TestPlan:
    def test_only_rows_at_or_over_the_threshold(self):
        rows = [_row("old", "Sent 3 weeks ago"), _row("new", "Sent 2 days ago")]
        plan = plan_withdrawals(rows, older_than_days=14, excluded=set(), limit=20)
        assert [c["username"] for c in plan["chosen"]] == ["old"]
        assert plan["chosen"][0]["age_text"] == "Sent 3 weeks ago"

    def test_unparseable_age_is_not_old(self):
        rows = [_row("mystery", None), _row("weird", "Sent sometime")]
        plan = plan_withdrawals(rows, older_than_days=1, excluded=set(), limit=20)
        assert plan["chosen"] == []
        assert plan["skipped_unparsed_age"] == ["mystery", "weird"]

    def test_exclusion_by_username_and_url(self):
        rows = [
            _row("keep-me", "Sent 2 months ago"),
            _row("also-keep", "Sent 2 months ago"),
            _row("drop-me", "Sent 2 months ago"),
        ]
        excluded = normalize_exclusions(
            ["keep-me", "https://linkedin.com/in/also-keep"]
        )
        plan = plan_withdrawals(rows, older_than_days=7, excluded=excluded, limit=20)
        assert [c["username"] for c in plan["chosen"]] == ["drop-me"]
        assert plan["skipped_excluded"] == ["keep-me", "also-keep"]

    def test_max_withdrawals_bounds_and_takes_oldest_first(self):
        rows = [
            _row("a", "Sent 2 weeks ago"),
            _row("b", "Sent 3 months ago"),
            _row("c", "Sent 1 month ago"),
        ]
        plan = plan_withdrawals(rows, older_than_days=7, excluded=set(), limit=2)
        assert [c["username"] for c in plan["chosen"]] == ["b", "c"]


CONFIRM_TEXT = "Withdraw invitation\nIf you withdraw now, you won’t be able to resend."


def _withdrawer(rows: list[dict[str, str]], *, still_listed: bool = False):
    page = MagicMock()
    clicks: list[str] = []
    state: dict[str, Any] = {
        "dialogs": [],
        "opens_on_click": True,
        "closes_on_confirm": True,
        "confirm_outcome": "clicked",
        "listed": [],
        "confirm_error": None,
        "confirm_args": [],
        "cancels": 0,
    }

    async def evaluate(script: str, arg: Any = None):
        if script == iw._READ_ROWS_JS:
            return rows
        if script == iw._COUNT_ROWS_JS:
            return len(rows)
        if script == iw._SCROLL_LIST_JS:
            return True
        if script == iw._CLICK_WITHDRAW_JS:
            clicks.append(arg)
            if state["opens_on_click"]:
                state["dialogs"] = [CONFIRM_TEXT]
            return True
        if script == iw._OPEN_DIALOGS_JS:
            return state["dialogs"]
        if script == iw._CONFIRM_WITHDRAW_JS:
            state["confirm_args"].append(arg)
            if state["confirm_error"]:
                raise state["confirm_error"]
            if state["closes_on_confirm"]:
                state["dialogs"] = []
            return state["confirm_outcome"]
        if script == iw._CANCEL_DIALOG_JS:
            state["cancels"] += 1
            state["dialogs"] = []
            return True
        if script == iw._ROWS_FOR_USER_JS:
            if state["listed"]:
                return state["listed"].pop(0)
            return 1 if still_listed else 0
        raise AssertionError("unexpected script")

    page.evaluate = evaluate
    page.url = _SENT_INVITATIONS_URL
    page.locator.return_value.first.wait_for = AsyncMock()
    actions = MagicMock()
    actions._session.page = page
    actions._navigator._navigate_to_page = AsyncMock()
    actions._page_alerts = AsyncMock(return_value="")
    actions.state = state
    withdrawer = InvitationWithdrawer(actions, sent_list_url=_SENT_INVITATIONS_URL)
    return withdrawer, actions, clicks


@pytest.fixture(autouse=True)
def hooks():
    with (
        patch.object(iw.asyncio, "sleep", new_callable=AsyncMock) as sleep,
        patch.object(iw, "cooldown_resume_at", return_value=None),
        patch.object(iw, "JobStore"),
        patch.object(iw, "admit_write") as admit,
        patch.object(iw, "record_write") as record,
        patch.object(iw, "note_throttle_signal") as throttle,
    ):
        yield SimpleNamespace(
            admit=admit, record=record, throttle=throttle, sleep=sleep
        )


def _url_sequence(page, *urls: str, then: str = "https://www.linkedin.com/checkpoint/"):
    """Have ``page.url`` answer ``urls`` in order, then ``then`` for good."""
    queue = iter(urls)
    type(page).url = property(lambda self: next(queue, then))


ROWS = [
    _row("old-one", "Sent 2 months ago", "Old One"),
    _row("old-two", "Sent 5 weeks ago", "Old Two"),
    _row("fresh", "Sent 2 days ago"),
    _row("nodate", None),
]


class TestWithdraw:
    async def test_dry_run_withdraws_nothing(self):
        withdrawer, actions, clicks = _withdrawer(ROWS)
        result = await withdrawer.withdraw_invitations(7)
        assert clicks == []
        assert result["withdrawn"] == []
        assert result["rows_seen"] == len(ROWS)
        assert [i["username"] for i in result["would_withdraw"]] == [
            "old-one",
            "old-two",
        ]
        assert result["skipped_unparsed_age"] == ["nodate"]
        assert result["stopped_reason"] is None
        actions._navigator._navigate_to_page.assert_awaited_once_with(
            _SENT_INVITATIONS_URL
        )

    async def test_withdraws_confirms_and_reports(self):
        withdrawer, actions, clicks = _withdrawer(ROWS)
        result = await withdrawer.withdraw_invitations(
            7, exclude_usernames=["old-two"], dry_run=False
        )
        assert clicks == ["old-one"]
        assert result["withdrawn"] == [
            {
                "username": "old-one",
                "name": "Old One",
                "age_text": "Sent 2 months ago",
                "alert_text": "",
            }
        ]
        assert result["skipped_excluded"] == ["old-two"]
        assert "would_withdraw" not in result
        assert result["rows_seen"] == len(ROWS)
        assert actions.state["dialogs"] == []

    async def test_max_withdrawals_is_a_hard_bound(self):
        withdrawer, _, clicks = _withdrawer(ROWS)
        result = await withdrawer.withdraw_invitations(
            7, max_withdrawals=1, dry_run=False
        )
        assert clicks == ["old-one"]
        assert len(result["withdrawn"]) == 1

    async def test_stops_on_account_cooldown(self):
        from datetime import datetime, timezone

        withdrawer, _, clicks = _withdrawer(ROWS)
        resume = datetime(2030, 1, 1, tzinfo=timezone.utc)
        with patch.object(iw, "cooldown_resume_at", return_value=resume):
            result = await withdrawer.withdraw_invitations(7, dry_run=False)
        assert clicks == []
        assert result["stopped_reason"].startswith("account_cooldown")

    async def test_stops_when_row_is_still_listed(self):
        withdrawer, _, clicks = _withdrawer(ROWS, still_listed=True)
        result = await withdrawer.withdraw_invitations(7, dry_run=False)
        assert clicks == ["old-one"]
        assert result["withdrawn"] == []
        assert result["stopped_reason"] == "withdrawal_unverified"

    async def test_stops_when_dialog_does_not_close(self):
        withdrawer, actions, clicks = _withdrawer(ROWS)
        actions.state["closes_on_confirm"] = False
        result = await withdrawer.withdraw_invitations(7, dry_run=False)
        assert clicks == ["old-one"]
        assert result["stopped_reason"] == "confirmation_dialog_still_open"

    async def test_throttled_navigation_stops_with_nothing_done(self):
        from linkedin_mcp_server.core.exceptions import RateLimitError

        withdrawer, actions, clicks = _withdrawer(ROWS)
        actions._navigator._navigate_to_page = AsyncMock(
            side_effect=RateLimitError("429")
        )
        result = await withdrawer.withdraw_invitations(7, dry_run=False)
        assert clicks == []
        assert result["stopped_reason"].startswith("throttled")


class TestRowsSeen:
    async def test_no_rows_is_reported_not_silent(self):
        withdrawer, actions, clicks = _withdrawer([])
        result = await withdrawer.withdraw_invitations(7, dry_run=False)
        assert result["rows_seen"] == 0
        assert result["stopped_reason"] == "no_rows_found"
        assert clicks == []

    async def test_no_rows_on_a_dry_run(self):
        withdrawer, _, _ = _withdrawer([])
        result = await withdrawer.withdraw_invitations(7)
        assert result["rows_seen"] == 0
        assert result["stopped_reason"] == "no_rows_found"
        assert result["would_withdraw"] == []

    async def test_rows_seen_counts_rows_that_are_not_old_enough(self):
        withdrawer, _, _ = _withdrawer([_row("fresh", "Sent 2 days ago")])
        result = await withdrawer.withdraw_invitations(30)
        assert result["rows_seen"] == 1
        assert result["stopped_reason"] is None
        assert result["would_withdraw"] == []


class TestInfiniteScroll:
    @staticmethod
    def _grow(page, totals: list[int]):
        """Count grows to the next total on each scroll; ``scrolls`` counts them."""
        original = page.evaluate
        seen = {"scrolls": 0, "counts": 0}

        async def evaluate(script, arg=None):
            if script == iw._COUNT_ROWS_JS:
                seen["counts"] += 1
                return totals[min(seen["scrolls"], len(totals) - 1)]
            if script == iw._SCROLL_LIST_JS:
                seen["scrolls"] += 1
                return True
            return await original(script, arg)

        page.evaluate = evaluate
        return seen

    async def test_scrolls_until_the_count_is_unchanged_three_times(self, hooks):
        withdrawer, actions, _ = _withdrawer(ROWS)
        seen = self._grow(actions._session.page, [10, 20, 30])
        result = await withdrawer.withdraw_invitations(7)
        assert seen["scrolls"] == 5
        assert result["stopped_reason"] is None

    async def test_a_round_continues_as_soon_as_the_count_grows(self, hooks):
        withdrawer, actions, _ = _withdrawer(ROWS)
        seen = self._grow(actions._session.page, [10, 20, 30])
        await withdrawer.withdraw_invitations(7)
        # Two growing rounds poll once each; three flat rounds poll the full
        # round budget each, plus the initial count.
        assert seen["counts"] == 1 + 2 + 3 * iw._SCROLL_ROUND_POLLS
        slept = [c.args[0] for c in hooks.sleep.await_args_list]
        assert slept.count(iw._SCROLL_POLL_SECONDS) == 2 + 3 * iw._SCROLL_ROUND_POLLS

    async def test_a_list_that_never_stops_growing_is_reported(self, hooks):
        withdrawer, actions, _ = _withdrawer(ROWS)
        self._grow(actions._session.page, list(range(10, 400, 10)))
        with patch.object(iw, "_MAX_LOAD_ROUNDS", 4):
            result = await withdrawer.withdraw_invitations(7, dry_run=False)
        assert result["stopped_reason"] == "list_not_fully_loaded"
        assert result["withdrawn"] == []

    async def test_deadline_during_loading_reports_the_partial_list(self, hooks):
        withdrawer, actions, clicks = _withdrawer(ROWS)
        self._grow(actions._session.page, list(range(10, 400, 10)))
        clock = SimpleNamespace(monotonic=lambda: 10_000.0)
        with patch.object(iw, "time", clock):
            result = await withdrawer.withdraw_invitations(
                7, dry_run=False, deadline=500.0
            )
        assert result["stopped_reason"] == "list_not_fully_loaded"
        assert result["rows_seen"] == len(ROWS)
        assert result["withdrawn"] == [] and clicks == []


class TestPartialReports:
    async def test_deadline_returns_what_was_withdrawn(self, hooks):
        withdrawer, _, clicks = _withdrawer(ROWS)
        clock = SimpleNamespace(monotonic=lambda: 10_000.0 if clicks else 100.0)
        with patch.object(iw, "time", clock):
            result = await withdrawer.withdraw_invitations(
                7, dry_run=False, deadline=500.0
            )
        assert clicks == ["old-one"]
        assert [i["username"] for i in result["withdrawn"]] == ["old-one"]
        assert result["stopped_reason"] == "deadline"

    async def test_an_error_mid_run_keeps_the_withdrawn_list(self, hooks):
        withdrawer, actions, clicks = _withdrawer(ROWS)
        original = actions._session.page.evaluate
        confirms = iter([True, RuntimeError("boom")])

        async def evaluate(script, arg=None):
            if script == iw._CONFIRM_WITHDRAW_JS:
                outcome = next(confirms)
                if isinstance(outcome, Exception):
                    raise outcome
            return await original(script, arg)

        actions._session.page.evaluate = evaluate
        result = await withdrawer.withdraw_invitations(7, dry_run=False)
        assert [i["username"] for i in result["withdrawn"]] == ["old-one"]
        assert result["stopped_reason"].startswith("error: RuntimeError")


class TestPacing:
    async def test_hourly_cap_stops_before_the_click(self, hooks):
        from datetime import datetime, timezone

        from linkedin_mcp_server.exceptions import ActionLimitError

        hooks.admit.side_effect = ActionLimitError(
            "withdrawals",
            limit=40,
            window="1 h",
            resume_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        withdrawer, _, clicks = _withdrawer(ROWS)
        result = await withdrawer.withdraw_invitations(7, dry_run=False)
        assert clicks == []
        assert result["stopped_reason"] == "hourly_cap"

    async def test_working_hours_refusal_stops_the_run(self, hooks):
        from datetime import datetime, timezone

        from linkedin_mcp_server.exceptions import ActionLimitError

        hooks.admit.side_effect = ActionLimitError(
            "withdrawals",
            limit=0,
            window="working hours",
            resume_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        withdrawer, _, clicks = _withdrawer(ROWS)
        result = await withdrawer.withdraw_invitations(7, dry_run=False)
        assert clicks == []
        assert result["stopped_reason"].startswith("action_limit")

    async def test_every_withdrawal_is_admitted_and_recorded(self, hooks):
        withdrawer, _, clicks = _withdrawer(ROWS)
        await withdrawer.withdraw_invitations(7, dry_run=False)
        assert clicks == ["old-one", "old-two"]
        assert hooks.admit.call_count == 2
        assert hooks.record.call_count == 2
        assert hooks.admit.call_args.args[1] == "withdrawals"


class TestGuards:
    async def test_a_dialog_already_open_is_never_confirmed(self, hooks):
        withdrawer, actions, clicks = _withdrawer(ROWS)
        actions.state["dialogs"] = [CONFIRM_TEXT]
        result = await withdrawer.withdraw_invitations(7, dry_run=False)
        assert clicks == []
        assert result["stopped_reason"] == "dialog_already_open"

    async def test_leaving_the_sent_list_records_a_throttle_signal(self, hooks):
        withdrawer, actions, _ = _withdrawer(ROWS)
        _url_sequence(actions._session.page, _SENT_INVITATIONS_URL)
        result = await withdrawer.withdraw_invitations(7, dry_run=False)
        assert result["stopped_reason"] == "left_sent_list"
        hooks.throttle.assert_called_once()

    async def test_an_alert_with_the_row_still_listed_stops_and_signals(self, hooks):
        withdrawer, actions, _ = _withdrawer(ROWS, still_listed=True)
        actions._page_alerts = AsyncMock(return_value="You've reached a limit")
        result = await withdrawer.withdraw_invitations(7, dry_run=False)
        assert result["stopped_reason"].startswith("linkedin_alert")
        hooks.throttle.assert_called_once()

    async def test_threshold_below_one_is_rejected(self, hooks):
        withdrawer, _, _ = _withdrawer(ROWS)
        with pytest.raises(ValueError):
            await withdrawer.withdraw_invitations(0)

    async def test_negative_cap_withdraws_nothing(self, hooks):
        withdrawer, _, clicks = _withdrawer(ROWS)
        result = await withdrawer.withdraw_invitations(
            7, max_withdrawals=-3, dry_run=False
        )
        assert clicks == [] and result["withdrawn"] == []


class TestReviewFixes:
    async def test_the_dialog_must_name_the_planned_first_name(self, hooks):
        withdrawer, actions, clicks = _withdrawer(ROWS)
        actions.state["confirm_outcome"] = "name_mismatch"
        result = await withdrawer.withdraw_invitations(7, dry_run=False)
        assert clicks == ["old-one"]
        assert actions.state["confirm_args"] == ["old"]
        assert result["stopped_reason"] == "confirmation_dialog_name_mismatch"
        assert result["withdrawn"] == [] and result["unconfirmed"] == []
        hooks.record.assert_not_called()
        assert actions.state["cancels"] == 1
        assert actions.state["dialogs"] == []

    async def test_a_dialog_that_stays_open_after_confirm_is_cancelled(self, hooks):
        withdrawer, actions, _ = _withdrawer(ROWS)
        actions.state["closes_on_confirm"] = False
        await withdrawer.withdraw_invitations(7, dry_run=False)
        assert actions.state["cancels"] == 1

    async def test_a_withdrawn_toast_is_success_and_is_reported(self, hooks):
        withdrawer, actions, _ = _withdrawer(ROWS)
        actions._page_alerts = AsyncMock(return_value="Invitation WITHDRAWN")
        result = await withdrawer.withdraw_invitations(7, dry_run=False)
        assert result["stopped_reason"] is None
        assert [i["username"] for i in result["withdrawn"]] == ["old-one", "old-two"]
        assert {i["alert_text"] for i in result["withdrawn"]} == {
            "Invitation WITHDRAWN"
        }
        hooks.throttle.assert_not_called()

    async def test_another_alert_text_is_still_pushback_and_is_reported(self, hooks):
        withdrawer, actions, _ = _withdrawer(ROWS)
        actions._page_alerts = AsyncMock(return_value="Slow down")
        result = await withdrawer.withdraw_invitations(7, dry_run=False)
        assert result["stopped_reason"] == "linkedin_alert: Slow down"
        assert result["unconfirmed"][0]["alert_text"] == "Slow down"
        hooks.throttle.assert_called_once()

    async def test_only_the_first_token_of_the_name_is_sent(self, hooks):
        rows = [_row("a", "Sent 2 months ago", "William Benton, MSEE, PMP, S")]
        withdrawer, actions, _ = _withdrawer(rows)
        await withdrawer.withdraw_invitations(7, dry_run=False)
        assert actions.state["confirm_args"] == ["william"]

    async def test_a_confirm_script_error_is_charged_and_unconfirmed(self, hooks):
        withdrawer, actions, _ = _withdrawer(ROWS)
        actions.state["confirm_error"] = RuntimeError("boom")
        result = await withdrawer.withdraw_invitations(7, dry_run=False)
        assert hooks.record.call_count == 1
        assert [i["username"] for i in result["unconfirmed"]] == ["old-one"]
        assert result["withdrawn"] == []
        assert result["stopped_reason"].startswith("error: RuntimeError")

    async def test_a_row_that_comes_back_within_the_recheck_is_unconfirmed(self, hooks):
        withdrawer, actions, _ = _withdrawer(ROWS)
        actions.state["listed"] = [0, 1]
        result = await withdrawer.withdraw_invitations(7, dry_run=False)
        assert result["withdrawn"] == []
        assert [i["username"] for i in result["unconfirmed"]] == ["old-one"]
        assert result["stopped_reason"] == "withdrawal_unverified"
        slept = [c.args[0] for c in hooks.sleep.await_args_list]
        assert iw._REMOVAL_RECHECK_SECONDS in slept

    async def test_an_alert_is_read_even_when_the_row_is_gone(self, hooks):
        withdrawer, actions, _ = _withdrawer(ROWS)
        actions._page_alerts = AsyncMock(return_value="You've reached a limit")
        result = await withdrawer.withdraw_invitations(7, dry_run=False)
        assert result["stopped_reason"].startswith("linkedin_alert")
        hooks.throttle.assert_called_once()


class TestSecondReview:
    async def test_no_click_when_the_page_is_not_the_sent_list(self, hooks):
        withdrawer, actions, clicks = _withdrawer(ROWS)
        actions._session.page.url = "https://www.linkedin.com/feed/"
        assert (
            await withdrawer._withdraw_one({"username": "old-one", "name": "Old One"})
            == "left_sent_list"
        )
        assert clicks == []

    async def test_a_confirmed_but_unverified_withdrawal_is_reported(self, hooks):
        withdrawer, _, _ = _withdrawer(ROWS, still_listed=True)
        result = await withdrawer.withdraw_invitations(7, dry_run=False)
        assert result["withdrawn"] == []
        assert [i["username"] for i in result["unconfirmed"]] == ["old-one"]
        assert result["stopped_reason"] == "withdrawal_unverified"

    async def test_leaving_the_list_after_confirm_is_unconfirmed(self, hooks):
        withdrawer, actions, _ = _withdrawer(ROWS)
        _url_sequence(actions._session.page, _SENT_INVITATIONS_URL)
        result = await withdrawer.withdraw_invitations(7, dry_run=False)
        assert [i["username"] for i in result["unconfirmed"]] == ["old-one"]
        assert result["stopped_reason"] == "left_sent_list"

    async def test_confirm_press_is_charged_even_if_the_dialog_stays_open(self, hooks):
        withdrawer, actions, _ = _withdrawer(ROWS)
        actions.state["closes_on_confirm"] = False
        result = await withdrawer.withdraw_invitations(7, dry_run=False)
        assert hooks.record.call_count == 1
        assert [i["username"] for i in result["unconfirmed"]] == ["old-one"]

    async def test_a_failing_budget_read_keeps_the_report_and_clicks_nothing(
        self, hooks
    ):
        hooks.admit.side_effect = OSError("lock timeout")
        withdrawer, _, clicks = _withdrawer(ROWS)
        result = await withdrawer.withdraw_invitations(7, dry_run=False)
        assert clicks == []
        assert result["stopped_reason"].startswith("error: OSError")

    async def test_a_failing_cooldown_read_stops_without_a_click(self, hooks):
        withdrawer, _, clicks = _withdrawer(ROWS)
        with patch.object(iw, "cooldown_resume_at", side_effect=ValueError("bad file")):
            result = await withdrawer.withdraw_invitations(7, dry_run=False)
        assert clicks == []
        assert result["stopped_reason"].startswith("error: ValueError")

    def test_age_cannot_span_lines(self):
        assert parse_age_days("Sent\n3 weeks ago") is None
        assert parse_age_days("Sent 3\nweeks ago") is None
        assert parse_age_days("Sent 3 weeks\nago") is None
