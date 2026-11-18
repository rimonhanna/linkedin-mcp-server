"""update_campaign_targeting reports what became of the political-ad declaration.

Synthetic data only: the scripted targeting module and LinkedIn of the
targeting tests stand in for Campaign Manager; this LinkedIn also keeps the
``politicalIntent`` a save sets, so the re-read shows it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from linkedin_mcp_server.scraping import campaign_manager_targeting as targeting
from linkedin_mcp_server.scraping import campaign_manager_write as write_module

from .test_campaign_manager_targeting import (
    CAMPAIGN,
    _criteria,
    _emit,
    _LinkedIn,
    _Module,
    _record,
    _response,
    _update,
)
from .test_campaign_manager_targeting_political import _run


class _Keeping(_LinkedIn):
    def __init__(self, page: Any) -> None:
        super().__init__(page, status="PAUSED")
        self.intent: str | None = None

    async def navigate(self, url: str) -> None:
        self.navigated.append(url)
        self.page.url = url
        self.page.main_frame = SimpleNamespace(url=url)
        payload = _record(self.keys, self.status)
        if self.intent is not None:
            payload["data"]["politicalIntent"] = self.intent
        _emit(self.page, _response(f"campaignManagerCampaigns/{CAMPAIGN}", payload))

    async def send(self, body: dict) -> str:
        outcome = await super().send(body)
        if outcome == "fallback":
            self.intent = body["patch"]["$set"].get("politicalIntent", self.intent)
        return outcome


def _launched(mock_page: Any) -> tuple[_Keeping, _Module]:
    linkedin = _Keeping(mock_page)
    module = _Module(linkedin)
    module.autosave = False
    mock_page.keyboard.press = AsyncMock()
    return linkedin, module


async def test_a_dry_run_shows_the_declared_intent_in_planned(mock_page):
    linkedin, module = _launched(mock_page)

    result = await _update(
        mock_page, linkedin, module, political_ad=False, confirm_send=False
    )

    assert result["status"] == "confirmation_required"
    assert result["planned"]["politicalIntent"] == "NOT_POLITICAL"


async def test_an_answered_declaration_that_reads_back_is_reported_recorded(mock_page):
    linkedin, module = _launched(mock_page)

    result, _ = await _run(mock_page, linkedin, module, "POLITICAL", political_ad=True)

    assert result["status"] == "updated", result["message"]
    assert result["applied"]["politicalIntent"] == "POLITICAL"
    assert "declaration was recorded" in result["message"]


async def test_a_declaration_no_dialog_asked_for_is_reported_not_recorded(mock_page):
    linkedin, module = _launched(mock_page)

    async def save_and_exit(_flow: Any, page: Any, *, draft: bool) -> bool:
        await linkedin.send(
            {"patch": {"$set": {"targetingCriteria": _criteria(module.keys)}}}
        )
        return True

    async def no_question(page: Any, political_ad: bool | None) -> str:
        return "absent"

    with (
        patch.object(write_module.CampaignWriteFlow, "save_and_exit", save_and_exit),
        patch.object(
            write_module.CampaignWriteFlow,
            "saves_by_footer",
            AsyncMock(return_value=True),
        ),
        patch.object(targeting, "political_dialog", no_question),
    ):
        result = await _update(mock_page, linkedin, module, political_ad=True)

    assert result["status"] == "updated", result["message"]
    assert result["applied"]["politicalIntent"] is None
    assert "declaration is not recorded" in result["message"]
    assert "was recorded" not in result["message"]
