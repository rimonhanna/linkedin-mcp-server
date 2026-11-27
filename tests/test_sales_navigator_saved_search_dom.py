"""Browser-DOM tests for saving a Sales Navigator search through its own controls.

Headless Chromium loads the lead search page's save switch and Saved searches
panel as measured on 2026-11-27 (``fixtures/sales-navigator-saved-search``),
with a script standing in for the app that sends the measured create, panel
read and rename requests. A route answers them as Sales Navigator did, and the
real write guard sits in front of it, so a request the guard refuses never
reaches the answer. Synthetic data only; no LinkedIn request is made.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server import pacing
from linkedin_mcp_server.pacing import SALES_LIST_WRITES, JobStore
from linkedin_mcp_server.scraping import sales_navigator_saved_search as saver_module
from linkedin_mcp_server.scraping.sales_navigator_list_guard import (
    register_sales_list_routes,
)
from linkedin_mcp_server.scraping.sales_navigator_payload import SearchFilter
from linkedin_mcp_server.scraping.sales_navigator_saved_search import (
    SalesNavigatorSearchSaver,
)
from linkedin_mcp_server.scraping.session import ScrapingSession

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

FIXTURES = Path(__file__).parent / "fixtures" / "sales-navigator-saved-search"
REQUESTS = json.loads((FIXTURES / "requests.json").read_text())
HTML = (FIXTURES / "search-page.html").read_text()
SAVED = "100000777"
NAME = "Example UK leads"
SEARCH_URL = "https://www.linkedin.com/sales/search/people?query=(x)"
TITLES = tuple(
    value["text"]
    for value in REQUESTS["lead_create"]["body"]["searchQuery"]["leadSearchQuery"][
        "filters"
    ][0]["values"]
)
SEARCH = [
    ("title", SearchFilter("CURRENT_TITLE", texts=TITLES)),
    ("company_hq_location", SearchFilter("COMPANY_HEADQUARTERS", ids=("101165590",))),
    ("industry", SearchFilter("INDUSTRY", ids=("4", "96"))),
    ("company_size", SearchFilter("COMPANY_HEADCOUNT", ids=("E", "F"))),
]


@dataclass
class _Dom:
    page: Any
    creates: list[Any] = field(default_factory=list)
    renames: list[tuple[str, Any]] = field(default_factory=list)
    reads: int = 0
    store: Any = None
    create_status: int = 201
    elements: list[dict[str, Any]] = field(
        default_factory=lambda: copy.deepcopy(REQUESTS["readback"]["elements"])
    )
    create_body: Any = field(
        default_factory=lambda: copy.deepcopy(REQUESTS["lead_create"]["body"])
    )

    def __post_init__(self) -> None:
        # The search was saved under the app's generated name first.
        self.elements[0]["name"] = self.create_body["name"]


@pytest.fixture
async def dom(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "jobs")
    monkeypatch.setattr(saver_module, "JobStore", lambda *a, **k: store)
    monkeypatch.setenv("WORKING_HOURS_DISABLED", "1")
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                channel="chromium", headless=True
            )
            page = await browser.new_page()
        except Exception as exc:
            if os.environ.get("CI"):
                raise
            pytest.skip(f"chromium unavailable: {exc}")
        page.set_default_timeout(3_000)
        state = _Dom(page)
        state.store = store

        async def fulfill(route):
            request = route.request
            url = request.url
            if "salesApiSavedSearchesV2" in url and request.method == "GET":
                state.reads += 1
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"elements": state.elements}),
                )
            elif url.endswith("/salesApiSavedSearchesV2") and request.method == "POST":
                state.creates.append(json.loads(request.post_data or "null"))
                await route.fulfill(
                    status=state.create_status,
                    headers={"x-restli-id": REQUESTS["lead_create"]["x-restli-id"]},
                    body="",
                )
            elif "/salesApiSavedSearchesV2/(" in url:
                body = json.loads(request.post_data or "null")
                state.renames.append((url, body))
                state.elements[0]["name"] = body["patch"]["$set"]["name"]
                await route.fulfill(status=204, body="")
            else:
                await route.fulfill(
                    status=200, content_type="text/html", body="<html></html>"
                )

        await page.route("https://www.linkedin.com/**", fulfill)
        # As the browser does at page creation.
        await register_sales_list_routes(page)
        try:
            yield state
        finally:
            await browser.close()


class _Navigator:
    def __init__(self, dom: _Dom):
        self._dom = dom

    async def _navigate_to_page(self, url: str) -> None:
        await _load(self._dom)


class _Reader:
    """Stands in for SalesNavigatorReader: the search page, already echoed."""

    def __init__(self, dom: _Dom):
        self._session = ScrapingSession(dom.page)
        self._navigator = _Navigator(dom)
        self._dom = dom
        self.searches = 0

    async def lead_criteria(self, **_criteria):
        return None, list(SEARCH)

    async def _search(self, kind, resource, keywords, filters, page):
        self.searches += 1
        await _load(self._dom)
        return SEARCH_URL, None


async def _load(dom: _Dom) -> None:
    config = {
        "createBody": dom.create_body,
        "readPath": REQUESTS["readback"]["path"].removeprefix(
            "/sales-api/salesApiSavedSearchesV2"
        ),
    }
    await dom.page.goto("https://www.linkedin.com/sales/search/people")
    await dom.page.set_content(
        HTML.replace(
            "<body>",
            f"<body><script>window.__savedSearch = {json.dumps(config)};</script>",
            1,
        )
    )


def _spent(dom: _Dom) -> int:
    now = datetime.now(timezone.utc)
    budget = pacing.load_account_budget(dom.store, now)
    return budget.ledger.spent_kind(SALES_LIST_WRITES, now, 24 * 3600)


async def _save(dom: _Dom, name: str = NAME) -> tuple[dict[str, Any], _Reader]:
    reader = _Reader(dom)
    saver = SalesNavigatorSearchSaver(reader)
    result = await saver.save_search(
        "leads", name, criteria={"title": ["x"]}, confirm_send=True
    )
    return result, reader


async def test_the_switch_saves_the_search_and_the_panel_names_it(dom):
    result, reader = await _save(dom)

    assert result["status"] == "saved", result
    assert result["retry_safe"] is False
    assert result["search"]["id"] == SAVED
    assert dom.creates == [REQUESTS["lead_create"]["body"]]
    assert dom.renames == [
        (
            "https://www.linkedin.com" + REQUESTS["rename"]["path"],
            REQUESTS["rename"]["body"],
        )
    ]
    # The read-back is a fresh panel read after the search page loaded again.
    assert dom.reads == 2
    assert reader.searches == 1
    assert _spent(dom) == 2


async def test_a_create_for_another_search_never_leaves_the_browser(dom):
    dom.create_body["searchQuery"]["leadSearchQuery"]["filters"].pop(1)

    result, _ = await _save(dom)

    assert result["status"] == "not_sent", result
    assert result["retry_safe"] is True
    assert dom.creates == [] and dom.renames == []
    assert _spent(dom) == 0


async def test_a_refused_create_is_not_renamed(dom):
    dom.create_status = 403

    result, _ = await _save(dom)

    assert result["status"] == "refused_by_linkedin", result
    assert len(dom.creates) == 1 and dom.renames == []
    assert _spent(dom) == 1


async def test_a_switch_already_on_sends_nothing(dom):
    reader = _Reader(dom)
    saver = SalesNavigatorSearchSaver(reader)
    original = reader._search

    async def search_then_turn_on(*args):
        found = await original(*args)
        await dom.page.evaluate(
            "document.querySelector('#save-search-st116').checked = true"
        )
        return found

    setattr(reader, "_search", search_then_turn_on)
    result = await saver.save_search(
        "leads", NAME, criteria={"title": ["x"]}, confirm_send=True
    )

    assert result["status"] == "not_sent", result
    assert dom.creates == []


@pytest.mark.parametrize("cap", ["HOURLY_ACTIONS_MAX", "SALES_LIST_WRITES_MAX"])
async def test_a_create_the_rename_cannot_follow_is_not_sent(dom, monkeypatch, cap):
    # Room for the create alone: it would leave the search under the app's name.
    monkeypatch.setenv(cap, "1")

    result, _ = await _save(dom)

    assert result["status"] == "not_sent", result
    assert result["retry_safe"] is True
    assert dom.creates == [] and dom.renames == []
    assert _spent(dom) == 0


async def test_a_create_sent_after_the_send_timeout_never_leaves(dom, monkeypatch):
    monkeypatch.setattr(saver_module, "_SEND_TIMEOUT", 0.2)
    reader = _Reader(dom)
    saver = SalesNavigatorSearchSaver(reader)
    original = reader._search

    async def search_with_a_slow_switch(*args):
        found = await original(*args)
        # The app's create, sent only after the tool stopped waiting for it.
        await dom.page.evaluate(
            """(body) => {
              const input = document.querySelector('#save-search-st116');
              input.parentElement.addEventListener('click', (event) => {
                event.stopImmediatePropagation();
                setTimeout(() => fetch(
                  'https://www.linkedin.com/sales-api/salesApiSavedSearchesV2',
                  {method: 'POST', body: JSON.stringify(body)},
                ).catch(() => null), 600);
              }, true);
            }""",
            dom.create_body,
        )
        return found

    setattr(reader, "_search", search_with_a_slow_switch)
    result = await saver.save_search(
        "leads", NAME, criteria={"title": ["x"]}, confirm_send=True
    )
    await asyncio.sleep(1.0)

    assert result["status"] == "not_sent", result
    assert result["retry_safe"] is True
    assert dom.creates == []
    assert _spent(dom) == 0
