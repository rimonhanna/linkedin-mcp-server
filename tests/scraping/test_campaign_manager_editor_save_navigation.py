"""A page that navigates while the editor polls or notes a stop.

Save and exit leaves the page once LinkedIn took the save, which destroys the
page's execution context under a running check. Those checks and the stop
notes are diagnostics: the outcome stays the recorded write's. Synthetic
data only: the scripted app of the editor tests stands in for Campaign
Manager.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from patchright.async_api import Error as PlaywrightError

from linkedin_mcp_server.scraping.campaign_manager_political import political_dialog

from .test_campaign_manager_editor import (
    CAMPAIGN,
    _App,
    _budget_autosave,
    _campaign,
    _set,
    _update,
)
from .test_campaign_manager_editor_political import BUDGET, _eu_app

_GONE = "Execution context was destroyed, most likely because of a navigation"


def _raising(app: _App, program: str, before: Any = None) -> None:
    """``program`` raises a navigation's error, after running ``before`` once."""
    scripted = app.evaluate

    async def evaluate(script: str, arg: Any = None) -> Any:
        if app.programs.get(script) == program:
            if before is not None:
                await before()
            raise PlaywrightError(_GONE)
        return await scripted(script, arg)

    app.page.evaluate.side_effect = evaluate


async def test_a_political_check_cut_by_navigation_reads_as_no_dialog():
    page = AsyncMock()
    page.evaluate.side_effect = PlaywrightError(_GONE)

    assert await political_dialog(page, False) == "absent"


async def test_a_confirm_that_navigates_still_reports_the_recorded_save(mock_page):
    app = _eu_app(mock_page)
    sent: list[bool] = []

    async def confirm_then_leave() -> None:
        if not sent:
            sent.append(True)
            await app.send(
                f"campaignManagerCampaigns/{CAMPAIGN}",
                _set(dailyBudget=BUDGET, politicalIntent="NOT_POLITICAL"),
                "partial_update",
            )

    app.political = AsyncMock()
    _raising(app, "political", confirm_then_leave)

    result = await _update(mock_page, app, daily_budget="11", political_ad=False)

    assert result["status"] == "updated", result["message"]
    assert app.records[CAMPAIGN]["dailyBudget"] == BUDGET


async def test_a_dialog_check_cut_by_navigation_still_reports_the_save(mock_page):
    app = _eu_app(mock_page)
    sent: list[bool] = []

    async def save_then_leave() -> None:
        if not sent:
            sent.append(True)
            await app.send(
                f"campaignManagerCampaigns/{CAMPAIGN}",
                _set(dailyBudget=BUDGET),
                "partial_update",
            )

    _raising(app, "dialog", save_then_leave)

    result = await _update(mock_page, app, daily_budget="11")

    assert result["status"] == "updated", result["message"]


async def test_a_stop_note_cut_by_navigation_keeps_the_stop(mock_page):
    app = _App(mock_page, _campaign())
    app.on_blur = _budget_autosave
    app.save_status = 400
    _raising(app, "input_state")

    result = await _update(mock_page, app, daily_budget="12.5")

    assert result["status"] == "save_failed"
    assert result["retry_safe"] is True
