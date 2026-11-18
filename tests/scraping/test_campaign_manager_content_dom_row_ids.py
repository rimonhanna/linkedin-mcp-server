"""A redrawn table is not clicked (synthetic markup in headless chromium).

The live table keys its rows by URN. If it redraws between finding the post's
row and the click, with as many rows as before but in another order, the
click programs must not check a row or click add.
"""

from __future__ import annotations

import pytest

from linkedin_mcp_server.scraping.campaign_manager_content import click_library

from scraping.test_campaign_manager_content_dom import (
    _actions,
    _clicks,
    _load,
    page,  # noqa: F401  (fixture)
)

pytestmark = [pytest.mark.browser_dom, pytest.mark.xdist_group("browser_runtime")]

POST = "urn:li:share:100000701"
OTHER = "urn:li:share:100000702"


def _urn_rows(*urns: str) -> str:
    cells = "".join(
        f'<tr><td><input type="checkbox" id="select-row-{urn}-checkbox" '
        f"onclick=\"clicked('{urn.rsplit(':', 1)[1]}')\"></td></tr>"
        for urn in urns
    )
    return f"<table><tbody>{cells}</tbody></table>"


def _ids(*urns: str) -> list[str]:
    return [f"select-row-{urn}-checkbox" for urn in urns]


async def test_rows_located_in_one_order_are_not_clicked_in_another(page):  # noqa: F811
    # Located as [POST, OTHER]; the table now shows [OTHER, POST].
    await _load(page, _urn_rows(OTHER, POST) + _actions("en"))
    located = _ids(POST, OTHER)

    assert await click_library(page, "row", 0, located) is False
    assert await click_library(page, "add", 0, located) is False
    assert await _clicks(page) == []


async def test_rows_located_in_the_same_order_are_clicked(page):  # noqa: F811
    await _load(page, _urn_rows(POST, OTHER) + _actions("en"))
    located = _ids(POST, OTHER)

    assert await click_library(page, "row", 0, located) is True
    assert await click_library(page, "add", 0, located) is True
    assert await _clicks(page) == ["100000701", "add"]
