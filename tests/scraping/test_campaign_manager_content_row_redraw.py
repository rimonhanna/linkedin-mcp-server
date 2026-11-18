"""Clicks follow the row ids the post was located by (synthetic data only).

The table can redraw between finding the post's row and clicking it. With the
same number of rows but other ids, nothing is clicked.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.response_structure import ResponseLog
from linkedin_mcp_server.scraping.campaign_manager_write import ANCHORS

from scraping.test_campaign_manager_content import (
    DETAILS,
    LIBRARY_PATH,
    OTHER,
    POST,
    _App,
    _emit,
    _response,
    _run,
)
from scraping.test_campaign_manager_content_row_identity import _urn_id
from unittest.mock import patch


class _RedrawApp(_App):
    """URN-keyed rows that are reordered once the post's row was located."""

    def __init__(self, page: Any) -> None:
        super().__init__(page)
        page.url = DETAILS
        page.main_frame = SimpleNamespace(url=DETAILS)
        self.shown = [_urn_id(OTHER), _urn_id(POST)]
        self.redraw = True

    def _state(self, checked: int | None = None) -> dict:
        return {
            "rows": [
                {"id": row_id, "disabled": False, "checked": i == checked}
                for i, row_id in enumerate(self.shown)
            ],
            "add": {"disabled": checked is None},
        }

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if "linkedin-mcp:cm-content-rows" in script:
            state = self._state()
            if self.redraw:
                self.shown = [_urn_id(POST), _urn_id(OTHER)]
            return state
        return await super().evaluate(script, arg)


async def test_a_reordered_table_is_not_clicked(mock_page):
    app = _RedrawApp(mock_page)

    result = await _run(mock_page, app, confirm=True)

    assert result["status"] == "control_unavailable"
    assert result["retry_safe"] is True
    assert app.clicks == []
    assert app.sent == []


class _NoPagingApp(_App):
    """A library answer whose request and answer give no page size or total."""

    def __init__(self, page: Any, *, count: str | None, paging: dict | None) -> None:
        super().__init__(page)
        self.page_size = count
        self.paging = paging

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if "linkedin-mcp:cm-route" not in script:
            return await super().evaluate(script, arg)
        self.navigated.append(arg)
        self.page.url = "https://www.linkedin.com" + arg
        elements = [{"content": {"*sponsoredUpdateContent": OTHER}}]
        data: dict[str, Any] = {"elements": elements}
        if self.paging is not None:
            data["paging"] = self.paging
        included = [
            {
                "entityUrn": OTHER,
                "sponsorable": True,
                "nonSponsorableReasons": [],
                "visibility": "ORGANIC",
            }
        ]
        size = f"&count={self.page_size}" if self.page_size is not None else ""
        payload = {"data": data, "included": included}
        _emit(self.page, [_response(f"{LIBRARY_PATH}{size}&start=0", payload)])
        return True


@pytest.mark.parametrize(
    ("count", "paging", "whole"),
    [
        ("100", None, True),
        ("1", None, False),
        (None, {"count": 10, "start": 0}, True),
        (None, {"count": 1, "start": 0}, False),
        (None, None, False),
        (None, {"count": 1, "start": 0, "total": 1}, True),
    ],
    ids=[
        "short-page",
        "full-page",
        "answer-page-size",
        "answer-full-page",
        "no-size",
        "total",
    ],
)
async def test_not_in_the_library_only_when_it_is_provably_whole(
    mock_page, count, paging, whole
):
    app = _NoPagingApp(mock_page, count=count, paging=paging)
    mock_page.url = DETAILS
    mock_page.main_frame = SimpleNamespace(url=DETAILS)

    result = await _run(mock_page, app, confirm=False)

    assert result["status"] == "post_not_found"
    if whole:
        assert "is not in this ad set's content library" in result["message"]
    else:
        assert "is not among the 1 most recent" in result["message"]


async def test_one_stop_is_recorded_once_when_the_row_anchor_timed_out(mock_page):
    app = _RedrawApp(mock_page)
    app.shown = [_urn_id(OTHER)]
    app.redraw = False

    async def rows_never_render(_script: str, *, arg: str, timeout: float) -> None:
        if arg == ANCHORS["library_rows"]:
            raise PlaywrightTimeoutError("timeout")

    mock_page.wait_for_function.side_effect = rows_never_render
    log = ResponseLog("test")

    with patch(
        "linkedin_mcp_server.scraping.campaign_manager_write.current_log",
        return_value=log,
    ):
        result = await _run(mock_page, app, confirm=False)

    assert result["status"] == "control_unavailable"
    (page,) = log.pages
    assert page["library_rows"]["row_ids"] == ["urn"]
