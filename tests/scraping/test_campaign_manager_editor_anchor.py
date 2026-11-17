"""The editor waits for its controls to render; a page that never renders them
is control_unavailable with the page's anchor structure in the failure notes.

Synthetic data only: the scripted app of the editor tests stands in for
Campaign Manager.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.error_diagnostics import build_issue_diagnostics
from linkedin_mcp_server.response_structure import current_log
from linkedin_mcp_server.scraping import campaign_manager_write as write_module
from linkedin_mcp_server.scraping.campaign_manager_write import ANCHORS

from .test_campaign_manager_editor import CAMPAIGN, _App, _campaign, _update

_SUMMARY = {
    "anchors": {name: {"count": 0, "visible": 0} for name in ANCHORS},
    "loading": True,
}


def _never_renders(page: Any, app: _App) -> list[tuple[str, Any]]:
    """The editor never appears: every anchor wait times out."""
    waits: list[tuple[str, Any]] = []

    async def wait_for_function(script: str, *, arg: Any, **kwargs: Any) -> None:
        waits.append((arg, kwargs["timeout"]))
        raise PlaywrightTimeoutError("not rendered")

    async def evaluate(script: str, arg: Any = None) -> Any:
        if script == write_module._STRUCTURE_JS:
            return _SUMMARY
        return await app.evaluate(script, arg)

    page.wait_for_function = AsyncMock(side_effect=wait_for_function)
    page.evaluate = AsyncMock(side_effect=evaluate)
    return waits


async def test_a_budget_that_never_renders_is_control_unavailable_and_noted(
    mock_page,
):
    app = _App(mock_page, _campaign())
    app.states["bid-and-budget__daily-budget"] = "missing"
    waits = _never_renders(mock_page, app)

    result = await _update(mock_page, app, daily_budget="12.5", confirm_send=False)

    assert result["status"] == "control_unavailable"
    assert waits == [(ANCHORS["daily_budget"], 15000.0)]
    log = current_log()
    assert log is not None
    (page,) = log.pages
    assert page["loading"] is True
    assert page["anchors"]["daily_budget"] == {"count": 0, "visible": 0}
    assert CAMPAIGN not in page["path"]
    assert page["path"].endswith("/campaigns/N/details")


async def test_a_dry_run_that_waits_types_focuses_and_sends_nothing(mock_page):
    app = _App(mock_page, _campaign())
    app.states["bid-and-budget__daily-budget"] = "missing"
    _never_renders(mock_page, app)

    await _update(mock_page, app, daily_budget="12.5", confirm_send=False)

    mock_page.keyboard.insert_text.assert_not_awaited()
    assert app.focused is None
    assert app.sent == []
    assert not any(arg.get("focus") for name, arg in app.calls if name == "input")


async def test_after_one_timeout_a_page_is_only_checked_once_more(mock_page):
    app = _App(mock_page, _campaign())
    waits = _never_renders(mock_page, app)
    flow = write_module.CampaignWriteFlow(AsyncMock(), AsyncMock())

    await flow.wait_for_anchor(mock_page, ANCHORS["daily_budget"])
    await flow.wait_for_anchor(mock_page, ANCHORS["start_date"])
    mock_page.url = mock_page.url.replace("/details", "/summary")
    await flow.wait_for_anchor(mock_page, ANCHORS["status_switch"])

    assert [timeout for _, timeout in waits] == [15000.0, 1, 15000.0]


async def test_the_notes_carry_the_page_structure_without_values(mock_page):
    app = _App(mock_page, _campaign())
    app.states["bid-and-budget__daily-budget"] = "missing"
    _never_renders(mock_page, app)
    await _update(mock_page, app, daily_budget="12.5", confirm_send=False)

    notes = build_issue_diagnostics(
        RuntimeError("control unavailable"), context="update_campaign"
    )

    with open(notes["issue_template_path"], encoding="utf-8") as handle:
        text = handle.read()
    section = text.split("## Page structure when a control was not found")[1]
    assert '"daily_budget": {"count": 0, "visible": 0}' in section
    assert '"loading": true' in section
    assert CAMPAIGN not in section
