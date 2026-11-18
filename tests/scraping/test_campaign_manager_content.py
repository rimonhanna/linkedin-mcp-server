"""Tests for add_post_to_campaign over a fake page (synthetic data only).

The page programs are answered from a scripted library; the app's creative
create is passed through the real write guard, so admission decides it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from linkedin_mcp_server.scraping import campaign_manager_content as content_module
from linkedin_mcp_server.scraping import campaign_manager_guard as guard_module
from linkedin_mcp_server.scraping import campaign_manager_write as write_module
from linkedin_mcp_server.scraping.campaign_manager_content import (
    CampaignContentEditor,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

CM = "https://www.linkedin.com/campaign-manager-api/"
ACCOUNT = "100000001"
CAMPAIGN = "100000301"
CREATIVE = "100000501"
POST = "urn:li:share:100000701"
OTHER = "urn:li:share:100000702"
APP = f"https://www.linkedin.com/campaignmanager/accounts/{ACCOUNT}"
DETAILS = f"{APP}/campaigns/{CAMPAIGN}/details"
LIBRARY = (
    f"{APP}/campaigns/{CAMPAIGN}/creatives/sponsorable-contents?businessId=personal"
)
EDITOR = f"{APP}/campaigns/{CAMPAIGN}/creatives/new"
LIBRARY_PATH = "campaignManagerContentLibraryEntities?q=organicCompanyContent"
CREATIVES_PATH = f"campaignManagerCreatives?q=campaignIds&campaignIds=List({CAMPAIGN})"


def _record(status: str = "DRAFT") -> dict:
    return {
        "data": {
            "id": int(CAMPAIGN),
            "accountId": int(ACCOUNT),
            "name": "Example ad set",
            "status": status,
        },
        "included": [],
    }


def _library(
    posts: list[tuple[str, bool, list[str]]], *, count: int = 100
) -> tuple[str, dict]:
    elements = [{"content": {"*sponsoredUpdateContent": urn}} for urn, _, _ in posts]
    included = [
        {
            "entityUrn": urn,
            "sponsorable": sponsorable,
            "nonSponsorableReasons": reasons,
            "visibility": "ORGANIC",
            "userGeneratedAdContent": {"contentType": "TEXT"},
        }
        for urn, sponsorable, reasons in posts
    ]
    path = f"{LIBRARY_PATH}&count={count}&start=0"
    return path, {"data": {"elements": elements}, "included": included}


def _creatives(reference: str = POST, creative: str = CREATIVE) -> dict:
    urn = f"urn:li:sponsoredCreative:{creative}"
    return {
        "data": {"elements": [urn]},
        "included": [
            {
                "entityUrn": urn,
                "id": urn,
                "campaign": f"urn:li:sponsoredCampaign:{CAMPAIGN}",
                "reference": reference,
                "status": "ACTIVE",
            }
        ],
    }


def _response(
    path: str,
    payload: Any = None,
    *,
    status: int = 200,
    method: str = "GET",
    body: Any = None,
    restli: str | None = None,
    created_id: str | None = None,
) -> SimpleNamespace:
    headers = {"x-restli-method": restli} if restli else {}
    request = SimpleNamespace(
        method=method,
        url=CM + path,
        headers={},
        post_data=json.dumps(body) if body is not None else None,
        all_headers=AsyncMock(return_value=headers),
    )
    answer = {"x-restli-id": created_id} if created_id else {}
    return SimpleNamespace(
        url=CM + path,
        status=status,
        request=request,
        json=AsyncMock(return_value=payload),
        all_headers=AsyncMock(return_value=answer),
    )


def _emit(page: Any, responses: list[SimpleNamespace]) -> None:
    for response in responses:
        for callback in list(page.listeners.get("response", [])):
            callback(response)


def _handler(page: Any) -> Any:
    (handler,) = {
        call.args[1]
        for call in page.route.await_args_list
        if call.args[0] == "**/campaign-manager-api/**"
    }
    return handler


class _App:
    """Script the pages, the library table and the app's answer to a click."""

    def __init__(self, page: Any) -> None:
        self.page = page
        self.status = "DRAFT"
        self.posts: list[tuple[str, bool, list[str]]] = [
            (OTHER, True, []),
            (POST, True, []),
        ]
        self.count = 100
        self.disabled_rows: set[int] = set()
        self.rows: int | None = None
        self.pill = {"found": True, "checked": True, "clicked": False}
        self.creatives = _creatives()
        self.clicks: list[tuple[str, int]] = []
        self.navigated: list[str] = []
        # What the app does when the add button is clicked.
        self.on_add: Callable[[], Any] = self.create
        self.create_body: dict = {
            "campaignId": int(CAMPAIGN),
            "reference": POST,
            "status": "ACTIVE",
            "type": "SPONSORED_STATUS_UPDATE",
            "name": "",
        }
        self.create_status = 200
        self.sent: list[str] = []
        page.evaluate = AsyncMock(side_effect=self.evaluate)

    async def navigate(self, url: str) -> None:
        self.navigated.append(url)
        self.page.url = url
        if url == DETAILS:
            _emit(
                self.page,
                [
                    _response(
                        f"campaignManagerCampaigns/{CAMPAIGN}", _record(self.status)
                    ),
                    _response(CREATIVES_PATH, self.creatives),
                ],
            )

    def _state(self, checked: int | None = None) -> dict:
        count = len(self.posts) if self.rows is None else self.rows
        return {
            "rows": [
                {
                    "id": f"select-row-{i}-checkbox",
                    "disabled": i in self.disabled_rows,
                    "checked": i == checked,
                }
                for i in range(count)
            ],
            "add": {"disabled": checked is None},
        }

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if "linkedin-mcp:cm-route" in script:
            self.navigated.append(arg)
            self.page.url = "https://www.linkedin.com" + arg
            path, payload = _library(self.posts, count=self.count)
            _emit(self.page, [_response(path, payload)])
            return True
        if "linkedin-mcp:cm-content-pill" in script:
            return self.pill
        if "linkedin-mcp:cm-content-rows" in script:
            return self._state()
        if "linkedin-mcp:cm-content-click" in script:
            step, index, _count = arg
            self.clicks.append((step, index))
            if step == "add":
                await self.on_add()
            return {"clicked": True, "state": self._state(index)}
        raise AssertionError(f"unexpected script: {script[:60]}")

    async def create(self) -> None:
        await self.send("campaignManagerCreatives", self.create_body, "create")

    async def send(self, path: str, body: dict, restli: str) -> None:
        """The app's write, through the guard; answered only if it passes."""
        response = _response(
            path,
            {},
            method="POST",
            body=body,
            restli=restli,
            status=self.create_status,
            created_id=f"urn:li:sponsoredCreative:{CREATIVE}",
        )
        outcome: list[str] = []
        route = SimpleNamespace(
            request=response.request,
            abort=AsyncMock(side_effect=lambda: outcome.append("abort")),
            fallback=AsyncMock(side_effect=lambda: outcome.append("fallback")),
        )
        await _handler(self.page)(route)
        self.sent.append(outcome[0])
        if outcome[0] == "fallback":
            _emit(self.page, [response])

    @contextmanager
    def running(self) -> Iterator[None]:
        with (
            patch.object(
                PageNavigator, "_navigate_to_page", AsyncMock(side_effect=self.navigate)
            ),
            patch.object(PageNavigator, "_charge_navigation"),
            patch.object(ScrapingSession, "check_rate_limit", new_callable=AsyncMock),
            patch.object(ScrapingSession, "dismiss_modal", new_callable=AsyncMock),
            patch.object(write_module, "_READY_TIMEOUT", 0.3),
            patch.object(content_module, "_READY_TIMEOUT", 0.3),
            patch.object(content_module, "_SAVE_TIMEOUT", 0.3),
            patch.object(content_module, "_CREATIVES_WAIT", 0.3),
            patch.object(content_module, "_ROWS_POLLS", 2),
            patch.object(content_module, "_ROWS_POLL_SECONDS", 0),
        ):
            yield


async def _run(page: Any, app: _App, *, confirm: bool = True) -> dict:
    session = ScrapingSession(page)
    editor = CampaignContentEditor(session, PageNavigator(session))
    with app.running():
        return await editor.add_post_to_campaign(
            ACCOUNT, CAMPAIGN, POST, confirm_send=confirm
        )


@pytest.fixture
def app(mock_page) -> _App:
    mock_page.url = DETAILS
    mock_page.main_frame = SimpleNamespace(url=DETAILS)
    return _App(mock_page)


# --- refused before the library ----------------------------------------------


async def test_an_active_ad_set_is_refused_before_the_library_opens(mock_page, app):
    app.status = "ACTIVE"

    result = await _run(mock_page, app)

    assert result["status"] == "campaign_active"
    assert result["retry_safe"] is True
    assert result["target"]["status"] == "ACTIVE"
    assert app.navigated == [DETAILS]
    assert app.clicks == []


@pytest.mark.parametrize("status", ["COMPLETED", "ARCHIVED", "CANCELED"])
async def test_a_finished_ad_set_is_not_editable(mock_page, app, status):
    app.status = status

    result = await _run(mock_page, app)

    assert result["status"] == "not_editable"
    assert app.clicks == []


@pytest.mark.parametrize("status", ["DRAFT", "PAUSED"])
async def test_dry_run_verifies_and_clicks_nothing(mock_page, app, status):
    app.status = status

    result = await _run(mock_page, app, confirm=False)

    assert result["status"] == "confirmation_required"
    assert result["retry_safe"] is True
    assert result["url"] == LIBRARY
    assert result["planned"] == {
        "postUrn": POST,
        "visibility": "ORGANIC",
        "contentType": "TEXT",
    }
    assert result["target"] == {
        "kind": "campaign",
        "uiLabel": "Ad set",
        "id": int(CAMPAIGN),
        "accountId": int(ACCOUNT),
        "name": "Example ad set",
        "status": status,
    }
    assert app.clicks == []
    assert guard_module._GUARDS[mock_page].admission is None
    assert guard_module._GUARDS[mock_page].read_mode is True


# --- finding the post ------------------------------------------------------------


async def test_a_post_missing_from_a_short_library_is_not_found(mock_page, app):
    app.posts = [(OTHER, True, [])]

    result = await _run(mock_page, app)

    assert result["status"] == "post_not_found"
    assert "most recent" not in result["message"]
    assert app.clicks == []


async def test_a_post_beyond_the_first_page_says_how_far_it_looked(mock_page, app):
    app.posts = [(f"urn:li:share:1000008{i:02d}", True, []) for i in range(3)]
    app.count = 3

    result = await _run(mock_page, app)

    assert result["status"] == "post_not_found"
    assert "3 most recent" in result["message"]


async def test_a_post_the_server_refuses_carries_its_reasons(mock_page, app):
    app.posts = [(POST, False, ["EXAMPLE_REASON_ONE", "EXAMPLE_REASON_TWO"])]

    result = await _run(mock_page, app)

    assert result["status"] == "post_not_sponsorable"
    assert result["planned"]["reasons"] == ["EXAMPLE_REASON_ONE", "EXAMPLE_REASON_TWO"]
    assert app.clicks == []


async def test_a_row_the_app_disables_is_not_sponsorable_without_reasons(
    mock_page, app
):
    app.disabled_rows = {1}

    result = await _run(mock_page, app)

    assert result["status"] == "post_not_sponsorable"
    assert result["planned"]["reasons"] == []
    assert "objective or format" in result["message"]
    assert app.clicks == []


async def test_rows_that_do_not_match_the_answer_stop_before_any_click(mock_page, app):
    app.rows = 1

    result = await _run(mock_page, app)

    assert result["status"] == "control_unavailable"
    assert app.clicks == []


async def test_a_filter_that_cannot_be_selected_stops(mock_page, app):
    app.pill = {"found": True, "checked": False, "clicked": False}

    result = await _run(mock_page, app)

    assert result["status"] == "control_unavailable"
    assert app.clicks == []


# --- adding --------------------------------------------------------------------------


async def test_adding_clicks_the_posts_row_then_add_and_verifies(mock_page, app):
    result = await _run(mock_page, app)

    assert result["status"] == "added"
    assert result["retry_safe"] is False
    assert result["created_id"] == CREATIVE
    assert result["applied"] == {
        "creativeId": CREATIVE,
        "postUrn": POST,
        "status": "ACTIVE",
    }
    assert app.clicks == [("row", 1), ("add", 1)]
    assert app.sent == ["fallback"]
    assert app.navigated[-1] == DETAILS
    assert guard_module._GUARDS[mock_page].admission is None
    assert guard_module._GUARDS[mock_page].read_mode is True


async def test_a_create_for_another_post_is_blocked(mock_page, app):
    app.create_body = {**app.create_body, "reference": OTHER}

    result = await _run(mock_page, app)

    assert result["status"] == "change_blocked"
    assert result["retry_safe"] is True
    assert result["refused"] == [
        {
            "method": "POST",
            "resource": "campaignManagerCreatives",
            "reason": "creative create for another post",
        }
    ]
    assert app.sent == ["abort"]


async def test_any_other_write_on_add_is_blocked(mock_page, app):
    async def remove() -> None:
        await app.send("campaignManagerContentLibraryEntities", {"urn": POST}, "action")

    app.on_add = remove

    result = await _run(mock_page, app)

    assert result["status"] == "change_blocked"
    assert app.sent == ["abort"]


async def test_only_one_creative_is_admitted(mock_page, app):
    async def twice() -> None:
        await app.create()
        await app.create()

    app.on_add = twice

    result = await _run(mock_page, app)

    assert app.sent == ["fallback", "abort"]
    assert result["status"] == "added"
    assert [entry["reason"] for entry in result["refused"]] == [
        "second creative create"
    ]


async def test_the_app_opening_its_ad_editor_needs_more_details(mock_page, app):
    async def open_editor() -> None:
        mock_page.url = EDITOR
        mock_page.main_frame = SimpleNamespace(url=EDITOR)
        for callback in list(mock_page.listeners.get("framenavigated", [])):
            callback(mock_page.main_frame)

    app.on_add = open_editor

    result = await _run(mock_page, app)

    assert result["status"] == "needs_more_details"
    assert result["retry_safe"] is True


async def test_no_request_and_no_move_is_control_unavailable(mock_page, app):
    app.on_add = AsyncMock()

    result = await _run(mock_page, app)

    assert result["status"] == "control_unavailable"
    assert result["retry_safe"] is True


async def test_a_refused_create_is_save_failed(mock_page, app):
    app.create_status = 422

    result = await _run(mock_page, app)

    assert result["status"] == "save_failed"
    assert result["retry_safe"] is True


async def test_a_new_ad_missing_on_reread_is_unverified(mock_page, app):
    app.creatives = _creatives(creative="100000599")

    result = await _run(mock_page, app)

    assert result["status"] == "save_unverified"
    assert result["retry_safe"] is False
    assert result["created_id"] == CREATIVE


async def test_a_new_ad_with_another_reference_is_unverified(mock_page, app):
    app.creatives = _creatives(reference=OTHER)

    result = await _run(mock_page, app)

    assert result["status"] == "save_unverified"


async def test_a_library_that_reloaded_with_the_post_moved_stops(mock_page, app):
    original = app.evaluate

    async def evaluate(script: str, arg: Any = None) -> Any:
        if "linkedin-mcp:cm-content-rows" in script:
            state = await original(script, arg)
            path, payload = _library([(POST, True, []), (OTHER, True, [])])
            _emit(mock_page, [_response(path, payload)])
            return state
        return await original(script, arg)

    mock_page.evaluate = AsyncMock(side_effect=evaluate)

    result = await _run(mock_page, app)

    assert result["status"] == "control_unavailable"
    assert app.clicks == []


async def test_an_invalid_post_urn_raises_before_the_browser(mock_page, app):
    session = ScrapingSession(mock_page)
    editor = CampaignContentEditor(session, PageNavigator(session))

    with app.running(), pytest.raises(Exception, match="post_urn"):
        await editor.add_post_to_campaign(
            ACCOUNT, CAMPAIGN, "urn:li:article:1", confirm_send=True
        )

    assert app.navigated == []
