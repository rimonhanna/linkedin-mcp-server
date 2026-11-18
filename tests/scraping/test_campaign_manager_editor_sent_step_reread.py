"""A step that fails after its save left is read back, never "nothing was sent".

The newer editor saves a draft's date as the day is pressed, so a calendar
that stays open after the press can fail the pick with the save already
gone. Synthetic data only.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .test_campaign_manager_editor import (
    CAMPAIGN,
    _App,
    _campaign,
    _create,
    _ms,
    _set,
    _status,
    _update,
)
from .test_campaign_manager_editor_calendar_press import _StaysOpen
from .test_campaign_manager_editor_new_draft import _NewDraftApp


async def test_pick_failing_after_its_autosave_left_is_read_back(mock_page):
    app = _StaysOpen(mock_page, _campaign())
    old_end = _campaign()["runSchedule"]["end"]
    app.picks = [_set(runSchedule={"start": _ms(date(2030, 1, 15)), "end": old_end})]

    result = await _update(mock_page, app, start_date=date(2030, 1, 15))

    assert result["status"] == "save_unverified", result["message"]
    assert "nothing" not in result["message"].lower(), result["message"]
    assert "already sent a save" in result["message"]
    assert result["retry_safe"] is False
    assert result["applied"] == {"startDate": "2030-01-15"}
    assert app.sent == [(f"campaignManagerCampaigns/{CAMPAIGN}", "fallback")]


async def test_pick_failing_with_nothing_sent_still_says_so(mock_page):
    app = _StaysOpen(mock_page, _campaign())

    result = await _update(mock_page, app, start_date=date(2030, 1, 15))

    assert result["status"] == "control_unavailable", result["message"]
    assert "nothing of it was sent" in result["message"]
    assert "applied" not in result
    assert app.sent == []


class _BlurStays(_NewDraftApp):
    """The new ad set's name input keeps focus after Tab."""

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        answer = await super().evaluate(script, arg)
        if self.programs.get(script) == "blur":
            return {"blurred": False}
        return answer


async def test_a_name_that_did_not_take_creates_nothing(mock_page):
    app = _BlurStays(mock_page)

    result = await _create(mock_page, app)

    assert result["status"] == "control_unavailable", result["message"]
    assert "Nothing was created" in result["message"]
    assert result["retry_safe"] is True
    assert "created_id" not in result
    assert app.sent == []


async def test_status_save_without_a_clear_answer_is_read_back(mock_page):
    app = _App(mock_page, _campaign(status="ACTIVE"))
    app.save_status = 503

    result = await _status(mock_page, app, "PAUSED")

    assert result["status"] == "save_unverified", result["message"]
    assert result["retry_safe"] is False
    assert result["applied"] == {"status": "ACTIVE"}
