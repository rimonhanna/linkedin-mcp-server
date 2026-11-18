"""The content library row is tied to the post by its id (synthetic data only).

Live 2026-11-18 the library table is keyed by ``urn`` (bundle ``@rowIdKey``)
and shows a filtered, paged copy of the library answer: posts already in the
ad set are dropped and it pages at 5. A row is the post's own only when its id
names the post, or when index-keyed ids cover the whole answer on one page.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from linkedin_mcp_server.response_structure import ResponseLog, track
from linkedin_mcp_server.scraping.campaign_manager_content import locate_row

from scraping.test_campaign_manager_content import (
    DETAILS,
    OTHER,
    POST,
    _App,
    _run,
)


def _urn_id(urn: str) -> str:
    return f"select-row-{urn}-checkbox"


def _index_id(i: int) -> str:
    return f"select-row-{i}-checkbox"


class _KeyedApp(_App):
    """Rows keyed as the live table keys them, possibly a subset of the answer."""

    def __init__(self, page: Any, shown: list[str]) -> None:
        super().__init__(page)
        page.url = DETAILS
        page.main_frame = SimpleNamespace(url=DETAILS)
        self.shown = shown

    def _state(self, checked: int | None = None) -> dict:
        return {
            "rows": [
                {
                    "id": row_id,
                    "disabled": i in self.disabled_rows,
                    "checked": i == checked,
                }
                for i, row_id in enumerate(self.shown)
            ],
            "add": {"disabled": checked is None},
        }


async def test_a_urn_keyed_row_is_found_live_shape(mock_page):
    # One post, one row keyed by its URN: the run that stopped live.
    app = _KeyedApp(mock_page, [_urn_id(POST)])
    app.posts = [(POST, True, [])]

    result = await _run(mock_page, app, confirm=False)

    assert result["status"] == "confirmation_required", result["message"]


async def test_a_client_side_disabled_row_is_not_sponsorable(mock_page):
    # The app's own checks (a missing destination URL) disable the checkbox.
    app = _KeyedApp(mock_page, [_urn_id(POST)])
    app.posts = [(POST, True, [])]
    app.disabled_rows = {0}

    result = await _run(mock_page, app, confirm=False)

    assert result["status"] == "post_not_sponsorable"
    assert app.clicks == []


async def test_the_post_row_is_clicked_where_the_table_shows_it(mock_page):
    # The answer lists OTHER then POST; the table shows POST first and a third
    # row, so answer index 1 is not row 1.
    app = _KeyedApp(
        mock_page, [_urn_id(POST), _urn_id("urn:li:ugcPost:100000799"), _urn_id(OTHER)]
    )

    result = await _run(mock_page, app, confirm=True)

    assert result["status"] == "added", result["message"]
    assert app.clicks == [("row", 0), ("add", 0)]


async def test_an_unidentifiable_row_stops_and_records_the_table(mock_page):
    # Six posts answered, five index-keyed rows on the first page.
    posts = [(f"urn:li:share:10000080{i}", True, []) for i in range(5)]
    app = _KeyedApp(mock_page, [_index_id(i) for i in range(5)])
    app.posts = [*posts, (POST, True, [])]
    log = ResponseLog("test")

    with patch(
        "linkedin_mcp_server.scraping.campaign_manager_write.current_log",
        return_value=log,
    ):
        track(log)
        result = await _run(mock_page, app, confirm=True)

    assert result["status"] == "control_unavailable"
    assert "tie to the post" in result["message"]
    assert app.clicks == []
    (page,) = log.pages
    assert page["library_rows"] == {
        "answer_posts": 6,
        "rows": 5,
        "row_ids": ["index"] * 5,
        "checked": 0,
        "add": {"disabled": True},
    }


@pytest.mark.parametrize(
    ("ids", "index", "count", "expected"),
    [
        ([_urn_id(OTHER), _urn_id(POST)], 0, 2, 1),
        ([_urn_id(POST), _urn_id(POST)], 0, 2, None),
        ([_urn_id(OTHER)], 1, 2, None),
        ([_index_id(0), _index_id(1)], 1, 2, 1),
        ([_index_id(0)], 1, 2, None),
        ([_index_id(i) for i in range(6)], 3, 6, None),
        ([_index_id(1), _index_id(0)], 1, 2, None),
    ],
    ids=[
        "urn-exact",
        "urn-twice",
        "urn-absent",
        "index-whole-answer",
        "index-subset",
        "index-beyond-one-page",
        "index-out-of-order",
    ],
)
def test_locate_row(ids, index, count, expected):
    rows = [{"id": row_id} for row_id in ids]
    assert locate_row(rows, POST, index, count) == expected
