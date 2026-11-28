"""Browser-DOM test for the connections page's row read (``ROWS_JS``).

The fixture is the measured row structure of
``/mynetwork/invite-connect/connections/`` with every class, image and value
replaced; names, headlines and ids are fake. Requests other than the fixture
are aborted, so nothing reaches LinkedIn. Skipped when chromium is not
installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping.recent_connections_parse import (
    CONNECTIONS_URL,
    ROWS_JS,
    connection_from_row,
)

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

FIXTURE = (
    Path(__file__).parent / "fixtures" / "recent-connections" / "connections-page.html"
)


@pytest.fixture
async def dom_page():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            page = await (await browser.new_context()).new_page()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            yield page
        finally:
            await browser.close()


async def _load(page: Any, body: str) -> None:
    html = f'<!DOCTYPE html><html lang="en"><body>{body}</body></html>'

    async def handle(route: Any) -> None:
        if route.request.url == CONNECTIONS_URL:
            await route.fulfill(content_type="text/html", body=html)
        else:
            await route.abort()

    await page.route("**/*", handle)
    await page.goto(CONNECTIONS_URL)


async def test_each_row_yields_its_name_headline_ids_and_date(dom_page):
    await _load(dom_page, FIXTURE.read_text(encoding="utf-8"))

    rows = [connection_from_row(raw) for raw in await dom_page.evaluate(ROWS_JS)]

    assert [
        (r["name"], r["headline"], r["profileId"], r["connectedOn"]) for r in rows if r
    ] == [
        (
            "Jane Doe",
            "Fake headline one",
            "ACoAAFAKE00000000000000000000000000000001",
            "2026-11-27",
        ),
        (
            "John Roe",
            "Fake headline two",
            "ACoAAFAKE00000000000000000000000000000002",
            "2026-11-26",
        ),
        ("Alex Poe", None, "ACoAAFAKE00000000000000000000000000000003", "2026-11-25"),
    ]


async def test_a_profile_link_without_a_message_link_is_no_row(dom_page):
    # The ad's /in/ link in the fixture is the same case inside a full page.
    await _load(
        dom_page,
        '<main><div><a href="/in/lone-link/"><p>Somebody</p></a>'
        "<p>Connected on November 26, 2026</p></div></main>",
    )

    assert await dom_page.evaluate(ROWS_JS) == []


async def test_the_date_is_the_paragraph_outside_every_link(dom_page):
    # The name and headline paragraphs sit inside the profile link and the
    # button's text inside the button: neither is taken for the date.
    await _load(
        dom_page,
        '<main><div><div><a href="/in/x-fake/"><p>X Fake</p><p>Head</p></a>'
        "<button><p>Remove</p></button><p>Joined 2 days ago</p></div>"
        '<a href="/messaging/compose/?profileUrn=urn%3Ali%3Afsd_profile%3A'
        'ACoAAFAKE00000000000000000000000000000009">Message</a></div></main>',
    )

    (raw,) = await dom_page.evaluate(ROWS_JS)

    assert raw["loose"] == ["Joined 2 days ago"]
    assert raw["label"].split("\n")[0] == "X Fake"
