"""A launched ad set's targeting Save and exit and the political-ad question.

Synthetic data only: the scripted targeting module and LinkedIn of the
targeting tests stand in for Campaign Manager; the question is scripted at
``political_dialog``, whose program the DOM tests cover.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from linkedin_mcp_server.scraping import campaign_manager_targeting as targeting
from linkedin_mcp_server.scraping import campaign_manager_write as write_module

from .test_campaign_manager_targeting import (
    ORIGINAL,
    _criteria,
    _LinkedIn,
    _Module,
    _update,
)


def _launched(mock_page: Any) -> tuple[_LinkedIn, _Module]:
    linkedin = _LinkedIn(mock_page, status="PAUSED")
    module = _Module(linkedin)
    # A launched ad set saves only by Save and exit.
    module.autosave = False
    mock_page.keyboard.press = AsyncMock()
    return linkedin, module


async def _run(
    mock_page: Any,
    linkedin: _LinkedIn,
    module: _Module,
    intent: str,
    **kwargs: Any,
) -> tuple[dict[str, Any], list[Any]]:
    """Save and exit asks; answering it saves the targeting with ``intent``."""
    asked: list[Any] = []
    answered: list[bool] = []

    async def save_and_exit(_flow: Any, page: Any, *, draft: bool) -> bool:
        return True

    async def question(page: Any, political_ad: bool | None) -> str:
        if answered:
            return "absent"
        asked.append(political_ad)
        if political_ad is None:
            return "open"
        answered.append(True)
        await linkedin.send(
            {
                "patch": {
                    "$set": {
                        "targetingCriteria": _criteria(module.keys),
                        "politicalIntent": intent,
                    }
                }
            }
        )
        return "answered"

    with (
        patch.object(write_module.CampaignWriteFlow, "save_and_exit", save_and_exit),
        # Save and exit is the classic editor's footer.
        patch.object(
            write_module.CampaignWriteFlow,
            "saves_by_footer",
            AsyncMock(return_value=True),
        ),
        patch.object(targeting, "political_dialog", question),
        patch.object(targeting, "_SAVE_TIMEOUT", 0.5),
    ):
        result = await _update(mock_page, linkedin, module, **kwargs)
    return result, asked


async def test_question_without_a_declaration_stops_and_sends_nothing(mock_page):
    linkedin, module = _launched(mock_page)

    result, asked = await _run(mock_page, linkedin, module, "NOT_POLITICAL")

    assert result["status"] == "declaration_required"
    assert result["retry_safe"] is True
    assert "political_ad true" in result["message"]
    assert asked == [None]
    mock_page.keyboard.press.assert_any_await("Escape")
    assert linkedin.keys == ORIGINAL


async def test_declared_political_answers_that_and_saves(mock_page):
    linkedin, module = _launched(mock_page)

    result, asked = await _run(
        mock_page, linkedin, module, "POLITICAL", political_ad=True
    )

    assert result["status"] == "updated", result["message"]
    assert asked == [True]
    assert linkedin.keys != ORIGINAL


async def test_a_save_with_the_other_declaration_is_refused(mock_page):
    linkedin, module = _launched(mock_page)

    result, _ = await _run(
        mock_page, linkedin, module, "NOT_POLITICAL", political_ad=True
    )

    assert result["status"] == "change_blocked"
    assert result["retry_safe"] is True
    assert result["refused"][0]["reason"] == "politicalIntent is not the planned value"
    assert linkedin.keys == ORIGINAL
