"""update_campaign and the political-ad declaration the app writes itself.

The classic Save and exit resets ``politicalIntent`` to ``NOT_DECLARED`` for
targeting without an EU location; the newer editor fills ``NOT_POLITICAL``
in its autosave. Synthetic data only: the scripted app of the editor tests
stands in for Campaign Manager.
"""

from __future__ import annotations

from typing import Any

from .test_campaign_manager_editor import CAMPAIGN, _App, _campaign, _set, _update

BUDGET = {"amount": "11", "currencyCode": "EUR"}


def _classic(mock_page: Any, **record: Any) -> _App:
    app = _App(mock_page, _campaign(status="ACTIVE", **record))
    app.classic = True
    app.on_footer = _set(dailyBudget=BUDGET, politicalIntent="NOT_DECLARED")
    return app


async def test_reset_on_an_undeclared_ad_set_is_admitted(mock_page):
    app = _classic(mock_page)

    result = await _update(mock_page, app, daily_budget="11")

    assert result["status"] == "updated", result["message"]
    assert app.records[CAMPAIGN]["politicalIntent"] == "NOT_DECLARED"


async def test_reset_on_a_not_declared_ad_set_is_admitted(mock_page):
    app = _classic(mock_page, politicalIntent="NOT_DECLARED")

    result = await _update(mock_page, app, daily_budget="11")

    assert result["status"] == "updated", result["message"]


async def test_reset_that_erases_a_declaration_is_refused(mock_page):
    app = _classic(mock_page, politicalIntent="NOT_POLITICAL")

    result = await _update(mock_page, app, daily_budget="11")

    assert result["status"] == "declaration_would_clear"
    assert result["retry_safe"] is True
    assert "political_ad" in result["message"]
    assert app.records[CAMPAIGN]["politicalIntent"] == "NOT_POLITICAL"
    assert app.records[CAMPAIGN]["dailyBudget"] != BUDGET


async def test_reset_despite_a_given_declaration_changes_nothing(mock_page):
    app = _classic(mock_page, politicalIntent="POLITICAL")

    result = await _update(mock_page, app, daily_budget="11", political_ad=True)

    assert result["status"] == "declaration_cleared_by_linkedin"
    assert result["retry_safe"] is True
    assert app.records[CAMPAIGN]["politicalIntent"] == "POLITICAL"
    assert app.records[CAMPAIGN]["dailyBudget"] != BUDGET


def _newer_draft(mock_page: Any) -> _App:
    """A draft in the newer editor: its autosave fills NOT_POLITICAL."""
    app = _App(mock_page, _campaign(politicalIntent="NOT_DECLARED"))
    app.on_blur = lambda _input, text: _set(
        dailyBudget={"amount": text, "currencyCode": "EUR"},
        politicalIntent="NOT_POLITICAL",
    )
    return app


async def test_newer_editor_fill_passes_with_a_not_political_declaration(mock_page):
    app = _newer_draft(mock_page)

    result = await _update(mock_page, app, daily_budget="11", political_ad=False)

    assert result["status"] == "updated", result["message"]
    assert app.records[CAMPAIGN]["politicalIntent"] == "NOT_POLITICAL"


async def test_newer_editor_fill_without_a_declaration_asks_for_one(mock_page):
    app = _newer_draft(mock_page)

    result = await _update(mock_page, app, daily_budget="11")

    assert result["status"] == "declaration_required"
    assert result["retry_safe"] is True
    assert "political_ad false" in result["message"]
    assert app.records[CAMPAIGN]["politicalIntent"] == "NOT_DECLARED"


async def test_newer_editor_fill_with_a_political_declaration_is_refused(mock_page):
    app = _newer_draft(mock_page)

    result = await _update(mock_page, app, daily_budget="11", political_ad=True)

    assert result["status"] == "declaration_not_offered"
    assert result["retry_safe"] is True
    assert "newer editor" in result["message"]
    assert app.records[CAMPAIGN]["politicalIntent"] == "NOT_DECLARED"
