"""The anchor wait's one-shot after a timeout lasts one tool call, and its
diagnostics never raise.

A flow lives as long as the server's extractor, so a page that rendered
slowly once must get the full wait again in the next tool call. Synthetic
mocks only; nothing reaches LinkedIn.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from patchright.async_api import Error as PlaywrightError
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.scraping import campaign_manager_write as write_module
from linkedin_mcp_server.scraping.campaign_manager_write import (
    ANCHORS,
    CampaignWriteFlow,
)

_PAGE_URL = "https://www.linkedin.com/campaignmanager/accounts/1/campaigns/2/details"
_GONE = "Execution context was destroyed, most likely because of a navigation"


class _Capture:
    def __init__(self, page: Any, **_options: Any) -> None:
        pass

    async def __aenter__(self) -> _Capture:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _page() -> MagicMock:
    page = MagicMock()
    page.url = _PAGE_URL
    page.evaluate = AsyncMock(return_value={})
    return page


def _flow(page: Any) -> CampaignWriteFlow:
    return CampaignWriteFlow(MagicMock(page=page), MagicMock())


async def test_a_new_tool_call_waits_the_full_time_on_a_page_that_timed_out():
    page = _page()
    page.wait_for_function = AsyncMock(side_effect=[PlaywrightTimeoutError("t"), None])
    flow = _flow(page)

    with patch.object(write_module, "CampaignManagerCapture", _Capture):
        async with flow.capturing("1"):
            assert await flow.wait_for_anchor(page, ANCHORS["footer"]) is False
        async with flow.capturing("1"):
            assert await flow.wait_for_anchor(page, ANCHORS["footer"]) is True

    timeouts = [
        call.kwargs["timeout"] for call in page.wait_for_function.await_args_list
    ]
    assert timeouts == [write_module._ANCHOR_TIMEOUT * 1000] * 2


async def test_the_same_tool_call_checks_a_timed_out_page_only_once_more():
    page = _page()
    page.wait_for_function = AsyncMock(side_effect=PlaywrightTimeoutError("t"))
    flow = _flow(page)

    with patch.object(write_module, "CampaignManagerCapture", _Capture):
        async with flow.capturing("1"):
            await flow.wait_for_anchor(page, ANCHORS["footer"])
            await flow.wait_for_anchor(page, ANCHORS["footer"])

    assert page.wait_for_function.await_args_list[1].kwargs["timeout"] == 1


async def test_a_structure_note_cut_by_navigation_does_not_raise():
    page = _page()
    page.wait_for_function = AsyncMock(side_effect=PlaywrightTimeoutError("t"))
    page.evaluate = AsyncMock(side_effect=PlaywrightError(_GONE))

    assert await _flow(page).wait_for_anchor(page, ANCHORS["footer"]) is False


async def test_an_anchor_wait_cut_by_navigation_is_not_found():
    page = _page()
    page.wait_for_function = AsyncMock(side_effect=PlaywrightError(_GONE))

    assert await _flow(page).wait_for_anchor(page, ANCHORS["footer"]) is False
