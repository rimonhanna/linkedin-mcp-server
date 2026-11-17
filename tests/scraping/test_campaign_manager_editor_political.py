"""The political-ad question Save and exit asks on EU targeting.

The answer is the caller's own declaration: without ``political_ad`` the
question is closed and nothing is sent, with it exactly that answer is given
and only the matching ``politicalIntent`` is admitted. Synthetic data only:
the scripted app of the editor tests stands in for Campaign Manager.
"""

from __future__ import annotations

from typing import Any

import pytest

from linkedin_mcp_server.scraping.campaign_manager_political import (
    political_question,
)

from .test_campaign_manager_editor import (
    CAMPAIGN,
    DETAILS,
    _App,
    _campaign,
    _set,
    _update,
)

LOCATIONS = "urn:li:adTargetingFacet:locations"
GERMANY = "urn:li:geo:101282230"
EUROPE = "urn:li:geo:100506914"
DACH = "urn:li:geo:91000006"
AUSTRIA = "urn:li:geo:103883259"
# Not one of the app's EU countries or regions.
ELSEWHERE = "urn:li:geo:100000009"
BUDGET = {"amount": "11", "currencyCode": "EUR"}


def _targeting(*include: str, exclude: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "include": {"and": [{"or": {LOCATIONS: list(include)}}]},
        "exclude": {"or": {LOCATIONS: list(exclude)}} if exclude else {"or": {}},
    }


def _keys(*include: str, exclude: tuple[str, ...] = ()) -> list[tuple[str, str, str]]:
    return [("include_locations", LOCATIONS, urn) for urn in include] + [
        ("exclude_locations", LOCATIONS, urn) for urn in exclude
    ]


def _eu_app(mock_page: Any) -> _App:
    app = _App(
        mock_page, _campaign(status="ACTIVE", targetingCriteria=_targeting(GERMANY))
    )
    app.classic = True
    return app


def _asking(app: _App, saves: dict | None) -> list[Any]:
    """The question stays open until answered; answering sends ``saves``."""
    asked: list[Any] = []

    async def answer(arg: dict) -> dict:
        asked.append(arg["political"])
        if arg["political"] is None:
            return {"state": "open"}
        if saves is not None:
            await app.send(
                f"campaignManagerCampaigns/{CAMPAIGN}", saves, "partial_update"
            )
        app.political = None
        return {"state": "answered"}

    app.political = answer
    return asked


# --- political_question --------------------------------------------------------


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        (_keys(GERMANY), "will"),
        (_keys(EUROPE), "will"),
        (_keys(DACH), "will"),
        (_keys(EUROPE, exclude=(GERMANY,)), "will"),
        (_keys(GERMANY, exclude=(GERMANY,)), "may"),
        (_keys(DACH, exclude=(GERMANY, AUSTRIA)), "may"),
        (_keys(ELSEWHERE), "may"),
        (None, "may"),
    ],
)
def test_question_is_certain_only_for_an_included_eu_location(keys, expected):
    assert political_question(keys) == expected


# --- update_campaign -------------------------------------------------------------


async def test_no_question_saves_without_a_declaration(mock_page):
    app = _eu_app(mock_page)
    app.on_footer = _set(dailyBudget=BUDGET)

    result = await _update(mock_page, app, daily_budget="11")

    assert result["status"] == "updated", result["message"]
    assert app.records[CAMPAIGN]["dailyBudget"] == BUDGET
    assert "politicalIntent" not in app.records[CAMPAIGN]
    assert all(
        call.args != ("Escape",) for call in mock_page.keyboard.press.await_args_list
    )


async def test_question_without_a_declaration_is_closed_and_asks_the_caller(mock_page):
    app = _eu_app(mock_page)
    asked = _asking(app, _set(dailyBudget=BUDGET, politicalIntent="NOT_POLITICAL"))

    result = await _update(mock_page, app, daily_budget="11")

    assert result["status"] == "declaration_required"
    assert result["retry_safe"] is True
    assert "political_ad true" in result["message"]
    assert "legal declaration" in result["message"]
    assert asked == [None]
    mock_page.keyboard.press.assert_any_await("Escape")
    assert app.sent == []
    assert "politicalIntent" not in app.records[CAMPAIGN]
    assert app.navigated[-1] == DETAILS


async def test_declared_not_political_answers_exactly_that_and_saves(mock_page):
    app = _eu_app(mock_page)
    asked = _asking(app, _set(dailyBudget=BUDGET, politicalIntent="NOT_POLITICAL"))

    result = await _update(mock_page, app, daily_budget="11", political_ad=False)

    assert result["status"] == "updated", result["message"]
    assert asked == [False]
    assert app.sent == [(f"campaignManagerCampaigns/{CAMPAIGN}", "fallback")]
    assert app.records[CAMPAIGN]["politicalIntent"] == "NOT_POLITICAL"
    assert app.records[CAMPAIGN]["dailyBudget"] == BUDGET


async def test_declared_political_admits_only_the_political_intent(mock_page):
    app = _eu_app(mock_page)
    asked = _asking(app, _set(dailyBudget=BUDGET, politicalIntent="POLITICAL"))

    result = await _update(mock_page, app, daily_budget="11", political_ad=True)

    assert result["status"] == "updated", result["message"]
    assert asked == [True]
    assert app.records[CAMPAIGN]["politicalIntent"] == "POLITICAL"


async def test_a_save_with_the_other_declaration_is_refused(mock_page):
    app = _eu_app(mock_page)
    _asking(app, _set(dailyBudget=BUDGET, politicalIntent="POLITICAL"))

    result = await _update(mock_page, app, daily_budget="11", political_ad=False)

    assert result["status"] == "change_blocked"
    assert result["retry_safe"] is True
    assert result["refused"] == [
        {
            "method": "POST",
            "resource": "campaignManagerCampaigns",
            "reason": "politicalIntent is not the planned value",
        }
    ]
    assert "politicalIntent" not in app.records[CAMPAIGN]


async def test_a_declaration_the_caller_never_made_is_refused(mock_page):
    app = _eu_app(mock_page)
    app.on_footer = _set(dailyBudget=BUDGET, politicalIntent="NOT_POLITICAL")

    result = await _update(mock_page, app, daily_budget="11")

    assert result["status"] == "change_blocked"
    assert result["refused"][0]["reason"] == "unplanned change to politicalIntent"


async def test_a_question_in_an_unknown_layout_is_closed_unanswered(mock_page):
    app = _eu_app(mock_page)

    async def layout(arg: dict) -> dict:
        return {"state": "layout"}

    app.political = layout

    result = await _update(mock_page, app, daily_budget="11", political_ad=True)

    assert result["status"] == "control_unavailable"
    assert result["retry_safe"] is True
    assert "closed unanswered" in result["message"]
    mock_page.keyboard.press.assert_any_await("Escape")
    assert app.sent == []


async def test_dry_run_says_an_eu_ad_set_will_be_asked(mock_page):
    app = _eu_app(mock_page)

    result = await _update(mock_page, app, daily_budget="11", confirm_send=False)

    assert result["status"] == "confirmation_required"
    assert result["planned"]["politicalAdQuestion"] == "will"
    assert "Saving will make Campaign Manager ask" in result["message"]
    assert "political_ad true or false" in result["message"]


async def test_dry_run_elsewhere_says_it_may_and_a_declared_one_says_nothing(mock_page):
    app = _App(mock_page, _campaign(targetingCriteria=_targeting(ELSEWHERE)))

    asked = await _update(mock_page, app, daily_budget="11", confirm_send=False)
    declared = await _update(
        mock_page, app, daily_budget="11", confirm_send=False, political_ad=False
    )

    assert asked["planned"]["politicalAdQuestion"] == "may"
    assert "Saving may make Campaign Manager ask" in asked["message"]
    assert declared["planned"]["politicalAdQuestion"] == "may"
    assert "political_ad" not in declared["message"]
