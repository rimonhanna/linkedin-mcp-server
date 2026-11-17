"""A draft's targeting in the classic editor saves by Save and exit, and the
political-ad declaration the app writes itself is decided by the guard.

Synthetic data only: the scripted targeting module and LinkedIn of the
targeting tests stand in for Campaign Manager.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from linkedin_mcp_server.scraping import campaign_manager_targeting as targeting
from linkedin_mcp_server.scraping import campaign_manager_write as write_module

from .test_campaign_manager_targeting import (
    CAMPAIGN,
    ORIGINAL,
    _criteria,
    _emit,
    _LinkedIn,
    _Module,
    _record,
    _response,
    _update,
)


class _Declared(_LinkedIn):
    """The ad set as LinkedIn keeps it, with a political-ad declaration."""

    def __init__(self, page: Any, intent: str | None, status: str = "DRAFT") -> None:
        super().__init__(page, status=status)
        self.intent = intent

    async def navigate(self, url: str) -> None:
        self.navigated.append(url)
        self.page.url = url
        self.page.main_frame = type("Frame", (), {"url": url})()
        record = _record(self.keys, self.status)
        if self.intent is not None:
            record["data"]["politicalIntent"] = self.intent
        _emit(self.page, _response(f"campaignManagerCampaigns/{CAMPAIGN}", record))


async def _classic(
    mock_page: Any,
    linkedin: _LinkedIn,
    *,
    intent: str | None = None,
    question: str = "absent",
    **kwargs: Any,
) -> tuple[dict[str, Any], list[bool]]:
    """Save and exit on the classic footer sends the targeting (and ``intent``)."""
    module = _Module(linkedin)
    module.autosave = False
    mock_page.keyboard.press = AsyncMock()
    drafts: list[bool] = []

    async def save_and_exit(_flow: Any, page: Any, *, draft: bool) -> bool:
        drafts.append(draft)
        if question == "absent":
            fields: dict[str, Any] = {"targetingCriteria": _criteria(module.keys)}
            if intent is not None:
                fields["politicalIntent"] = intent
            await linkedin.send({"patch": {"$set": fields}})
        return True

    async def dialog(page: Any, political_ad: bool | None) -> str:
        return question

    with (
        patch.object(write_module.CampaignWriteFlow, "save_and_exit", save_and_exit),
        patch.object(
            write_module.CampaignWriteFlow,
            "saves_by_footer",
            AsyncMock(return_value=True),
        ),
        patch.object(targeting, "political_dialog", dialog),
        patch.object(targeting, "_SAVE_TIMEOUT", 0.5),
    ):
        result = await _update(mock_page, linkedin, module, **kwargs)
    return result, drafts


async def test_classic_draft_saves_once_by_save_and_exit(mock_page):
    linkedin = _LinkedIn(mock_page)

    result, drafts = await _classic(mock_page, linkedin)

    assert result["status"] == "updated", result["message"]
    # The draft footer, never a wait for an autosave the classic editor never sends.
    assert drafts == [True]
    assert linkedin.keys != ORIGINAL


async def test_classic_draft_question_without_a_declaration_sends_nothing(mock_page):
    linkedin = _LinkedIn(mock_page)

    result, drafts = await _classic(mock_page, linkedin, question="open")

    assert result["status"] == "declaration_required"
    assert result["retry_safe"] is True
    assert drafts == [True]
    mock_page.keyboard.press.assert_any_await("Escape")
    assert linkedin.keys == ORIGINAL


async def test_classic_draft_reset_of_no_declaration_is_admitted(mock_page):
    linkedin = _Declared(mock_page, "NOT_DECLARED")

    result, _ = await _classic(mock_page, linkedin, intent="NOT_DECLARED")

    assert result["status"] == "updated", result["message"]


async def test_reset_that_erases_a_declaration_is_refused(mock_page):
    linkedin = _Declared(mock_page, "POLITICAL", status="PAUSED")

    result, drafts = await _classic(mock_page, linkedin, intent="NOT_DECLARED")

    assert result["status"] == "declaration_would_clear"
    assert result["retry_safe"] is True
    assert drafts == [False]
    assert linkedin.keys == ORIGINAL


async def test_reset_despite_a_declaration_is_reported_as_linkedins(mock_page):
    linkedin = _Declared(mock_page, "POLITICAL")

    result, _ = await _classic(
        mock_page, linkedin, intent="NOT_DECLARED", political_ad=True
    )

    assert result["status"] == "declaration_cleared_by_linkedin"
    assert result["retry_safe"] is True
    assert linkedin.keys == ORIGINAL


def _newer(linkedin: _LinkedIn) -> _Module:
    """A draft in the newer editor autosaves NOT_POLITICAL with each edit."""
    module = _Module(linkedin)
    module.save = lambda keys: {
        "patch": {
            "$set": {
                "targetingCriteria": _criteria(keys),
                "politicalIntent": "NOT_POLITICAL",
            }
        }
    }
    return module


async def test_newer_editor_fill_passes_with_a_not_political_declaration(mock_page):
    linkedin = _Declared(mock_page, "NOT_DECLARED")

    result = await _update(mock_page, linkedin, _newer(linkedin), political_ad=False)

    assert result["status"] == "updated", result["message"]


async def test_newer_editor_fill_without_a_declaration_asks_for_one(mock_page):
    linkedin = _Declared(mock_page, "NOT_DECLARED")

    result = await _update(mock_page, linkedin, _newer(linkedin))

    assert result["status"] == "declaration_required"
    assert result["retry_safe"] is True
    assert linkedin.keys == ORIGINAL


async def test_newer_editor_fill_with_a_political_declaration_is_refused(mock_page):
    linkedin = _Declared(mock_page, "NOT_DECLARED")

    result = await _update(mock_page, linkedin, _newer(linkedin), political_ad=True)

    assert result["status"] == "declaration_not_offered"
    assert result["retry_safe"] is True
    assert linkedin.keys == ORIGINAL
