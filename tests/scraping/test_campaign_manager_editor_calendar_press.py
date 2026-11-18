"""A date is picked only when the calendar took the day.

Campaign Manager's calendar selects a day on ``mouseup`` and then closes
itself; a bare ``click()`` left a draft's passed start date in place, and Save
and exit stayed disabled. Synthetic documents and data only.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from linkedin_mcp_server.scraping import campaign_manager_editor as editor

from .test_campaign_manager_editor import (
    TODAY,
    _App,
    _campaign,
    _ms,
    _update,
)
from .test_campaign_manager_editor_dom import _log, dom_page  # noqa: F401


def _calendar(*, selects: bool) -> str:
    """One start calendar on November 2030, selecting on mouseup like the app's."""
    days = "".join(
        f'<button type="button" data-calendar-day data-daynum="{d}">{d}</button>'
        for d in range(1, 31)
    )
    handler = (
        """root.addEventListener('mouseup', (event) => {
    const day = event.target.closest('[data-daynum]');
    if (event.button !== 0 || !day) return;
    input.value = 'picked ' + day.dataset.daynum;
    log('day:' + day.dataset.daynum);
    root.style.display = 'none';
});"""
        if selects
        else ""
    )
    return f"""<script>
function log(entry) {{
    document.body.dataset.log = (document.body.dataset.log || '') + entry + '|';
}}
</script>
<input name="start" data-datepicker-input value="before"
    onclick="document.getElementById('cal').style.display = 'block'">
<div data-calendar-root id="cal" style="display:none">{days}</div>
<script>
const root = document.getElementById('cal');
const input = document.querySelector('input[name=start]');
{handler}
document.addEventListener('keydown', (event) => {{
    if (event.key === 'Escape') {{ root.style.display = 'none'; log('escape'); }}
}});
</script>"""


@pytest.mark.browser_dom
@pytest.mark.xdist_group("browser_runtime")
@pytest.mark.parametrize("selects", [True, False])
async def test_day_is_pressed_so_a_mouseup_calendar_takes_it(dom_page, selects):  # noqa: F811
    await dom_page.set_content(_calendar(selects=selects))

    done = await editor.pick_date(
        dom_page, "start", date(2030, 11, 17), date(2030, 11, 18)
    )

    value = await dom_page.evaluate("document.querySelector('input').value")
    if selects:
        assert done is True
        assert await _log(dom_page) == ["day:18"]
        assert value == "picked 18"
    else:
        # A calendar still open did not take the day: closed, and reported.
        assert done is False
        assert await _log(dom_page) == ["escape"]
        assert value == "before"


class _StaysOpen(_App):
    """The calendar that never takes the day: it stays open after the press."""

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        picked = any(
            name == "calendar" and call["mode"] == "pick" for name, call in self.calls
        )
        if self.programs.get(script) == "calendar" and arg["mode"] == "closed":
            if picked:
                self.calls.append(("calendar", arg))
                return {"ok": False}
        return await super().evaluate(script, arg)


def _passed_draft() -> dict[str, Any]:
    # A draft whose start day passed on the real clock, which is what the
    # disabled-footer reason reads; the plan's today is the suite's.
    return _campaign(
        runSchedule={
            "start": _ms(datetime.now(UTC).date() - timedelta(days=1)),
            "end": _ms(TODAY + timedelta(days=7)),
        }
    )


async def test_untaken_start_date_stops_before_save_and_exit(mock_page):
    app = _StaysOpen(mock_page, _passed_draft())
    app.classic = True

    result = await _update(
        mock_page, app, start_date=TODAY + timedelta(days=1), political_ad=False
    )

    assert result["status"] == "control_unavailable", result["message"]
    assert "startDate control did not behave" in result["message"]
    assert "start date has passed" not in result["message"]
    assert [arg for name, arg in app.calls if name == "footer"] == []
    assert app.sent == []


class _SaveDisabled(_App):
    """The day is taken, yet Save and exit stays disabled for another field."""

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if self.programs.get(script) == "footer":
            self.calls.append(("footer", arg))
            return {"clicked": False, "count": 2, "disabled": True}
        if script not in self.programs:
            return None  # the page structure the stop records
        return await super().evaluate(script, arg)


async def test_a_moved_start_is_not_named_when_save_stays_disabled(mock_page):
    app = _SaveDisabled(mock_page, _passed_draft())
    app.classic = True

    result = await _update(
        mock_page, app, start_date=TODAY + timedelta(days=1), political_ad=False
    )

    assert result["status"] == "control_unavailable", result["message"]
    assert "fields is invalid" in result["message"], result["message"]
    assert "start date has passed" not in result["message"]
    assert app.sent == []
