"""Campaign Manager edits: budget and schedule, pause and resume, new drafts.

API naming as in the reads: a campaign is the UI's "Ad set", a campaign group
its "Campaign". Every tool runs the three phases of ``CampaignWriteFlow``: a
latched read that verifies the target and every control, a write window that
lets through only the app's own save that matches the plan, and a latched
re-read that confirms what LinkedIn kept.

Nothing here launches a draft or touches billing: the only status change
admitted is a pause or a resume of an active or paused ad set, and every other
write the app attempts (a billing configuration, a campaign group) is aborted.
``create_campaign_draft`` puts the draft in the chosen campaign group and never
creates a group: an ad account without that group is refused.

Per the AGENTS.md Scraping Rules no visible text is read: controls are found by
id, ``data-*`` attribute, role and structure, values by the app's own JSON.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import re
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

from patchright.async_api import Error as PlaywrightError

from linkedin_mcp_server.core.exceptions import (
    LinkedInScraperException,
)
from linkedin_mcp_server.scraping.campaign_manager import _SETUP_PATH
from linkedin_mcp_server.scraping.campaign_manager_api import (
    CampaignManagerCapture,
    CmResponse,
    CmWriteResponse,
)
from linkedin_mcp_server.scraping.campaign_manager_changes import (
    CampaignEditPlan,
    Refusal,
    admit_campaign_create,
    admit_campaign_patch,
    admit_status_patch,
    format_budget,
    ms_to_date,
    parse_budget,
    plan_campaign_edit,
    refuse_invalid_campaign_edit,
    refuse_invalid_draft,
    targeting_keys,
)
from linkedin_mcp_server.scraping.campaign_manager_guard import (
    CmWriteRequest,
    WriteWindow,
    explicit_campaign_write,
)
from linkedin_mcp_server.scraping.campaign_manager_payload import (
    envelope_elements,
    envelope_record,
    same_id,
)
from linkedin_mcp_server.scraping.campaign_manager_political import (
    DECLARATION_REQUIRED,
    POLITICAL_INTENT,
    UNANSWERED,
    declaration_note,
    declaration_stop,
    declared_intent,
    political_dialog,
    political_question,
    question_sentence,
    refuse_intent,
    sent_intent,
)
from linkedin_mcp_server.scraping.campaign_manager_results import (
    campaign_action_result,
    retry_safe_from,
)
from linkedin_mcp_server.scraping.campaign_manager_write import (
    ANCHORS,
    CampaignWriteFlow,
    footer_disabled_reason,
    record_write_window,
)
from linkedin_mcp_server.scraping.identifiers import (
    campaign_manager_url,
    normalize_ad_account_id,
    normalize_campaign_group_id,
    normalize_campaign_id,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

logger = logging.getLogger(__name__)

_UPDATE = "update_campaign"
_STATUS = "set_campaign_status"
_CREATE = "create_campaign_draft"

_READY_TIMEOUT = 12.0
_SAVE_TIMEOUT = 20.0
# A resume whose app opens its reactivation dialog instead of saving.
_DIALOG_GRACE = 5.0
_POLL_INTERVAL = 0.25
_POLL_ATTEMPTS = 12

_BUDGET_SCOPE = "[data-live-test-bid-and-budget]"
_BUDGET_INPUTS = {
    "daily": "bid-and-budget__daily-budget",
    "lifetime": "bid-and-budget__total-budget",
}
_NAME_INPUT = "campaign-name"
_FIELDS = {
    "daily": "dailyBudget",
    "lifetime": "totalBudget",
    "start": "startDate",
    "end": "endDate",
}
_NUMERIC = re.compile(r"\d{1,20}")
_STEP_ANCHORS = {
    "daily": ANCHORS["daily_budget"],
    "lifetime": ANCHORS["total_budget"],
    "start": ANCHORS["start_date"],
    "end": ANCHORS["end_date"],
}

# One text input by id, optionally inside ``scope``; focusing selects its
# whole value so one inserted text replaces it. The app re-renders a budget
# input on focus (its formatted amount becomes the raw number), which moves the
# caret after the old value, so the selection is made once that render landed.
_INPUT_JS = """async ({ id, scope, focus }) => {
    /* linkedin-mcp:cm-editor */
    const inputs = [...document.querySelectorAll('input')].filter((el) => el.id === id);
    if (inputs.length !== 1) return { state: inputs.length ? 'ambiguous' : 'missing' };
    const input = inputs[0];
    if (scope && !input.closest(scope)) return { state: 'missing' };
    if (input.disabled || input.readOnly) return { state: 'disabled' };
    if (input.getClientRects().length === 0) return { state: 'hidden' };
    if (!focus) return { state: 'ready' };
    input.focus();
    await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
    if (document.activeElement !== input) return { state: 'unfocused' };
    input.select();
    const whole = input.selectionStart === 0 && input.selectionEnd === input.value.length;
    return { state: whole ? 'ready' : 'unselected' };
}"""
# Whether the focused input holds exactly ``text``; answers no value.
_TYPED_JS = """({ id, text }) => {
    /* linkedin-mcp:cm-editor */
    const input = document.activeElement;
    if (!input || input.id !== id) return { state: 'unfocused' };
    return { state: input.value === text ? 'typed' : 'mismatch' };
}"""
# Run after Tab: blurs the input only if Tab left it focused.
_BLUR_JS = """(id) => {
    /* linkedin-mcp:cm-editor */
    const input = document.activeElement;
    if (input && input.id === id) input.blur();
    const left = document.activeElement;
    return { blurred: !left || left.id !== id };
}"""
# The input's state for the failure notes: counts and flags, never its value.
_INPUT_STATE_JS = """(id) => {
    /* linkedin-mcp:cm-editor */
    const inputs = [...document.querySelectorAll('input')].filter((el) => el.id === id);
    const input = inputs[0];
    return {
        count: inputs.length,
        attached: Boolean(input && input.isConnected),
        focused: Boolean(input && document.activeElement === input),
        empty: !input || input.value === '',
        disabled: Boolean(input && input.disabled),
    };
}"""
_SWITCH_JS = """({ checked, click }) => {
    /* linkedin-mcp:cm-editor */
    const switches = [...document.querySelectorAll(
        '.campaign-header-status__toggle-container input[role=switch]'
    )];
    if (switches.length !== 1) return { state: 'count', count: switches.length };
    const toggle = switches[0];
    if (toggle.disabled) return { state: 'disabled' };
    if (toggle.checked !== checked) return { state: 'mismatch' };
    if (click) toggle.click();
    return { state: 'ready' };
}"""
_DIALOG_JS = """() => {
    /* linkedin-mcp:cm-editor */
    return [...document.querySelectorAll('[role=dialog]')].some(
        (el) => el.getClientRects().length > 0
    );
}"""
_OBJECTIVE_JS = """({ value, click }) => {
    /* linkedin-mcp:cm-editor */
    const radios = [...document.querySelectorAll(
        'input[type=radio][name="objective-selection"]'
    )];
    const available = radios.filter((el) => !el.disabled).map((el) => el.value);
    const matches = radios.filter((el) => el.value === value);
    if (matches.length !== 1 || matches[0].disabled) {
        return { state: 'unavailable', available };
    }
    if (click) matches[0].click();
    return { state: 'ready', available };
}"""
# The one visible date input of ``name``; ``open`` clicks it, which shows its
# pre-rendered calendar on the value's month.
_DATE_INPUT_JS = """({ name, open }) => {
    /* linkedin-mcp:cm-calendar */
    const inputs = [...document.querySelectorAll('input[data-datepicker-input]')]
        .filter((el) => el.name === name && el.getClientRects().length > 0);
    if (inputs.length !== 1) return { state: inputs.length ? 'ambiguous' : 'missing' };
    const input = inputs[0];
    if (input.disabled || input.readOnly) return { state: 'disabled' };
    if (open) {
        input.focus();
        input.click();
    }
    return { state: 'ready' };
}"""
# Every calendar is pre-rendered, hidden; only the open one has a box. A day
# of a neighbouring month carries ``<d>-shadow`` and is never counted or used.
_CALENDAR_JS = """async ({ mode, days, day, forward }) => {
    /* linkedin-mcp:cm-calendar */
    const roots = [...document.querySelectorAll('[data-calendar-root]')].filter((el) => {
        const box = el.getBoundingClientRect();
        return box.width > 0 && box.height > 0;
    });
    if (mode === 'closed') return { ok: roots.length === 0 };
    if (roots.length !== 1) return { ok: false, reason: 'roots', count: roots.length };
    const root = roots[0];
    if (mode === 'step') {
        const buttons = root.querySelectorAll(
            forward ? '[data-calendar-next-month]' : '[data-calendar-prev-month]'
        );
        if (buttons.length !== 1 || buttons[0].disabled) return { ok: false, reason: 'step' };
        buttons[0].click();
        await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
        return { ok: true };
    }
    const real = [...root.querySelectorAll('[data-calendar-day][data-daynum]')].filter(
        (el) => !el.getAttribute('data-daynum').endsWith('-shadow')
    );
    if (real.length !== days) return { ok: false, reason: 'days', count: real.length };
    if (mode === 'check') return { ok: true };
    const targets = real.filter((el) => el.getAttribute('data-daynum') === String(day));
    if (targets.length !== 1 || targets[0].disabled) return { ok: false, reason: 'day' };
    targets[0].click();
    return { ok: true };
}"""


def _today() -> date:
    return datetime.now(UTC).date()


def _days_in(day: date) -> int:
    return calendar.monthrange(day.year, day.month)[1]


def _state(result: Any) -> str | None:
    return result.get("state") if isinstance(result, dict) else None


def _ok(result: Any) -> bool:
    return isinstance(result, dict) and result.get("ok") is True


async def _poll(page: Any, script: str, arg: Mapping[str, Any]) -> Any:
    """Run a check-then-act program until it succeeds or the attempts run out.

    The program acts only on the attempt whose checks pass, so a retry while
    the app is still rendering never clicks twice.
    """
    result: Any = None
    for attempt in range(_POLL_ATTEMPTS):
        result = await page.evaluate(script, dict(arg))
        # The calendar answers ``ok``, the objective tiles a ``state``.
        if _ok(result) or _state(result) == "ready":
            return result
        if attempt + 1 < _POLL_ATTEMPTS:
            await asyncio.sleep(_POLL_INTERVAL)
    return result


async def set_text(page: Any, input_id: str, scope: str | None, text: str) -> bool:
    """Replace an input's value with ``text`` in one insertion, then Tab away.

    Focus selects the whole value, so the app sees one input event carrying
    the final text and never an intermediate value it could save. The value is
    checked before the input loses focus: the app commits a budget only on the
    ``change`` that leaving the input fires, so an input holding anything but
    ``text`` is left focused and uncommitted, and the caller leaves the page.
    """
    focused = await page.evaluate(
        _INPUT_JS, {"id": input_id, "scope": scope, "focus": True}
    )
    if _state(focused) != "ready":
        logger.debug("Input %s not focused: %s", input_id, focused)
        return False
    await page.keyboard.insert_text(text)
    typed = await page.evaluate(_TYPED_JS, {"id": input_id, "text": text})
    if _state(typed) != "typed":
        logger.debug("Input %s not typed: %s", input_id, typed)
        return False
    await page.keyboard.press("Tab")
    blurred = await page.evaluate(_BLUR_JS, input_id)
    return isinstance(blurred, dict) and blurred.get("blurred") is True


async def check_calendar(page: Any, name: str, month: date) -> bool:
    """Open the date input's calendar, check it shows ``month``, close it."""
    opened = await page.evaluate(_DATE_INPUT_JS, {"name": name, "open": True})
    if _state(opened) != "ready":
        return False
    shown = await _poll(
        page, _CALENDAR_JS, {"mode": "check", "days": _days_in(month), "day": 0}
    )
    await page.keyboard.press("Escape")
    closed = await page.evaluate(_CALENDAR_JS, {"mode": "closed"})
    return _ok(shown) and _ok(closed)


async def pick_date(page: Any, name: str, origin: date, target: date) -> bool:
    """Open the calendar on ``origin``'s month, move to ``target``'s, click its day.

    Each month step and the day click check the open calendar first: one
    visible calendar, a day count equal to the month's, one enabled day.
    """
    opened = await page.evaluate(_DATE_INPUT_JS, {"name": name, "open": True})
    if _state(opened) != "ready":
        return False
    shown = await _poll(
        page, _CALENDAR_JS, {"mode": "check", "days": _days_in(origin), "day": 0}
    )
    if not _ok(shown):
        await page.keyboard.press("Escape")
        return False
    months = (target.year - origin.year) * 12 + target.month - origin.month
    for _ in range(abs(months)):
        stepped = await page.evaluate(
            _CALENDAR_JS, {"mode": "step", "forward": months > 0, "days": 0, "day": 0}
        )
        if not _ok(stepped):
            await page.keyboard.press("Escape")
            return False
    picked = await _poll(
        page,
        _CALENDAR_JS,
        {"mode": "pick", "days": _days_in(target), "day": target.day},
    )
    if not _ok(picked):
        await page.keyboard.press("Escape")
        return False
    if not _ok(await page.evaluate(_CALENDAR_JS, {"mode": "closed"})):
        await page.keyboard.press("Escape")
    return True


async def toggle_status(page: Any, *, checked: bool, click: bool) -> str | None:
    """The header switch's state; ``click`` toggles it only when it is ``checked``."""
    return _state(await page.evaluate(_SWITCH_JS, {"checked": checked, "click": click}))


def _amount(money: Any) -> Decimal | None:
    amount = money.get("amount") if isinstance(money, Mapping) else None
    if isinstance(amount, bool) or not isinstance(amount, (str, int, float)):
        return None
    try:
        return Decimal(str(amount))
    except InvalidOperation:
        return None


def _schedule_day(record: Mapping[str, Any], key: str) -> date | None:
    schedule = record.get("runSchedule")
    value = schedule.get(key) if isinstance(schedule, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return ms_to_date(value)


def _campaign_target(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": "campaign",
        "uiLabel": "Ad set",
        "id": record.get("id"),
        "accountId": record.get("accountId"),
        "name": record.get("name"),
        "status": record.get("status"),
    }


def _edit_values(record: Mapping[str, Any], plan: CampaignEditPlan) -> dict[str, Any]:
    """The planned fields as the record now holds them."""
    values: dict[str, Any] = {}
    for step in plan.order:
        if step in ("daily", "lifetime"):
            values[_FIELDS[step]] = record.get(
                "dailyBudget" if step == "daily" else "totalBudget"
            )
        else:
            day = _schedule_day(record, step)
            values[_FIELDS[step]] = day.isoformat() if day is not None else None
    return values


def _planned_edit(record: Mapping[str, Any], plan: CampaignEditPlan) -> dict[str, Any]:
    planned: dict[str, Any] = {}
    for step in plan.order:
        if step in ("daily", "lifetime"):
            field = "dailyBudget" if step == "daily" else "totalBudget"
            money = record.get(field)
            currency = money.get("currencyCode") if isinstance(money, Mapping) else None
            amount = plan.daily if step == "daily" else plan.lifetime
            assert amount is not None
            planned[_FIELDS[step]] = {
                "amount": format_budget(amount),
                "currencyCode": currency,
            }
        else:
            day = plan.start if step == "start" else plan.end
            assert day is not None
            planned[_FIELDS[step]] = day.isoformat()
    return planned


def _edit_matches(record: Mapping[str, Any], plan: CampaignEditPlan) -> bool:
    checks = (
        (plan.daily, lambda: _amount(record.get("dailyBudget"))),
        (plan.lifetime, lambda: _amount(record.get("totalBudget"))),
        (plan.start, lambda: _schedule_day(record, "start")),
        (plan.end, lambda: _schedule_day(record, "end")),
    )
    return all(want is None or got() == want for want, got in checks)


def _edit_admission(
    record: Mapping[str, Any], plan: CampaignEditPlan
) -> tuple[dict[str, Any], set[str]]:
    allowed: dict[str, Any] = {}
    if plan.daily is not None:
        allowed["dailyBudget"] = plan.daily
    if plan.lifetime is not None:
        allowed["totalBudget"] = plan.lifetime
    if plan.start is not None or plan.end is not None:
        allowed["runSchedule"] = {
            "start": plan.start or _schedule_day(record, "start"),
            "end": plan.end or _schedule_day(record, "end"),
        }
    # Two dates are two autosaves: the state between them is on the path.
    path = {"runSchedule"} if plan.start is not None and plan.end is not None else set()
    return allowed, path


def _is_patch(campaign_id: str) -> Callable[[Any], bool]:
    def matches(write: Any) -> bool:
        return (
            write.call.resource == "campaignManagerCampaigns"
            and write.call.entity_id == campaign_id
            and (write.restli_method or "").lower() == "partial_update"
        )

    return matches


def _is_create(write: Any) -> bool:
    return (
        write.call.resource == "campaignManagerCampaigns"
        and write.call.entity_id is None
        and (write.restli_method or "").lower() == "create"
    )


def _stopped(outcome: CmWriteResponse | str, window: WriteWindow) -> Refusal:
    """Why a save the plan waited for did not succeed, in the common vocabulary."""
    if outcome == "refused":
        declaration = declaration_stop(window.refused)
        if declaration is not None:
            return declaration
        return (
            "change_blocked",
            "Campaign Manager tried to save a value that is not the planned one, "
            "so it was stopped before it left the browser.",
        )
    if isinstance(outcome, CmWriteResponse):
        if 400 <= outcome.status < 500:
            return (
                "save_failed",
                f"LinkedIn refused the change (HTTP {outcome.status}); nothing "
                "was changed.",
            )
        return (
            "save_unverified",
            f"LinkedIn answered the save with HTTP {outcome.status}, so whether "
            "it was kept is unknown. Check the ad set before retrying.",
        )
    if window.admitted:
        return (
            "save_unverified",
            "The change was sent but LinkedIn's answer did not arrive. Check the "
            "ad set before retrying.",
        )
    if outcome == "declaration":
        return ("declaration_required", DECLARATION_REQUIRED)
    if outcome == "unanswered":
        return ("control_unavailable", UNANSWERED)
    if outcome == "dialog":
        return (
            "control_unavailable",
            "Campaign Manager asked a question in a dialog before saving that "
            "this tool does not answer, so the dialog was closed and nothing "
            "was sent. Make this change in Campaign Manager.",
        )
    if window.refused:
        return (
            "control_unavailable",
            "Campaign Manager did not send the save of this change; the other "
            "changes it tried were stopped (see refused), so nothing was sent.",
        )
    return (
        "control_unavailable",
        "Campaign Manager did not send a save for the change, so nothing was sent.",
    )


class CampaignManagerEditor:
    """Budget, schedule and status edits of an ad set, and new draft ad sets."""

    def __init__(self, session: ScrapingSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator
        self._flow = CampaignWriteFlow(session, navigator)

    async def _await_save(
        self,
        capture: CampaignManagerCapture,
        window: WriteWindow,
        is_save: Callable[[Any], bool],
        *,
        writes: int,
        refused: int,
        dialog_grace: float | None = None,
        asks_political: bool = False,
        political_ad: bool | None = None,
    ) -> CmWriteResponse | str:
        """LinkedIn's answer to the planned save, or why it never came.

        ``"refused"``: the guard aborted the app's save of this entity as off
        plan. ``"dialog"``: a dialog stayed open ``dialog_grace`` seconds
        without a save; it was closed with Escape, never by its buttons.
        ``"timeout"``: nothing came. With ``asks_political`` the political-ad
        question is answered with ``political_ad``, the caller's declaration;
        without one it is closed with Escape (``"declaration"``), and one
        that is not the known dialog is closed unanswered (``"unanswered"``).
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _SAVE_TIMEOUT
        dialog_since: float | None = None
        page = self._session.page
        while True:
            if any(is_save(request) for request, _ in window.refused[refused:]):
                return "refused"
            answer = await self._flow.wait_saved(
                capture, is_save, writes, timeout=_POLL_INTERVAL
            )
            if answer is not None:
                return answer
            if asks_political:
                question = await political_dialog(page, political_ad)
                if question == "answered":
                    asks_political = False
                    dialog_since = None
                    continue
                if question not in ("absent", None):
                    await page.keyboard.press("Escape")
                    return "declaration" if question == "open" else "unanswered"
            now = loop.time()
            if dialog_grace is not None and await self._dialog_open(page):
                dialog_since = now if dialog_since is None else dialog_since
                if now - dialog_since >= dialog_grace:
                    await page.keyboard.press("Escape")
                    return "dialog"
            if now >= deadline:
                return "timeout"

    @staticmethod
    async def _dialog_open(page: Any) -> bool:
        """Whether a dialog shows; a page navigating away (the save went) has none."""
        try:
            return await page.evaluate(_DIALOG_JS) is True
        except PlaywrightError:
            logger.debug("Dialog check interrupted", exc_info=True)
            return False

    async def _reread(
        self, capture: CampaignManagerCapture, account: str, campaign: str, suffix: str
    ) -> dict[str, Any] | None:
        try:
            record = await self._flow.read_campaign(
                capture, account, campaign, suffix=suffix
            )
        except LinkedInScraperException:
            logger.debug("Campaign Manager re-read failed", exc_info=True)
            return None
        return record if isinstance(record, dict) else None

    # update_campaign ---------------------------------------------------------

    async def update_campaign(
        self,
        account_id: str,
        campaign_id: str,
        *,
        daily_budget: str | None = None,
        lifetime_budget: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        confirm_send: bool,
        political_ad: bool | None = None,
    ) -> dict[str, Any]:
        """Change an ad set's budget amount or schedule dates.

        The budget and schedule types never switch. A draft in the editor
        that autosaves saves each field as it is edited; any other ad set,
        a draft in the classic editor included, saves once, by Save and exit.
        ``political_ad`` is the user's own declaration whether the ad set's
        ads are political, for the question Save and exit asks on EU
        targeting; None never answers it.
        """
        account = normalize_ad_account_id(account_id)
        campaign = normalize_campaign_id(campaign_id)
        url = campaign_manager_url(account, f"/campaigns/{campaign}/details")
        today = _today()
        invalid = refuse_invalid_campaign_edit(
            daily_budget=daily_budget,
            lifetime_budget=lifetime_budget,
            start_date=start_date,
            end_date=end_date,
            today=today,
        )
        if invalid is not None:
            return campaign_action_result(
                _UPDATE, url, "invalid_change", invalid, retry_safe=True
            )
        page = self._session.page
        async with self._flow.capturing() as capture:
            record = await self._flow.read_campaign(capture, account, campaign)
            if isinstance(record, tuple):
                return campaign_action_result(_UPDATE, url, *record, retry_safe=True)
            target = _campaign_target(record)
            plan = plan_campaign_edit(
                record,
                daily=parse_budget(daily_budget) if daily_budget is not None else None,
                lifetime=parse_budget(lifetime_budget)
                if lifetime_budget is not None
                else None,
                start=start_date,
                end=end_date,
                today=today,
            )
            if not isinstance(plan, CampaignEditPlan):
                status, message = plan
                return campaign_action_result(
                    _UPDATE, url, status, message, retry_safe=True, target=target
                )
            planned = _planned_edit(record, plan)
            if political_ad is not None:
                planned[POLITICAL_INTENT] = declared_intent(political_ad)
            problem = await self._check_edit_controls(page, record, plan)
            if problem is not None:
                return campaign_action_result(
                    _UPDATE,
                    url,
                    *problem,
                    retry_safe=True,
                    target=target,
                    planned=planned,
                )
            if not confirm_send:
                question = political_question(
                    targeting_keys(record.get("targetingCriteria"))
                )
                return campaign_action_result(
                    _UPDATE,
                    url,
                    "confirmation_required",
                    "Nothing was changed. Call again with confirm_send true to "
                    "apply the planned change."
                    + (question_sentence(question) if political_ad is None else ""),
                    retry_safe=True,
                    target=target,
                    planned={**planned, "politicalAdQuestion": question},
                )
            return await self._apply_edit(
                capture,
                url,
                account,
                campaign,
                record,
                plan,
                target,
                planned,
                political_ad,
            )

    async def _check_edit_controls(
        self, page: Any, record: Mapping[str, Any], plan: CampaignEditPlan
    ) -> Refusal | None:
        """Every control the plan needs is there, unique and enabled; no edit."""
        for step in plan.order:
            await self._flow.wait_for_anchor(page, _STEP_ANCHORS[step])
            if step in _BUDGET_INPUTS:
                state = _state(
                    await page.evaluate(
                        _INPUT_JS,
                        {
                            "id": _BUDGET_INPUTS[step],
                            "scope": _BUDGET_SCOPE,
                            "focus": False,
                        },
                    )
                )
                ready = state == "ready"
            else:
                state = _state(
                    await page.evaluate(_DATE_INPUT_JS, {"name": step, "open": False})
                )
                origin = _schedule_day(record, step)
                ready = (
                    state == "ready"
                    and origin is not None
                    and await check_calendar(page, step, origin)
                )
            if state == "disabled":
                return (
                    "not_editable",
                    f"Campaign Manager does not allow changing the {_FIELDS[step]} "
                    "of this ad set now. Nothing was changed.",
                )
            if not ready:
                return (
                    "control_unavailable",
                    f"The {_FIELDS[step]} control was not found in the layout this "
                    "tool knows. Nothing was changed.",
                )
        return None

    async def _apply_step(
        self, page: Any, step: str, record: Mapping[str, Any], plan: CampaignEditPlan
    ) -> bool:
        if step in _BUDGET_INPUTS:
            amount = plan.daily if step == "daily" else plan.lifetime
            assert amount is not None
            return await set_text(
                page, _BUDGET_INPUTS[step], _BUDGET_SCOPE, format_budget(amount)
            )
        origin = _schedule_day(record, step)
        target = plan.start if step == "start" else plan.end
        assert origin is not None and target is not None
        return await pick_date(page, step, origin, target)

    async def _apply_edit(
        self,
        capture: CampaignManagerCapture,
        url: str,
        account: str,
        campaign: str,
        record: Mapping[str, Any],
        plan: CampaignEditPlan,
        target: dict[str, Any],
        planned: dict[str, Any],
        political_ad: bool | None,
    ) -> dict[str, Any]:
        page = self._session.page
        draft = record.get("status") == "DRAFT"
        allowed, path = _edit_admission(record, plan)
        is_save = _is_patch(campaign)
        # A draft autosaves each field, except in the classic editor, which
        # saves a draft like a launched ad set: once, by Save and exit.
        newer_editor = not await self._flow.saves_by_footer(page)
        autosaves = draft and newer_editor

        def admit(request: CmWriteRequest) -> str | None:
            sent = sent_intent(request.body)
            refusal = admit_campaign_patch(
                request,
                campaign_id=campaign,
                record=record,
                allowed=allowed
                if sent is None
                else {**allowed, POLITICAL_INTENT: sent},
                path_fields=path,
            )
            if refusal is not None or sent is None:
                return refusal
            return refuse_intent(
                sent,
                record.get(POLITICAL_INTENT),
                political_ad,
                newer_editor=newer_editor,
            )

        saves = "autosave" if autosaves else "save_and_exit"
        saved = 0
        stop: Refusal | None = None
        async with self._flow.apply(page, admit) as window:
            for step in plan.order:
                writes, refused = capture.write_count, len(window.refused)
                if not await self._apply_step(page, step, record, plan):
                    stop = (
                        "control_unavailable",
                        f"The {_FIELDS[step]} control did not behave as expected, "
                        "so the edit stopped there and nothing of it was sent.",
                    )
                    await self._note_stop(page, window, plan, saves, step, "control")
                    break
                if autosaves:
                    outcome = await self._await_save(
                        capture, window, is_save, writes=writes, refused=refused
                    )
                    if not (isinstance(outcome, CmWriteResponse) and outcome.ok):
                        stop = _stopped(outcome, window)
                        await self._note_stop(page, window, plan, saves, step, outcome)
                        break
                    saved += 1
            if stop is None and not autosaves:
                writes, refused = capture.write_count, len(window.refused)
                if await self._flow.save_and_exit(page, draft=draft):
                    outcome = await self._await_save(
                        capture,
                        window,
                        is_save,
                        writes=writes,
                        refused=refused,
                        dialog_grace=_DIALOG_GRACE,
                        asks_political=True,
                        political_ad=political_ad,
                    )
                    if isinstance(outcome, CmWriteResponse) and outcome.ok:
                        saved += 1
                    else:
                        stop = _stopped(outcome, window)
                        await self._note_stop(
                            page, window, plan, saves, "footer", outcome
                        )
                else:
                    stop = (
                        "control_unavailable",
                        f"{footer_disabled_reason(record)} Nothing was saved."
                        if self._flow.footer_disabled
                        else "Save and exit was not found in the layout this tool "
                        "knows, so nothing was saved.",
                    )
                    await self._note_stop(
                        page, window, plan, saves, "footer", "control"
                    )
        if stop is not None:
            await self._flow.leave(page, url)
        if stop is not None and not saved:
            return campaign_action_result(
                _UPDATE,
                url,
                *stop,
                retry_safe=stop[0] == "save_failed" or retry_safe_from(window),
                target=target,
                planned=planned,
                refused=window.refused,
            )
        after = await self._reread(capture, account, campaign, "/details")
        applied = _edit_values(after, plan) if after is not None else None
        if applied is not None and after is not None and political_ad is not None:
            applied[POLITICAL_INTENT] = after.get(POLITICAL_INTENT)
        note = declaration_note(political_ad, window.admitted, after)
        if stop is not None:
            return campaign_action_result(
                _UPDATE,
                url,
                "partially_applied",
                f"Only part of the change was saved: {stop[1]}{note}",
                retry_safe=False,
                target=target,
                planned=planned,
                applied=applied,
                refused=window.refused,
            )
        if after is None or not _edit_matches(after, plan):
            return campaign_action_result(
                _UPDATE,
                url,
                "save_unverified",
                "LinkedIn accepted the save, but the ad set read back afterwards "
                "does not show the planned values. Check it before retrying." + note,
                retry_safe=False,
                target=target,
                planned=planned,
                applied=applied,
                refused=window.refused,
            )
        return campaign_action_result(
            _UPDATE,
            url,
            "updated",
            "The ad set was updated." + note,
            retry_safe=False,
            target=_campaign_target(after),
            planned=planned,
            applied=applied,
            refused=window.refused,
        )

    async def _note_stop(
        self,
        page: Any,
        window: WriteWindow,
        plan: CampaignEditPlan,
        saves: str,
        step: str,
        outcome: CmWriteResponse | str,
    ) -> None:
        """Record a stopped change's window and budget inputs for the notes.

        A diagnostic only: an input a navigating page no longer answers for is
        left out, never raised, so the stop is reported as it happened.
        """
        inputs: dict[str, Any] = {}
        for name in plan.order:
            if name not in _BUDGET_INPUTS:
                continue
            try:
                inputs[_FIELDS[name]] = await page.evaluate(
                    _INPUT_STATE_JS, _BUDGET_INPUTS[name]
                )
            except PlaywrightError:
                logger.debug("Input state not recorded", exc_info=True)
        record_write_window(
            window,
            saves=saves,
            step=_FIELDS.get(step, step),
            outcome=(
                f"http {outcome.status}"
                if isinstance(outcome, CmWriteResponse)
                else outcome
            ),
            inputs=inputs,
        )

    # set_campaign_status -----------------------------------------------------

    async def set_campaign_status(
        self,
        account_id: str,
        campaign_id: str,
        *,
        status: str,
        confirm_send: bool,
    ) -> dict[str, Any]:
        """Pause an active ad set or resume a paused one; never launch a draft.

        The decision comes from the record's status, never from the switch,
        which is also enabled on ad sets that cannot change.
        """
        account = normalize_ad_account_id(account_id)
        campaign = normalize_campaign_id(campaign_id)
        url = campaign_manager_url(account, f"/campaigns/{campaign}/summary")
        if status not in ("ACTIVE", "PAUSED"):
            return campaign_action_result(
                _STATUS,
                url,
                "invalid_change",
                "status must be PAUSED or ACTIVE.",
                retry_safe=True,
            )
        page = self._session.page
        async with self._flow.capturing() as capture:
            record = await self._flow.read_campaign(
                capture, account, campaign, suffix="/summary"
            )
            if isinstance(record, tuple):
                return campaign_action_result(_STATUS, url, *record, retry_safe=True)
            target = _campaign_target(record)
            current = record.get("status")
            planned = {"status": {"from": current, "to": status}}
            if current not in ("ACTIVE", "PAUSED"):
                return campaign_action_result(
                    _STATUS,
                    url,
                    "not_editable",
                    f"This ad set is {current or 'in an unknown state'}; only an "
                    "active or paused ad set can be paused or resumed. Launching "
                    "a draft is not supported.",
                    retry_safe=True,
                    target=target,
                )
            if current == status:
                return campaign_action_result(
                    _STATUS,
                    url,
                    "no_change",
                    f"The ad set is already {status}.",
                    retry_safe=True,
                    target=target,
                )
            checked = current == "ACTIVE"
            await self._flow.wait_for_anchor(page, ANCHORS["status_switch"])
            state = await toggle_status(page, checked=checked, click=False)
            if state != "ready":
                return campaign_action_result(
                    _STATUS,
                    url,
                    "not_editable" if state == "disabled" else "control_unavailable",
                    "The status switch is not available in the layout this tool "
                    "knows. Nothing was changed.",
                    retry_safe=True,
                    target=target,
                    planned=planned,
                )
            if not confirm_send:
                return campaign_action_result(
                    _STATUS,
                    url,
                    "confirmation_required",
                    "Nothing was changed. Call again with confirm_send true to "
                    f"set the ad set to {status}.",
                    retry_safe=True,
                    target=target,
                    planned=planned,
                )

            def admit(request: CmWriteRequest) -> str | None:
                return admit_status_patch(
                    request,
                    campaign_id=campaign,
                    from_status=str(current),
                    to_status=status,
                )

            is_save = _is_patch(campaign)
            async with self._flow.apply(page, admit) as window:
                writes, refused = capture.write_count, len(window.refused)
                if await toggle_status(page, checked=checked, click=True) == "ready":
                    outcome: CmWriteResponse | str = await self._await_save(
                        capture,
                        window,
                        is_save,
                        writes=writes,
                        refused=refused,
                        dialog_grace=_DIALOG_GRACE if status == "ACTIVE" else None,
                    )
                else:
                    outcome = "timeout"
            if not (isinstance(outcome, CmWriteResponse) and outcome.ok):
                await self._flow.leave(page, url)
                if outcome == "dialog":
                    return campaign_action_result(
                        _STATUS,
                        url,
                        "reactivation_needs_changes",
                        "Campaign Manager asked for changes before this ad set "
                        "can resume. Its dialog was closed and nothing was sent; "
                        "resume it in Campaign Manager.",
                        retry_safe=retry_safe_from(window),
                        target=target,
                        planned=planned,
                        refused=window.refused,
                    )
                stop = _stopped(outcome, window)
                return campaign_action_result(
                    _STATUS,
                    url,
                    *stop,
                    retry_safe=stop[0] == "save_failed" or retry_safe_from(window),
                    target=target,
                    planned=planned,
                    refused=window.refused,
                )
            after = await self._reread(capture, account, campaign, "/summary")
        applied = {"status": after.get("status")} if after is not None else None
        if after is None or after.get("status") != status:
            return campaign_action_result(
                _STATUS,
                url,
                "save_unverified",
                "LinkedIn accepted the change, but the ad set read back afterwards "
                f"is not {status}. Check it before retrying.",
                retry_safe=False,
                target=target,
                planned=planned,
                applied=applied,
            )
        done = "paused" if status == "PAUSED" else "resumed"
        return campaign_action_result(
            _STATUS,
            url,
            done,
            f"The ad set was {done}.",
            retry_safe=False,
            target=_campaign_target(after),
            planned=planned,
            applied=applied,
            refused=window.refused,
        )

    # create_campaign_draft ---------------------------------------------------

    async def create_campaign_draft(
        self,
        account_id: str,
        campaign_group_id: str,
        *,
        objective: str,
        name: str,
        confirm_send: bool,
    ) -> dict[str, Any]:
        """Create a draft ad set with ``objective`` and ``name`` in an existing group.

        The draft is created in the chosen campaign group; this never creates
        a group, so an ad account without that group is refused
        (``no_campaign_group``). The draft is never launched.
        """
        account = normalize_ad_account_id(account_id)
        group = normalize_campaign_group_id(campaign_group_id)
        url = campaign_manager_url(
            account, f"/campaigns/new/setup?campaignGroupId={group}"
        )
        invalid = refuse_invalid_draft(objective, name)
        if invalid is not None:
            return campaign_action_result(
                _CREATE, url, "invalid_change", invalid, retry_safe=True
            )
        planned: dict[str, Any] = {
            "campaignGroupId": int(group),
            "objectiveType": objective,
            "name": name,
        }
        page = self._session.page
        async with self._flow.capturing() as capture:
            target, problem = await self._open_setup(capture, url, account, group)
            if problem is not None:
                return campaign_action_result(
                    _CREATE,
                    url,
                    *problem,
                    retry_safe=True,
                    target=target,
                    planned=planned,
                )
            await self._flow.wait_for_anchor(page, ANCHORS["objective"])
            tiles = await _poll(
                page, _OBJECTIVE_JS, {"value": objective, "click": False}
            )
            if _state(tiles) != "ready":
                available = tiles.get("available") if isinstance(tiles, dict) else []
                return campaign_action_result(
                    _CREATE,
                    url,
                    "objective_unavailable",
                    f"Campaign Manager does not offer the objective {objective} "
                    "here. Pass one of planned.available.",
                    retry_safe=True,
                    target=target,
                    planned={**planned, "available": available or []},
                )
            if not confirm_send:
                return campaign_action_result(
                    _CREATE,
                    url,
                    "confirmation_required",
                    "Nothing was created. Call again with confirm_send true to "
                    "create the draft ad set; it is never launched.",
                    retry_safe=True,
                    target=target,
                    planned=planned,
                )
            return await self._create(
                capture, url, account, group, objective, name, target, planned
            )

    async def _open_setup(
        self, capture: CampaignManagerCapture, url: str, account: str, group: str
    ) -> tuple[dict[str, Any] | None, Refusal | None]:
        """Load the setup page with every app write aborted and recorded.

        The app may create a campaign group or a billing configuration on its
        own here; recording the aborted requests is what shows a missing group.
        """
        page = self._session.page

        def is_group(response: CmResponse) -> bool:
            if response.call.resource == "campaignManagerCampaignGroups":
                record = envelope_record(response.payload)
                return response.call.entity_id == group and record is not None
            if response.call.resource == "campaignManagerReportingCampaignGroups":
                return response.ok and envelope_elements(response.payload) is not None
            return False

        async with explicit_campaign_write(
            page, admit=lambda _request: "read phase"
        ) as probe:
            after = len(capture.responses)
            await self._navigator._navigate_to_page(url)
            await self._session.check_rate_limit()
            await self._session.dismiss_modal()
            try:
                await capture.wait_for(
                    is_group,
                    after=after,
                    timeout=_READY_TIMEOUT,
                    wanted=(
                        "campaignManagerCampaignGroups",
                        "campaignManagerReportingCampaignGroups",
                    ),
                )
            except LinkedInScraperException:
                logger.debug("No campaign group response on setup")
            await capture.settle()
        record = _group_record(capture.matching(is_group, after), account, group)
        group_writes = any(
            request.call.resource == "campaignManagerCampaignGroups"
            for request, _ in probe.refused
        )
        if group_writes or record is None:
            return None, (
                "no_campaign_group",
                "That campaign group was not found in this ad account. This tool "
                "never creates a campaign group: pass the id of an existing one "
                "as list_campaigns returned it. Nothing was created.",
            )
        target = {
            "kind": "campaign_group",
            "uiLabel": "Campaign",
            "id": record.get("id"),
            "accountId": record.get("accountId"),
            "name": record.get("name"),
            "status": record.get("status"),
        }
        if _SETUP_PATH not in urlparse(page.url).path:
            return target, (
                "control_unavailable",
                "Campaign Manager did not open its ad set setup page. Nothing was "
                "created.",
            )
        return target, None

    async def _create(
        self,
        capture: CampaignManagerCapture,
        url: str,
        account: str,
        group: str,
        objective: str,
        name: str,
        target: dict[str, Any] | None,
        planned: dict[str, Any],
    ) -> dict[str, Any]:
        page = self._session.page
        create = admit_campaign_create(
            account_id=account, group_id=group, objective=objective
        )
        draft: dict[str, Any] = {}

        def admit(request: CmWriteRequest) -> str | None:
            if request.call.entity_id is None:
                return create(request)
            if "record" not in draft:
                return "write before the new draft was read"
            return admit_campaign_patch(
                request,
                campaign_id=draft["id"],
                record=draft["record"],
                allowed={"name": name},
            )

        prefix = urlparse(campaign_manager_url(account, "/campaigns/")).path

        def own_draft(path: str) -> bool:
            head = path.removeprefix(prefix).split("/", 1)[0]
            return path.startswith(prefix) and _NUMERIC.fullmatch(head) is not None

        stop: Refusal | None = None
        async with self._flow.apply(page, admit, expected_path=own_draft) as window:
            writes, refused = capture.write_count, len(window.refused)
            responses = len(capture.responses)
            clicked = await page.evaluate(
                _OBJECTIVE_JS, {"value": objective, "click": True}
            )
            outcome: CmWriteResponse | str = "timeout"
            if _state(clicked) == "ready":
                outcome = await self._await_save(
                    capture, window, _is_create, writes=writes, refused=refused
                )
            if not (isinstance(outcome, CmWriteResponse) and outcome.ok):
                stop = _stopped(outcome, window)
            elif outcome.created_id is None or not _NUMERIC.fullmatch(
                outcome.created_id
            ):
                stop = (
                    "save_unverified",
                    "LinkedIn accepted the draft but did not say its id. Check "
                    "the campaign group before retrying.",
                )
            else:
                draft["id"] = outcome.created_id
                stop = await self._rename(
                    capture, window, account, draft, name, responses
                )
        created_id = draft.get("id")
        if stop is not None:
            await self._flow.leave(page, campaign_manager_url(account, "/campaigns"))
        if stop is not None and created_id is None:
            return campaign_action_result(
                _CREATE,
                url,
                *stop,
                retry_safe=stop[0] == "save_failed" or retry_safe_from(window),
                target=target,
                planned=planned,
                refused=window.refused,
            )
        if stop is not None:
            return campaign_action_result(
                _CREATE,
                url,
                "partially_applied",
                f"The draft ad set was created but not renamed: {stop[1]}",
                retry_safe=False,
                target=target,
                planned=planned,
                created_id=created_id,
                refused=window.refused,
            )
        after = await self._reread(capture, account, str(created_id), "/details")
        applied = (
            {
                key: after.get(key)
                for key in ("status", "name", "campaignGroupId", "objectiveType")
            }
            if after is not None
            else None
        )
        expected = {
            "status": "DRAFT",
            "name": name,
            "campaignGroupId": int(group),
            "objectiveType": objective,
        }
        if applied != expected:
            return campaign_action_result(
                _CREATE,
                url,
                "save_unverified",
                "LinkedIn created the draft, but the ad set read back afterwards "
                "does not show the planned values. Check it before retrying.",
                retry_safe=False,
                target=target,
                planned=planned,
                applied=applied,
                created_id=created_id,
                refused=window.refused,
            )
        return campaign_action_result(
            _CREATE,
            url,
            "created",
            "The draft ad set was created. It is not launched.",
            retry_safe=False,
            target=target,
            planned=planned,
            applied=applied,
            created_id=created_id,
            refused=window.refused,
        )

    async def _rename(
        self,
        capture: CampaignManagerCapture,
        window: WriteWindow,
        account: str,
        draft: dict[str, Any],
        name: str,
        responses: int,
    ) -> Refusal | None:
        """Give the new draft ``name`` on its details page, where the input lives."""
        page = self._session.page
        campaign = draft["id"]
        details = campaign_manager_url(account, f"/campaigns/{campaign}/details")
        if urlparse(page.url).path != urlparse(details).path:
            await self._flow.leave(page, details)

        def is_draft(response: CmResponse) -> bool:
            record = envelope_record(response.payload)
            return (
                response.ok
                and response.call.resource == "campaignManagerCampaigns"
                and response.call.entity_id == campaign
                and record is not None
                and same_id(record.get("id"), campaign)
            )

        try:
            found = await capture.wait_for(
                is_draft,
                after=responses,
                timeout=_READY_TIMEOUT,
                wanted=("campaignManagerCampaigns",),
            )
        except LinkedInScraperException:
            return (
                "control_unavailable",
                "Campaign Manager did not load the new draft.",
            )
        draft["record"] = envelope_record(found.payload) or {}
        if draft["record"].get("name") == name:
            return None
        await self._flow.wait_for_anchor(page, ANCHORS["name_input"])
        writes, refused = capture.write_count, len(window.refused)
        if not await set_text(page, _NAME_INPUT, None, name):
            return (
                "control_unavailable",
                "The name input was not found in the layout this tool knows.",
            )
        outcome = await self._await_save(
            capture, window, _is_patch(campaign), writes=writes, refused=refused
        )
        if isinstance(outcome, CmWriteResponse) and outcome.ok:
            return None
        return _stopped(outcome, window)


def _group_record(
    responses: list[CmResponse], account: str, group: str
) -> dict[str, Any] | None:
    """The group, from its own record or the account's reporting list."""
    for response in responses:
        if not response.ok:
            continue
        if response.call.resource == "campaignManagerCampaignGroups":
            record = envelope_record(response.payload)
            candidates = [record] if record is not None else []
        else:
            candidates = envelope_elements(response.payload) or []
        for candidate in candidates:
            if same_id(candidate.get("id"), group) and same_id(
                candidate.get("accountId"), account
            ):
                return candidate
    return None
