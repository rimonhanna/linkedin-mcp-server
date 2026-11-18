"""A classic draft whose Save and exit LinkedIn keeps disabled after a targeting
edit stops with the reason, not as a footer that was not found.

Live (2026-11-18): a draft whose start date had passed showed the footer with
every button disabled (the classic editor's ``isTriggerButtonDisabled`` while
the ad set fails validation), and the stop said Save and exit "was not found".
Synthetic data only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_mcp_server.scraping import campaign_manager_write as write_module
from linkedin_mcp_server.scraping.campaign_manager_write import (
    CampaignWriteFlow,
    footer_disabled_reason,
)

from .test_campaign_manager_targeting import (
    CAMPAIGN,
    ORIGINAL,
    _emit,
    _LinkedIn,
    _Module,
    _record,
    _response,
    _update,
)
from .test_campaign_manager_write import _footer, _footer_click, _LABELS, dom_page

__all__ = ["dom_page"]


def _start_ms(days_from_today: int) -> int:
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((today + timedelta(days=days_from_today)).timestamp() * 1000)


class _Scheduled(_LinkedIn):
    """The ad set with a start date ``days`` from today."""

    def __init__(self, page: Any, days: int) -> None:
        super().__init__(page)
        self.start = _start_ms(days)

    async def navigate(self, url: str) -> None:
        self.navigated.append(url)
        self.page.url = url
        self.page.main_frame = type("Frame", (), {"url": url})()
        record = _record(self.keys, self.status)
        record["data"]["runSchedule"] = {"start": self.start}
        _emit(self.page, _response(f"campaignManagerCampaigns/{CAMPAIGN}", record))


async def _disabled_footer(mock_page: Any, linkedin: _LinkedIn) -> dict[str, Any]:
    module = _Module(linkedin)
    module.autosave = False

    async def save_and_exit(flow: Any, page: Any, *, draft: bool) -> bool:
        flow.footer_disabled = True
        return False

    with (
        patch.object(write_module.CampaignWriteFlow, "save_and_exit", save_and_exit),
        patch.object(
            write_module.CampaignWriteFlow,
            "saves_by_footer",
            AsyncMock(return_value=True),
        ),
    ):
        return await _update(mock_page, linkedin, module)


async def test_passed_start_date_is_named_as_why_nothing_was_saved(mock_page):
    linkedin = _Scheduled(mock_page, days=-1)

    result = await _disabled_footer(mock_page, linkedin)

    assert result["status"] == "control_unavailable"
    assert "start date has passed" in result["message"], result["message"]
    assert "not found" not in result["message"]
    assert result["retry_safe"] is True
    assert linkedin.keys == ORIGINAL


async def test_other_invalid_field_is_reported_as_a_disabled_save(mock_page):
    linkedin = _Scheduled(mock_page, days=1)

    result = await _disabled_footer(mock_page, linkedin)

    assert "fields is invalid" in result["message"], result["message"]
    assert "start date" not in result["message"]


@pytest.mark.parametrize(
    ("status", "days", "passed"),
    [("DRAFT", -1, True), ("DRAFT", 0, False), ("ACTIVE", -30, False)],
)
def test_only_a_drafts_passed_start_day_is_named(status, days, passed):
    record = {"status": status, "runSchedule": {"start": _start_ms(days)}}

    assert ("start date has passed" in footer_disabled_reason(record)) is passed


async def test_unclicked_save_and_exit_records_the_page_structure(mock_page):
    mock_page.evaluate = AsyncMock(
        return_value={"count": 2, "clicked": False, "disabled": True}
    )
    flow = CampaignWriteFlow(MagicMock(), MagicMock())

    with patch.object(
        CampaignWriteFlow, "record_page_structure", AsyncMock()
    ) as structure:
        assert await flow.save_and_exit(mock_page, draft=True) is False

    structure.assert_awaited_once_with(mock_page)
    assert flow.footer_disabled is True


@pytest.mark.browser_dom
@pytest.mark.xdist_group("browser_runtime")
async def test_footer_tells_a_disabled_save_from_a_missing_one(dom_page):
    # As captured live: Previous, Save and exit and Next all disabled.
    disabled = _footer(_LABELS["en"], previous=True).replace(
        "<button", "<button disabled"
    )

    with patch.object(CampaignWriteFlow, "record_page_structure", AsyncMock()):
        assert await _footer_click(dom_page, disabled, draft=True) == []
        flow = CampaignWriteFlow(MagicMock(), MagicMock())
        assert await flow.save_and_exit(dom_page, draft=True) is False
        assert flow.footer_disabled is True

        await dom_page.set_content("<p>no footer</p>")
        assert await flow.save_and_exit(dom_page, draft=True) is False
        assert flow.footer_disabled is False
