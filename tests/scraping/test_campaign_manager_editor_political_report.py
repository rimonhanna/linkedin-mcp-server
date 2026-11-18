"""update_campaign reports what became of the caller's political-ad declaration.

A declaration counts as recorded only when a save that left carried it and
the ad set read back holds it; otherwise the result says so plainly, while the
other fields' outcome stands. Synthetic data only: the scripted app of the
editor tests stands in for Campaign Manager.
"""

from __future__ import annotations

from .test_campaign_manager_editor import CAMPAIGN, _set, _update
from .test_campaign_manager_editor_political import BUDGET, _asking, _eu_app


async def test_a_dry_run_shows_the_declared_intent_in_planned(mock_page):
    app = _eu_app(mock_page)

    result = await _update(
        mock_page, app, daily_budget="11", political_ad=True, confirm_send=False
    )

    assert result["status"] == "confirmation_required"
    assert result["planned"]["politicalIntent"] == "POLITICAL"
    assert result["planned"]["politicalAdQuestion"] == "will"


async def test_an_answered_declaration_that_reads_back_is_reported_recorded(mock_page):
    app = _eu_app(mock_page)
    _asking(app, _set(dailyBudget=BUDGET, politicalIntent="NOT_POLITICAL"))

    result = await _update(mock_page, app, daily_budget="11", political_ad=False)

    assert result["status"] == "updated", result["message"]
    assert result["planned"]["politicalIntent"] == "NOT_POLITICAL"
    assert result["applied"]["politicalIntent"] == "NOT_POLITICAL"
    assert "declaration was recorded" in result["message"]


async def test_a_declaration_no_dialog_asked_for_is_reported_not_recorded(mock_page):
    app = _eu_app(mock_page)
    # Save and exit saves without asking: the declaration never leaves.
    app.on_footer = _set(dailyBudget=BUDGET)

    result = await _update(mock_page, app, daily_budget="11", political_ad=True)

    assert result["status"] == "updated", result["message"]
    assert app.records[CAMPAIGN]["dailyBudget"] == BUDGET
    assert result["applied"]["politicalIntent"] is None
    assert "declaration is not recorded" in result["message"]
    assert "was recorded" not in result["message"]
