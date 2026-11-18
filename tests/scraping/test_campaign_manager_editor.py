"""Tests for the Campaign Manager editor (synthetic data only).

A scripted app stands in for Campaign Manager: its DOM programs answer from
state, and every save it makes goes through the real guard route, so what the
admission refuses never reaches the capture.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.scraping import campaign_manager_editor as editor_module
from linkedin_mcp_server.scraping import campaign_manager_political as political_module
from linkedin_mcp_server.scraping import campaign_manager_write as write_module
from linkedin_mcp_server.scraping.campaign_manager_editor import CampaignManagerEditor
from linkedin_mcp_server.scraping.campaign_manager_write import CampaignWriteFlow
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

CM = "https://www.linkedin.com/campaign-manager-api/"
ACCOUNT = "100000001"
GROUP = "100000201"
CAMPAIGN = "100000301"
NEW = "100000302"
APP = f"https://www.linkedin.com/campaignmanager/accounts/{ACCOUNT}"
DETAILS = f"{APP}/campaigns/{CAMPAIGN}/details"
SUMMARY = f"{APP}/campaigns/{CAMPAIGN}/summary"
SETUP = f"{APP}/campaigns/new/setup?campaignGroupId={GROUP}"
TODAY = date(2030, 1, 1)
END_TIME = (23 * 60 + 45) * 60_000


def _ms(day: date, extra: int = 0) -> int:
    return int(
        datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()
    ) * 1000 + (extra)


def _campaign(**overrides: Any) -> dict[str, Any]:
    record = {
        "id": int(CAMPAIGN),
        "accountId": int(ACCOUNT),
        "campaignGroupId": int(GROUP),
        "name": "Example ad set",
        "status": "DRAFT",
        "objectiveType": "WEBSITE_VISIT",
        "dailyBudget": {"amount": "10", "currencyCode": "EUR"},
        "runSchedule": {
            "start": _ms(date(2030, 1, 10)),
            "end": _ms(date(2030, 2, 20), END_TIME),
        },
    }
    record.update(overrides)
    return record


def _response(
    path: str,
    payload: Any = None,
    *,
    status: int = 200,
    method: str = "GET",
    body: Any = None,
    restli: str | None = None,
    created_id: str | None = None,
) -> SimpleNamespace:
    headers = {"x-restli-method": restli} if restli else {}
    request = SimpleNamespace(
        method=method,
        url=CM + path,
        headers={},
        post_data=json.dumps(body) if body is not None else None,
        all_headers=AsyncMock(return_value=headers),
    )
    answer = {"x-restli-id": created_id} if created_id else {}
    return SimpleNamespace(
        url=CM + path,
        status=status,
        request=request,
        json=AsyncMock(return_value=payload),
        all_headers=AsyncMock(return_value=answer),
    )


def _emit(page: Any, responses: list[SimpleNamespace]) -> None:
    for response in responses:
        for callback in list(page.listeners.get("response", [])):
            callback(response)


def _handler(page: Any) -> Any:
    (handler,) = [
        call.args[1]
        for call in page.route.await_args_list
        if call.args[0] == "**/campaign-manager-api/**"
    ]
    return handler


def _set(**fields: Any) -> dict[str, Any]:
    return {"patch": {"$set": fields}}


class _App:
    """Campaign Manager as the editor sees it: pages, DOM programs and saves."""

    def __init__(self, page: Any, record: dict[str, Any]) -> None:
        self.page = page
        self.records = {CAMPAIGN: record}
        self.save_status = 200
        self.keeps = True
        self.objectives = ["BRAND_AWARENESS", "WEBSITE_VISIT"]
        self.states: dict[str, str] = {}
        self.dialog_on_switch = False
        self.dialog = False
        # The political-ad question: None absent, else how it answers.
        self.political: Callable[[Any], Awaitable[dict]] | None = None
        self.group_write_on_setup = False
        self.on_blur: Callable[[str, str], dict | None] | None = None
        self.picks: list[dict | None] = []
        self.on_footer: dict | None = None
        self.calls: list[tuple[str, Any]] = []
        self.navigated: list[str] = []
        self.typed: list[str] = []
        self.sent: list[tuple[str, str]] = []
        self.focused: str | None = None
        # The classic editor saves a draft only by its footer.
        self.classic = False
        page.url = DETAILS
        page.main_frame = SimpleNamespace(url=page.url)
        page.evaluate = AsyncMock(side_effect=self.evaluate)
        page.keyboard = SimpleNamespace(
            insert_text=AsyncMock(side_effect=self.typed.append),
            press=AsyncMock(),
        )
        self.programs = {
            editor_module._INPUT_JS: "input",
            editor_module._TYPED_JS: "typed",
            editor_module._BLUR_JS: "blur",
            editor_module._INPUT_STATE_JS: "input_state",
            write_module._CLASSIC_FOOTER_JS: "classic",
            editor_module._SWITCH_JS: "switch",
            editor_module._DIALOG_JS: "dialog",
            editor_module._OBJECTIVE_JS: "objective",
            editor_module._DATE_INPUT_JS: "date_input",
            editor_module._CALENDAR_JS: "calendar",
            write_module._FOOTER_JS: "footer",
            political_module.POLITICAL_JS: "political",
        }

    def _move(self, url: str) -> None:
        self.page.url = url
        self.page.main_frame = SimpleNamespace(url=url)
        for callback in list(self.page.listeners.get("framenavigated", [])):
            callback(self.page.main_frame)

    async def navigate(self, url: str) -> None:
        self.navigated.append(url)
        self._move(url)
        if url == SETUP:
            if self.group_write_on_setup:
                await self.send(
                    "campaignManagerCampaignGroups", {"name": "Auto"}, "create"
                )
            group = {
                "id": int(GROUP),
                "accountId": int(ACCOUNT),
                "name": "Example group",
            }
            _emit(
                self.page, [_response(f"campaignManagerCampaignGroups/{GROUP}", group)]
            )
            return
        for campaign, record in self.records.items():
            if f"/campaigns/{campaign}/" in url:
                payload = {"data": dict(record), "included": []}
                _emit(
                    self.page,
                    [_response(f"campaignManagerCampaigns/{campaign}", payload)],
                )

    async def send(
        self,
        path: str,
        body: dict,
        restli: str,
        *,
        created_id: str | None = None,
    ) -> bool:
        response = _response(
            path,
            {},
            status=self.save_status,
            method="POST",
            body=body,
            restli=restli,
            created_id=created_id,
        )
        outcome: list[str] = []
        route = SimpleNamespace(
            request=response.request,
            abort=AsyncMock(side_effect=lambda: outcome.append("abort")),
            fallback=AsyncMock(side_effect=lambda: outcome.append("fallback")),
        )
        await _handler(self.page)(route)
        self.sent.append((path, outcome[0]))
        if outcome[0] != "fallback":
            return False
        _emit(self.page, [response])
        campaign = path.rsplit("/", 1)[-1]
        if 200 <= self.save_status < 300 and self.keeps and campaign in self.records:
            self.records[campaign].update(body["patch"]["$set"])
        return True

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        name = self.programs[script]
        self.calls.append((name, arg))
        if name == "input":
            state = self.states.get(arg["id"], "ready")
            if arg["focus"] and state == "ready":
                self.focused = arg["id"]
            return {"state": state}
        if name == "typed":
            return {"state": "typed" if self.typed[-1] == arg["text"] else "mismatch"}
        if name == "input_state":
            return {"count": 1, "attached": True, "focused": False, "empty": False}
        if name == "classic":
            return 1 if self.classic else 0
        if name == "blur":
            body = self.on_blur(arg, self.typed[-1]) if self.on_blur else None
            if body is not None:
                campaign = NEW if arg == "campaign-name" else CAMPAIGN
                await self.send(
                    f"campaignManagerCampaigns/{campaign}", body, "partial_update"
                )
            return {"blurred": True}
        if name == "switch":
            active = self.records[CAMPAIGN]["status"] == "ACTIVE"
            state = self.states.get("switch", "ready")
            if state == "ready" and arg["checked"] != active:
                state = "mismatch"
            if arg["click"] and state == "ready":
                if self.dialog_on_switch:
                    self.dialog = True
                else:
                    status = "PAUSED" if active else "ACTIVE"
                    await self.send(
                        f"campaignManagerCampaigns/{CAMPAIGN}",
                        _set(status=status),
                        "partial_update",
                    )
            return {"state": state}
        if name == "dialog":
            return self.dialog
        if name == "political":
            if self.political is None:
                return {"state": "absent"}
            return await self.political(arg)
        if name == "objective":
            ready = arg["value"] in self.objectives
            return {
                "state": "ready" if ready else "unavailable",
                "available": self.objectives,
            }
        if name == "date_input":
            return {"state": self.states.get(arg["name"], "ready")}
        if name == "calendar":
            if arg["mode"] == "pick" and self.picks:
                body = self.picks.pop(0)
                if body is not None:
                    await self.send(
                        f"campaignManagerCampaigns/{CAMPAIGN}", body, "partial_update"
                    )
            return {"ok": True}
        if name == "footer":
            if self.on_footer is not None:
                await self.send(
                    f"campaignManagerCampaigns/{CAMPAIGN}",
                    self.on_footer,
                    "partial_update",
                )
            return {"clicked": True, "count": 1}
        raise AssertionError(name)

    def clicked(self, name: str) -> list[Any]:
        return [
            arg
            for program, arg in self.calls
            if program == name and isinstance(arg, dict) and arg.get("click")
        ]

    @contextmanager
    def running(self) -> Iterator[None]:
        with (
            patch.object(
                PageNavigator, "_navigate_to_page", AsyncMock(side_effect=self.navigate)
            ),
            patch.object(ScrapingSession, "check_rate_limit", new_callable=AsyncMock),
            patch.object(ScrapingSession, "dismiss_modal", new_callable=AsyncMock),
            patch.object(write_module, "_READY_TIMEOUT", 0.3),
            patch.object(editor_module, "_READY_TIMEOUT", 0.3),
            patch.object(editor_module, "_SAVE_TIMEOUT", 0.3),
            patch.object(editor_module, "_POLL_INTERVAL", 0.01),
            patch.object(editor_module, "_DIALOG_GRACE", 0.05),
            patch.object(editor_module, "_today", return_value=TODAY),
        ):
            yield


def _editor(page: Any) -> CampaignManagerEditor:
    session = ScrapingSession(page)
    return CampaignManagerEditor(session, PageNavigator(session))


def _budget_autosave(_input: str, text: str) -> dict:
    return _set(dailyBudget={"amount": text, "currencyCode": "EUR"})


async def _update(page: Any, app: _App, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("confirm_send", True)
    with app.running():
        return await _editor(page).update_campaign(ACCOUNT, CAMPAIGN, **kwargs)


# --- update_campaign -------------------------------------------------------------


async def test_dry_run_verifies_and_plans_without_a_window_or_an_edit(mock_page):
    app = _App(mock_page, _campaign())

    with patch.object(CampaignWriteFlow, "apply", side_effect=AssertionError):
        result = await _update(mock_page, app, daily_budget="12.5", confirm_send=False)

    assert result["status"] == "confirmation_required"
    assert result["retry_safe"] is True
    assert result["planned"] == {
        "dailyBudget": {"amount": "12.5", "currencyCode": "EUR"},
        "politicalAdQuestion": "may",
    }
    assert result["target"]["id"] == int(CAMPAIGN)
    mock_page.keyboard.insert_text.assert_not_awaited()
    assert app.sent == []


async def test_invalid_budget_is_refused_before_the_browser(mock_page):
    app = _App(mock_page, _campaign())

    result = await _update(mock_page, app, daily_budget="12,5")

    assert result["status"] == "invalid_change"
    assert result["retry_safe"] is True
    assert app.navigated == []


async def test_draft_budget_autosave_admitted_and_reread_is_updated(mock_page):
    app = _App(mock_page, _campaign())
    app.on_blur = _budget_autosave

    result = await _update(mock_page, app, daily_budget="12.5")

    assert result["status"] == "updated"
    assert result["retry_safe"] is False
    assert result["applied"] == {
        "dailyBudget": {"amount": "12.5", "currencyCode": "EUR"}
    }
    assert app.typed == ["12.5"]
    assert app.sent == [(f"campaignManagerCampaigns/{CAMPAIGN}", "fallback")]
    assert "refused" not in result


async def test_off_plan_autosave_is_aborted_as_change_blocked(mock_page):
    app = _App(mock_page, _campaign())
    app.on_blur = lambda _input, _text: _set(
        dailyBudget={"amount": "99", "currencyCode": "EUR"}
    )

    result = await _update(mock_page, app, daily_budget="12.5")

    assert result["status"] == "change_blocked"
    assert result["retry_safe"] is True
    assert result["refused"] == [
        {
            "method": "POST",
            "resource": "campaignManagerCampaigns",
            "reason": "dailyBudget is not the planned value",
        }
    ]
    assert app.records[CAMPAIGN]["dailyBudget"]["amount"] == "10"
    # The refused edit is discarded by leaving the page.
    assert app.navigated[-1] == DETAILS


async def test_refused_save_is_save_failed(mock_page):
    app = _App(mock_page, _campaign())
    app.on_blur = _budget_autosave
    app.save_status = 400

    result = await _update(mock_page, app, daily_budget="12.5")

    assert result["status"] == "save_failed"
    assert result["retry_safe"] is True


async def test_reread_mismatch_is_save_unverified(mock_page):
    app = _App(mock_page, _campaign())
    app.on_blur = _budget_autosave
    app.keeps = False

    result = await _update(mock_page, app, daily_budget="12.5")

    assert result["status"] == "save_unverified"
    assert result["retry_safe"] is False


async def test_interrupted_second_date_is_partially_applied(mock_page):
    app = _App(mock_page, _campaign())
    old_end = _campaign()["runSchedule"]["end"]
    new_start = _ms(date(2030, 1, 15))
    app.picks = [
        _set(runSchedule={"start": new_start, "end": old_end}),
        _set(runSchedule={"start": new_start, "end": _ms(date(2030, 4, 1), END_TIME)}),
    ]

    result = await _update(
        mock_page, app, start_date=date(2030, 1, 15), end_date=date(2030, 3, 5)
    )

    assert result["status"] == "partially_applied"
    assert result["retry_safe"] is False
    assert result["applied"] == {"startDate": "2030-01-15", "endDate": "2030-02-20"}
    steps = [
        arg for name, arg in app.calls if name == "calendar" and arg["mode"] == "step"
    ]
    assert steps == [{"mode": "step", "forward": True, "days": 0, "day": 0}]


async def test_launched_campaign_saves_once_by_save_and_exit(mock_page):
    app = _App(
        mock_page,
        _campaign(
            status="PAUSED",
            dailyBudget=None,
            totalBudget={"amount": "500", "currencyCode": "EUR"},
        ),
    )
    app.on_footer = _set(totalBudget={"amount": "650", "currencyCode": "EUR"})

    result = await _update(mock_page, app, lifetime_budget="650")

    assert result["status"] == "updated"
    assert app.typed == ["650"]
    assert app.sent == [(f"campaignManagerCampaigns/{CAMPAIGN}", "fallback")]


async def test_disabled_budget_input_is_not_editable(mock_page):
    app = _App(mock_page, _campaign())
    app.states["bid-and-budget__daily-budget"] = "disabled"

    result = await _update(mock_page, app, daily_budget="12.5")

    assert result["status"] == "not_editable"
    mock_page.keyboard.insert_text.assert_not_awaited()


# --- set_campaign_status ---------------------------------------------------------


async def _status(page: Any, app: _App, status: str, **kwargs: Any) -> dict:
    kwargs.setdefault("confirm_send", True)
    with app.running():
        return await _editor(page).set_campaign_status(
            ACCOUNT, CAMPAIGN, status=status, **kwargs
        )


async def test_draft_is_never_launched(mock_page):
    app = _App(mock_page, _campaign(status="DRAFT"))

    result = await _status(mock_page, app, "ACTIVE")

    assert result["status"] == "not_editable"
    assert result["retry_safe"] is True
    assert app.clicked("switch") == []
    assert app.sent == []


async def test_pause_is_admitted_and_verified(mock_page):
    app = _App(mock_page, _campaign(status="ACTIVE"))

    result = await _status(mock_page, app, "PAUSED")

    assert result["status"] == "paused"
    assert result["retry_safe"] is False
    assert result["applied"] == {"status": "PAUSED"}
    assert app.navigated[0] == SUMMARY


async def test_same_status_is_no_change(mock_page):
    app = _App(mock_page, _campaign(status="PAUSED"))

    result = await _status(mock_page, app, "PAUSED")

    assert result["status"] == "no_change"
    assert app.clicked("switch") == []


async def test_resume_reactivation_dialog_is_closed_with_escape(mock_page):
    app = _App(mock_page, _campaign(status="PAUSED"))
    app.dialog_on_switch = True

    result = await _status(mock_page, app, "ACTIVE")

    assert result["status"] == "reactivation_needs_changes"
    assert result["retry_safe"] is True
    mock_page.keyboard.press.assert_any_await("Escape")
    assert app.sent == []


async def test_switch_out_of_step_with_the_record_is_not_clicked(mock_page):
    app = _App(mock_page, _campaign(status="ACTIVE"))
    app.states["switch"] = "mismatch"

    result = await _status(mock_page, app, "PAUSED")

    assert result["status"] == "control_unavailable"
    assert app.clicked("switch") == []


# --- create_campaign_draft -------------------------------------------------------


async def _create(page: Any, app: _App, **kwargs: Any) -> dict:
    kwargs.setdefault("confirm_send", True)
    kwargs.setdefault("objective", "WEBSITE_VISIT")
    kwargs.setdefault("name", "Example draft")
    with app.running():
        return await _editor(page).create_campaign_draft(ACCOUNT, GROUP, **kwargs)


async def test_create_with_a_refused_group_write_is_no_campaign_group(mock_page):
    app = _App(mock_page, _campaign())
    app.group_write_on_setup = True

    result = await _create(mock_page, app)

    assert result["status"] == "no_campaign_group"
    assert result["retry_safe"] is True
    assert app.sent == [("campaignManagerCampaignGroups", "abort")]
    assert app.navigated == [SETUP]


async def test_unknown_objective_lists_the_available_ones(mock_page):
    app = _App(mock_page, _campaign())

    result = await _create(mock_page, app, objective="VIDEO_VIEW")

    assert result["status"] == "objective_unavailable"
    assert result["planned"]["available"] == ["BRAND_AWARENESS", "WEBSITE_VISIT"]
    assert app.sent == []


async def test_create_dry_run_creates_nothing(mock_page):
    app = _App(mock_page, _campaign())

    result = await _create(mock_page, app, confirm_send=False)

    assert result["status"] == "confirmation_required"
    assert result["target"]["kind"] == "campaign_group"
    assert result["planned"]["politicalAdQuestion"] == "may"
    assert app.navigated == [SETUP]


async def test_campaign_group_id_must_be_numeric(mock_page):
    with pytest.raises(InvalidReferenceError):
        await _editor(mock_page).create_campaign_draft(
            ACCOUNT, "example", objective="WEBSITE_VISIT", name="x", confirm_send=False
        )
