"""Tests for the shared Campaign Manager write flow (synthetic data only).

The flow runs over the fake page the read tests use; the footer program also
runs against synthetic markup in headless chromium (skipped without it).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.scraping import campaign_manager_api as api_module
from linkedin_mcp_server.scraping import campaign_manager_guard as guard_module
from linkedin_mcp_server.scraping import campaign_manager_write as write_module
from linkedin_mcp_server.scraping.campaign_manager_write import CampaignWriteFlow
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

CM = "https://www.linkedin.com/campaign-manager-api/"
ACCOUNT = "100000001"
CAMPAIGN = "100000301"
APP = f"https://www.linkedin.com/campaignmanager/accounts/{ACCOUNT}"
DETAILS = f"{APP}/campaigns/{CAMPAIGN}/details"
SETUP = f"{APP}/campaigns/new/setup?isNewAdvertiser=true"


def _record(name: str = "Example ad set", account: int = int(ACCOUNT)) -> dict:
    return {
        "data": {
            "id": int(CAMPAIGN),
            "accountId": account,
            "name": name,
            "status": "DRAFT",
        },
        "included": [],
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


class _Browser:
    """Script what each page view emits; a redirected URL lands on setup."""

    def __init__(self, page: Any) -> None:
        self.page = page
        self.loads: dict[str, list[list[SimpleNamespace]]] = {}
        self.redirects: set[str] = set()
        self.navigated: list[str] = []

    async def navigate(self, url: str) -> None:
        self.navigated.append(url)
        self.page.url = SETUP if url in self.redirects else url
        waves = self.loads.get(url, [])
        _emit(self.page, waves.pop(0) if waves else [])

    @contextmanager
    def running(self) -> Iterator[None]:
        with (
            patch.object(
                PageNavigator, "_navigate_to_page", AsyncMock(side_effect=self.navigate)
            ),
            patch.object(ScrapingSession, "check_rate_limit", new_callable=AsyncMock),
            patch.object(ScrapingSession, "dismiss_modal", new_callable=AsyncMock),
            patch.object(write_module, "_READY_TIMEOUT", 0.2),
        ):
            yield


def _flow(page: Any) -> CampaignWriteFlow:
    session = ScrapingSession(page)
    return CampaignWriteFlow(session, PageNavigator(session))


async def _read(page: Any, browser: _Browser, flow: CampaignWriteFlow | None = None):
    flow = flow or _flow(page)
    with browser.running():
        async with flow.capturing() as capture:
            return await flow.read_campaign(capture, ACCOUNT, CAMPAIGN)


# --- read_campaign ---------------------------------------------------------


async def test_read_campaign_returns_the_record_and_latches_the_page(mock_page):
    browser = _Browser(mock_page)
    record = _response(f"campaignManagerCampaigns/{CAMPAIGN}", _record())
    browser.loads[DETAILS] = [[record]]

    result = await _read(mock_page, browser)

    assert result == _record()["data"]
    assert browser.navigated == [DETAILS]
    assert guard_module._GUARDS[mock_page].read_mode is True


async def test_read_campaign_refuses_a_setup_redirect_before_any_click(mock_page):
    browser = _Browser(mock_page)
    browser.redirects.add(DETAILS)
    record = _response(f"campaignManagerCampaigns/{CAMPAIGN}", _record())
    browser.loads[DETAILS] = [[record]]
    evaluate = AsyncMock()
    mock_page.evaluate = evaluate

    result = await _read(mock_page, browser)

    assert isinstance(result, tuple)
    assert result[0] == "target_not_found"
    assert "setup page" in result[1]
    evaluate.assert_not_awaited()


async def test_read_campaign_refuses_a_campaign_of_another_account(mock_page):
    browser = _Browser(mock_page)
    record = _response(
        f"campaignManagerCampaigns/{CAMPAIGN}", _record(account=100000002)
    )
    browser.loads[DETAILS] = [[record]]

    result = await _read(mock_page, browser)

    assert isinstance(result, tuple)
    assert result[0] == "target_not_found"
    assert "does not belong" in result[1]


@pytest.mark.parametrize("status", [403, 404])
async def test_read_campaign_refused_id_is_target_not_found_at_once(mock_page, status):
    browser = _Browser(mock_page)
    refused = _response(f"campaignManagerCampaigns/{CAMPAIGN}", None, status=status)
    browser.loads[DETAILS] = [[refused]]

    with patch.object(api_module, "note_throttle_signal") as signal:
        result = await _read(mock_page, browser)

    assert isinstance(result, tuple)
    assert result[0] == "target_not_found"
    assert "campaign_id" in result[1]
    signal.assert_not_called()


async def test_reread_ignores_the_record_from_before_it(mock_page):
    browser = _Browser(mock_page)
    path = f"campaignManagerCampaigns/{CAMPAIGN}"
    browser.loads[DETAILS] = [
        [_response(path, _record("Before"))],
        [_response(path, _record("After"))],
    ]
    flow = _flow(mock_page)

    with browser.running():
        async with flow.capturing() as capture:
            first = await flow.read_campaign(capture, ACCOUNT, CAMPAIGN)
            second = await flow.read_campaign(capture, ACCOUNT, CAMPAIGN)

    assert isinstance(first, dict) and first["name"] == "Before"
    assert isinstance(second, dict) and second["name"] == "After"


async def test_reread_without_a_fresh_record_raises_instead_of_reusing(mock_page):
    browser = _Browser(mock_page)
    path = f"campaignManagerCampaigns/{CAMPAIGN}"
    browser.loads[DETAILS] = [[_response(path, _record())], []]
    flow = _flow(mock_page)

    with browser.running():
        async with flow.capturing() as capture:
            await flow.read_campaign(capture, ACCOUNT, CAMPAIGN)
            with pytest.raises(LinkedInScraperException):
                await flow.read_campaign(capture, ACCOUNT, CAMPAIGN)


# --- apply -------------------------------------------------------------------


def _handler(page: Any) -> Any:
    (handler,) = [
        call.args[1]
        for call in page.route.await_args_list
        if call.args[0] == "**/campaign-manager-api/**"
    ]
    return handler


async def _send(page: Any, path: str) -> str:
    """Pass one app write through the guard; returns abort or fallback."""
    request = _response(
        path, method="POST", body={"patch": {}}, restli="partial_update"
    ).request
    outcome: list[str] = []
    route = SimpleNamespace(
        request=request,
        abort=AsyncMock(side_effect=lambda: outcome.append("abort")),
        fallback=AsyncMock(side_effect=lambda: outcome.append("fallback")),
    )
    await _handler(page)(route)
    return outcome[0]


def _navigated(page: Any, url: str) -> None:
    page.url = url
    page.main_frame = SimpleNamespace(url=url)
    for callback in list(page.listeners.get("framenavigated", [])):
        callback(page.main_frame)


async def test_apply_refuses_every_write_after_the_page_left_its_route(mock_page):
    mock_page.url = DETAILS
    mock_page.main_frame = SimpleNamespace(url=DETAILS)
    flow = _flow(mock_page)
    path = f"campaignManagerCampaigns/{CAMPAIGN}"

    async with flow.apply(mock_page, lambda _req: None) as window:
        assert await _send(mock_page, path) == "fallback"
        _navigated(mock_page, f"{APP}/campaigns/100000399/details")
        assert await _send(mock_page, path) == "abort"

    assert len(window.admitted) == 1
    assert [reason for _req, reason in window.refused] == [
        "the page left the planned route"
    ]
    assert (
        flow.moved_to()
        == f"/campaignmanager/accounts/{ACCOUNT}/campaigns/100000399/details"
    )
    assert mock_page.listeners["framenavigated"] == []
    assert guard_module._GUARDS[mock_page].read_mode is True


async def test_apply_keeps_admitting_on_the_expected_step(mock_page):
    mock_page.url = f"{APP}/campaigns/new/setup?campaignGroupId=100000201"
    flow = _flow(mock_page)
    path = "campaignManagerCampaigns"

    async with flow.apply(
        mock_page,
        lambda _req: None,
        expected_path=lambda p: p.endswith("/details"),
    ) as window:
        _navigated(mock_page, DETAILS)
        assert await _send(mock_page, path) == "fallback"

    assert flow.moved_to() is None
    assert window.refused == []


async def test_apply_unpins_on_an_exception(mock_page):
    mock_page.url = DETAILS
    flow = _flow(mock_page)

    with pytest.raises(RuntimeError):
        async with flow.apply(mock_page, lambda _req: None):
            raise RuntimeError("boom")

    assert mock_page.listeners["framenavigated"] == []
    assert guard_module._GUARDS[mock_page].admission is None


# --- wait_saved ----------------------------------------------------------------


async def test_wait_saved_ignores_answers_before_after(mock_page):
    flow = _flow(mock_page)
    path = f"campaignManagerCampaigns/{CAMPAIGN}"

    def save(name: str) -> SimpleNamespace:
        return _response(
            path,
            None,
            method="POST",
            body={"patch": {"$set": {"name": name}}},
            restli="partial_update",
        )

    def is_save(write: Any) -> bool:
        return write.call.resource == "campaignManagerCampaigns"

    async with flow.capturing() as capture:
        _emit(mock_page, [save("Before")])
        await capture.settle()
        after = capture.write_count
        assert await flow.wait_saved(capture, is_save, after, timeout=0.1) is None
        _emit(mock_page, [save("After")])
        found = await flow.wait_saved(capture, is_save, after, timeout=1.0)

    assert found is not None
    assert found.body == {"patch": {"$set": {"name": "After"}}}
    assert found.restli_method == "partial_update"


# --- save_and_exit -----------------------------------------------------------------


@pytest.mark.parametrize(("draft", "expected"), [(True, 2), (False, 1)])
async def test_save_and_exit_asks_for_the_layout_of_the_campaign_kind(
    mock_page, draft, expected
):
    mock_page.evaluate = AsyncMock(return_value={"count": expected, "clicked": True})

    assert await _flow(mock_page).save_and_exit(mock_page, draft=draft) is True

    call = mock_page.evaluate.await_args
    assert call is not None
    script, arg = call.args
    assert "linkedin-mcp:cm-footer" in script
    assert arg == expected


async def test_save_and_exit_reports_an_unclicked_footer(mock_page):
    mock_page.evaluate = AsyncMock(return_value={"count": 2, "clicked": False})

    assert await _flow(mock_page).save_and_exit(mock_page, draft=False) is False


# --- leave ---------------------------------------------------------------------


async def test_leave_accepts_only_the_leave_page_prompt_and_unregisters(mock_page):
    flow = _flow(mock_page)
    dialogs = [
        SimpleNamespace(type=kind, accept=AsyncMock(), dismiss=AsyncMock())
        for kind in ("beforeunload", "confirm")
    ]

    async def navigate(_url: str) -> None:
        for dialog in dialogs:
            for callback in list(mock_page.listeners.get("dialog", [])):
                await callback(dialog)

    with patch.object(
        PageNavigator, "_navigate_to_page", AsyncMock(side_effect=navigate)
    ):
        await flow.leave(mock_page, DETAILS)

    dialogs[0].accept.assert_awaited_once()
    dialogs[0].dismiss.assert_not_awaited()
    dialogs[1].dismiss.assert_awaited_once()
    dialogs[1].accept.assert_not_awaited()
    assert mock_page.listeners["dialog"] == []


# --- footer program in a real DOM ------------------------------------------------

_LABELS = {
    "en": ("Previous", "Save and exit", "Next"),
    "de": ("Zurück", "Speichern und beenden", "Weiter"),
}


def _footer(labels: tuple[str, str, str], *, previous: bool, extra: str = "") -> str:
    back, save, forward = labels
    return (
        "<div data-js-wizard-container-footer-actions>"
        + (
            f"<button onclick=\"clicked('previous')\">{back}</button>"
            if previous
            else ""
        )
        + '<a href="#">account</a>'
        + extra
        + f'<button onclick="clicked(\'save\')" aria-label="{save}">{save}</button>'
        + f"<button data-live-test-wizard-container__button--next "
        f"onclick=\"clicked('next')\">{forward}</button></div>"
    )


@pytest.fixture
async def dom_page():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            page = await (await browser.new_context()).new_page()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        page.set_default_timeout(1500)
        try:
            yield page
        finally:
            await browser.close()


async def _footer_click(page: Any, body: str, *, draft: bool) -> list[str]:
    await page.set_content(
        # The DOM is shared with the isolated world the flow evaluates in.
        "<script>function clicked(n) { document.body.dataset.clicks = "
        "(document.body.dataset.clicks || '') + n + ' '; }</script>" + body
    )
    flow = CampaignWriteFlow(MagicMock(), MagicMock())
    await flow.save_and_exit(page, draft=draft)
    return (await page.evaluate("document.body.dataset.clicks || ''")).split()


@pytest.mark.browser_dom
@pytest.mark.xdist_group("browser_runtime")
@pytest.mark.parametrize("locale", sorted(_LABELS))
async def test_footer_clicks_the_last_button_before_next(dom_page, locale):
    labels = _LABELS[locale]
    draft = await _footer_click(dom_page, _footer(labels, previous=True), draft=True)
    launched = await _footer_click(
        dom_page, _footer(labels, previous=False), draft=False
    )

    assert (draft, launched) == (["save"], ["save"])


@pytest.mark.browser_dom
@pytest.mark.xdist_group("browser_runtime")
@pytest.mark.parametrize(
    ("body", "draft"),
    [
        (_footer(_LABELS["en"], previous=True), False),
        (_footer(_LABELS["en"], previous=False), True),
        (
            _footer(_LABELS["en"], previous=False, extra="<button>Extra</button>"),
            False,
        ),
        ("<div data-js-wizard-container-footer-actions></div>", False),
        (
            _footer(_LABELS["en"], previous=False) * 2,
            False,
        ),
        ("<p>no footer</p>", False),
        (
            "<div data-js-wizard-container-footer-actions>"
            "<button data-live-test-wizard-container__button--next "
            "onclick=\"clicked('next')\">Next</button>"
            "<button onclick=\"clicked('save')\">Save and exit</button></div>",
            False,
        ),
    ],
    ids=[
        "two-on-launched",
        "one-on-draft",
        "extra",
        "empty",
        "two-footers",
        "none",
        "next-first",
    ],
)
async def test_footer_clicks_nothing_on_an_unexpected_layout(dom_page, body, draft):
    assert await _footer_click(dom_page, body, draft=draft) == []


@pytest.mark.browser_dom
@pytest.mark.xdist_group("browser_runtime")
async def test_footer_never_clicks_a_disabled_or_hidden_save(dom_page):
    disabled = _footer(_LABELS["en"], previous=False).replace(
        'aria-label="Save and exit"', 'aria-label="Save and exit" disabled'
    )
    hidden = _footer(_LABELS["en"], previous=False).replace(
        'aria-label="Save and exit"', 'aria-label="Save and exit" hidden'
    )

    assert await _footer_click(dom_page, disabled, draft=False) == []
    assert await _footer_click(dom_page, hidden, draft=False) == []
