"""Browser-DOM tests for the Campaign Manager editor's page programs.

The documents are synthetic: each case is a claim about a program, never about
LinkedIn's markup. Labels come in two locales to show no text is read. Nothing
reaches LinkedIn; skipped when chromium is not installed.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping import campaign_manager_editor as editor

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

_LABELS = {
    "en": {"budget": "Daily budget", "next": "Next month", "prev": "Previous month"},
    "de": {
        "budget": "Tagesbudget",
        "next": "Nächster Monat",
        "prev": "Vorheriger Monat",
    },
}

_LOG = """<script>
function log(entry) {
    document.body.dataset.log = (document.body.dataset.log || '') + entry + '|';
}
</script>"""


@pytest.fixture
async def dom_page():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            page = await (await browser.new_context()).new_page()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        page.set_default_timeout(1500)
        try:
            yield page
        finally:
            await browser.close()


async def _log(page: Any) -> list[str]:
    text = await page.evaluate("document.body.dataset.log || ''")
    return [entry for entry in text.split("|") if entry]


# --- budget input ----------------------------------------------------------------


@pytest.mark.parametrize("locale", sorted(_LABELS))
async def test_budget_input_receives_one_input_event_with_the_amount(dom_page, locale):
    label = _LABELS[locale]["budget"]
    await dom_page.set_content(
        _LOG + "<div data-live-test-bid-and-budget>"
        f'<input id="bid-and-budget__daily-budget" aria-label="{label}" value="10" '
        "oninput=\"log('input:' + this.value)\" "
        "onchange=\"log('change:' + this.value)\"></div>"
        f'<input id="other" aria-label="{label}">'
    )

    done = await editor.set_text(
        dom_page,
        "bid-and-budget__daily-budget",
        "[data-live-test-bid-and-budget]",
        "12.5",
    )

    assert done is True
    assert await _log(dom_page) == ["input:12.5", "change:12.5"]


async def test_budget_input_outside_its_block_is_not_typed_into(dom_page):
    await dom_page.set_content(
        _LOG + '<input id="bid-and-budget__daily-budget" value="10" '
        "oninput=\"log('input')\">"
    )

    done = await editor.set_text(
        dom_page, "bid-and-budget__daily-budget", "[data-live-test-bid-and-budget]", "1"
    )

    assert done is False
    assert await _log(dom_page) == []


# --- calendar --------------------------------------------------------------------


def _calendar_page(labels: dict[str, str], *, year: int, month: int, fixed: int = 0):
    """A start and an end calendar, pre-rendered and hidden; the end one opens.

    Every month shows the neighbours' days as shadows, so day numbers repeat.
    ``fixed`` renders that many days whatever the month.
    """
    return (
        _LOG
        + '<input name="start" data-datepicker-input value="a">'
        + '<input name="end" data-datepicker-input value="b" '
        "onclick=\"document.getElementById('end').style.display = 'block'\">"
        + '<div data-calendar-root id="start" style="display:none">'
        "<button data-calendar-next-month onclick=\"log('wrong-root')\">&gt;</button>"
        + "".join(
            f'<button type="button" data-calendar-day data-daynum="{d}" '
            f"onclick=\"log('wrong-root')\">{d}</button>"
            for d in range(1, 32)
        )
        + "</div>"
        + '<div data-calendar-root id="end" style="display:none">'
        f'<button data-calendar-prev-month aria-label="{labels["prev"]}" '
        'onclick="shift(-1)">&lt;</button>'
        f'<button data-calendar-next-month aria-label="{labels["next"]}" '
        'onclick="shift(1)">&gt;</button>'
        '<div id="days"></div></div>'
        + f"""<script>
let year = {year}, month = {month - 1};
function render() {{
    const count = {fixed} || new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
    const shadow = (d) => '<button type="button" data-calendar-day data-daynum="'
        + d + '-shadow" onclick="log(\\'shadow:' + d + '\\')">' + d + '</button>';
    let html = shadow(30) + shadow(31);
    for (let d = 1; d <= count; d++) {{
        html += '<button type="button" data-calendar-day data-daynum="' + d
            + '" onclick="pick(' + d + ')">' + d + '</button>';
    }}
    for (let d = 1; d <= 6; d++) html += shadow(d);
    document.getElementById('days').innerHTML = html;
}}
function shift(step) {{
    month += step;
    if (month > 11) {{ month = 0; year += 1; }}
    if (month < 0) {{ month = 11; year -= 1; }}
    log('step:' + step);
    setTimeout(render, 0);
}}
function pick(d) {{
    log('day:' + (month + 1) + '-' + d);
    document.getElementById('end').style.display = 'none';
}}
document.addEventListener('keydown', (event) => {{
    if (event.key === 'Escape') {{
        document.getElementById('end').style.display = 'none';
        log('escape');
    }}
}});
render();
</script>"""
    )


@pytest.mark.parametrize("locale", sorted(_LABELS))
async def test_calendar_steps_forward_and_clicks_the_real_day(dom_page, locale):
    await dom_page.set_content(_calendar_page(_LABELS[locale], year=2030, month=1))

    done = await editor.pick_date(dom_page, "end", date(2030, 1, 20), date(2030, 3, 5))

    assert done is True
    # Day 5 also shows as a shadow of the next month; only the real one is used.
    assert await _log(dom_page) == ["step:1", "step:1", "day:3-5"]


async def test_calendar_steps_back(dom_page):
    await dom_page.set_content(_calendar_page(_LABELS["en"], year=2030, month=3))

    done = await editor.pick_date(dom_page, "end", date(2030, 3, 9), date(2030, 2, 28))

    assert done is True
    assert await _log(dom_page) == ["step:-1", "day:2-28"]


async def test_calendar_with_the_wrong_day_count_is_never_clicked(dom_page):
    # February shown with 31 days: not the month the input holds.
    await dom_page.set_content(
        _calendar_page(_LABELS["en"], year=2030, month=2, fixed=31)
    )

    done = await editor.pick_date(dom_page, "end", date(2030, 2, 10), date(2030, 2, 12))

    assert done is False
    assert await _log(dom_page) == ["escape"]


async def test_check_calendar_opens_checks_and_closes(dom_page):
    await dom_page.set_content(_calendar_page(_LABELS["de"], year=2030, month=4))

    assert await editor.check_calendar(dom_page, "end", date(2030, 4, 2)) is True
    assert await _log(dom_page) == ["escape"]


# --- status switch -------------------------------------------------------------------


def _switch(*, checked: bool, label: str) -> str:
    return (
        _LOG + '<div class="campaign-header-status__toggle-container">'
        f'<input type="checkbox" role="switch" aria-label="{label}" '
        f"{'checked' if checked else ''} onclick=\"log('switch')\"></div>"
    )


@pytest.mark.parametrize("label", ["Active", "Aktiv"])
async def test_status_switch_is_clicked_only_when_it_matches_the_record(
    dom_page, label
):
    await dom_page.set_content(_switch(checked=False, label=label))
    mismatch = await editor.toggle_status(dom_page, checked=True, click=True)
    assert mismatch == "mismatch"
    assert await _log(dom_page) == []

    ready = await editor.toggle_status(dom_page, checked=False, click=True)
    assert ready == "ready"
    assert await _log(dom_page) == ["switch"]


async def test_disabled_status_switch_is_not_clicked(dom_page):
    await dom_page.set_content(
        _switch(checked=True, label="Active").replace("<input ", "<input disabled ")
    )

    assert await editor.toggle_status(dom_page, checked=True, click=True) == "disabled"
    assert await _log(dom_page) == []


# --- objective -------------------------------------------------------------------


@pytest.mark.parametrize("label", ["Website visits", "Websitebesuche"])
async def test_objective_is_chosen_by_value(dom_page, label):
    await dom_page.set_content(
        _LOG
        + "".join(
            f'<label><input type="radio" name="objective-selection" value="{value}" '
            f"onclick=\"log('{value}')\" {extra}>{label}</label>"
            for value, extra in (
                ("BRAND_AWARENESS", ""),
                ("WEBSITE_VISIT", ""),
                ("JOB_APPLICANT", "disabled"),
            )
        )
    )

    blocked = await dom_page.evaluate(
        editor._OBJECTIVE_JS, {"value": "JOB_APPLICANT", "click": True}
    )
    chosen = await dom_page.evaluate(
        editor._OBJECTIVE_JS, {"value": "WEBSITE_VISIT", "click": True}
    )

    assert blocked == {
        "state": "unavailable",
        "available": ["BRAND_AWARENESS", "WEBSITE_VISIT"],
    }
    assert chosen["state"] == "ready"
    assert await _log(dom_page) == ["WEBSITE_VISIT"]
