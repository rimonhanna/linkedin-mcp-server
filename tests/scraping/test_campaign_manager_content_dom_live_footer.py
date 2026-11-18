"""The live library layout that stopped add_post_to_campaign (synthetic markup).

Live 2026-11-18: one index-keyed row, one post answered, and the add button
(the footer's sponsor button) a sibling of the "Remove from library" wrapper,
outside the trigger's parent. The add button was not found (``add: null``), so
a provable row still stopped.
"""

from __future__ import annotations

import pytest

from linkedin_mcp_server.scraping.campaign_manager_content import (
    library_rows,
    locate_row,
)

from scraping.test_campaign_manager_content_dom import (
    _actions,
    _add,
    _load,
    _pills,
    _rows,
    page,  # noqa: F401  (fixture)
)

pytestmark = [pytest.mark.browser_dom, pytest.mark.xdist_group("browser_runtime")]


@pytest.mark.parametrize("wrapped", [True, False], ids=["wrapper", "on-button"])
async def test_one_index_row_and_the_sibling_sponsor_button_are_found(
    page,  # noqa: F811
    wrapped,
):
    await _load(page, _pills("en") + _rows(1) + _actions("en", wrapped=wrapped))

    state = await library_rows(page) or {}

    assert state["add"] == {"disabled": True}
    assert locate_row(state["rows"], "urn:li:share:100000701", 0, 1) == 0
    assert await _add(page, 0, 1) == ["row-0", "add"]
