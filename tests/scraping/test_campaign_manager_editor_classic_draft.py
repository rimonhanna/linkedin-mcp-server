"""A draft in the classic editor saves by Save and exit, never by autosave; a
change that did not save leaves its write window's structure in the notes.

Synthetic data only: the scripted app of the editor tests stands in for
Campaign Manager.
"""

from __future__ import annotations

from linkedin_mcp_server.error_diagnostics import build_issue_diagnostics

from .test_campaign_manager_editor import (
    CAMPAIGN,
    DETAILS,
    _App,
    _budget_autosave,
    _campaign,
    _set,
    _update,
)


def _footer_clicks(app: _App) -> list[object]:
    return [arg for name, arg in app.calls if name == "footer"]


async def test_classic_draft_saves_by_save_and_exit(mock_page):
    app = _App(mock_page, _campaign())
    app.classic = True
    app.on_footer = _set(dailyBudget={"amount": "11", "currencyCode": "EUR"})

    result = await _update(mock_page, app, daily_budget="11")

    assert result["status"] == "updated", result["message"]
    assert result["applied"] == {"dailyBudget": {"amount": "11", "currencyCode": "EUR"}}
    # A draft's footer holds Previous and Save and exit before Next.
    assert _footer_clicks(app) == [2]
    assert app.sent == [(f"campaignManagerCampaigns/{CAMPAIGN}", "fallback")]


async def test_classic_draft_does_not_wait_for_an_autosave(mock_page):
    app = _App(mock_page, _campaign())
    app.classic = True
    app.on_footer = _set(dailyBudget={"amount": "11", "currencyCode": "EUR"})
    # An autosave the classic editor never sends would be a second save.
    app.on_blur = _budget_autosave

    result = await _update(mock_page, app, daily_budget="11")

    assert result["status"] == "updated", result["message"]
    assert _footer_clicks(app) == [2]


async def test_dialog_instead_of_a_save_is_closed_and_named(mock_page):
    app = _App(mock_page, _campaign())
    app.classic = True
    app.dialog = True

    result = await _update(mock_page, app, daily_budget="11")

    assert result["status"] == "control_unavailable"
    assert result["retry_safe"] is True
    assert "dialog was closed" in result["message"]
    assert "nothing was sent" in result["message"]
    mock_page.keyboard.press.assert_any_await("Escape")
    assert app.sent == []
    assert app.records[CAMPAIGN]["dailyBudget"]["amount"] == "10"
    assert app.navigated[-1] == DETAILS


async def test_autosave_that_never_comes_is_noted_without_values(mock_page):
    app = _App(mock_page, _campaign())

    result = await _update(mock_page, app, daily_budget="12.5")

    assert result["status"] == "control_unavailable"
    assert result["message"] == (
        "Campaign Manager did not send a save for the change, so nothing was sent."
    )
    notes = build_issue_diagnostics(
        RuntimeError(result["message"]), context="update_campaign"
    )
    with open(notes["issue_template_path"], encoding="utf-8") as handle:
        text = handle.read()
    section = text.split("## Write window when a change did not save")[1]
    section = section.split("## ")[0]
    assert '"saves": "autosave"' in section
    assert '"outcome": "timeout"' in section
    assert '"step": "dailyBudget"' in section
    assert '"admitted": []' in section
    assert '"attached": true' in section
    assert "12.5" not in section
    assert CAMPAIGN not in section


async def test_refused_save_by_the_footer_is_noted_with_field_names(mock_page):
    app = _App(mock_page, _campaign())
    app.classic = True
    app.on_footer = _set(
        dailyBudget={"amount": "11", "currencyCode": "EUR"}, name="Other name"
    )

    result = await _update(mock_page, app, daily_budget="11")

    assert result["status"] == "change_blocked"
    notes = build_issue_diagnostics(
        RuntimeError(result["message"]), context="update_campaign"
    )
    with open(notes["issue_template_path"], encoding="utf-8") as handle:
        text = handle.read()
    section = text.split("## Write window when a change did not save")[1]
    section = section.split("## ")[0]
    assert '"saves": "save_and_exit"' in section
    assert '"outcome": "refused"' in section
    assert '"set_keys": ["dailyBudget", "name"]' in section
    assert '"restli_method": "partial_update"' in section
    assert "Other name" not in section
