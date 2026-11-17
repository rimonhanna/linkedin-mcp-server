"""Campaign Manager app-write guard."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.scraping import campaign_manager_guard as guard

API = "https://www.linkedin.com/campaign-manager-api/"


class FakePage:
    def __init__(self, hang: bool = False, fail: bool = False) -> None:
        self.routes: list[tuple[str, Any]] = []
        self.hang = hang
        self.fail = fail

    async def route(self, pattern: str, handler: Any) -> None:
        if self.hang:
            await asyncio.Event().wait()
        if self.fail:
            raise RuntimeError("no route")
        self.routes.append((pattern, handler))


class FakeRoute:
    def __init__(self, method: str, url: str, headers: dict[str, str] | None = None):
        headers = headers or {}
        self.outcome = ""

        async def all_headers() -> dict[str, str]:
            return {k.lower(): v for k, v in headers.items()}

        # Provisional headers: the sync view lacks the tunnel header.
        self.request = SimpleNamespace(
            method=method, url=url, headers={}, all_headers=all_headers
        )

    async def abort(self) -> None:
        self.outcome = "abort"

    async def fallback(self) -> None:
        self.outcome = "fallback"


async def _outcome(page: FakePage, method: str, url: str, headers=None) -> str:
    route = FakeRoute(method, url, headers)
    await page.routes[0][1](route)
    return route.outcome


async def _registered() -> FakePage:
    page = FakePage()
    await guard.register_campaign_manager_routes(page)
    return page


async def test_registers_one_route_once():
    page = await _registered()
    await guard.register_campaign_manager_routes(page)
    assert [p for p, _ in page.routes] == ["**/campaign-manager-api/**"]


async def test_inert_before_latch():
    page = await _registered()
    url = API + "billingConfiguration?action=findOrCreate"
    assert await _outcome(page, "POST", url) == "fallback"


async def test_latched_passes_reads_and_aborts_writes():
    page = await _registered()
    async with guard.without_campaign_manager_writes(page):
        assert await _outcome(page, "GET", API + "adAccounts") == "fallback"
        tunneled = {"X-HTTP-Method-Override": "GET"}
        assert await _outcome(page, "POST", API + "q", tunneled) == "fallback"
        create = API + "billingConfiguration?action=findOrCreate"
        assert await _outcome(page, "POST", create) == "abort"
        assert (
            await _outcome(page, "POST", API + "q", {"x-http-method-override": "PUT"})
            == "abort"
        )
        assert await _outcome(page, "PUT", API + "campaigns/1") == "abort"
        assert await _outcome(page, "DELETE", API + "campaigns/1") == "abort"
    # Latched for late requests after the read returns.
    assert await _outcome(page, "POST", API + "x") == "abort"


async def test_registration_timeout_refuses(monkeypatch):
    monkeypatch.setattr(guard, "_REGISTRATION_TIMEOUT_SECONDS", 0.01)
    page = FakePage(hang=True)
    with pytest.raises(LinkedInScraperException, match="did not register"):
        async with guard.without_campaign_manager_writes(page):
            pytest.fail("a read must not run unguarded")


async def test_registration_failure_latches_refusal():
    page = FakePage(fail=True)
    with pytest.raises(RuntimeError):
        await guard.register_campaign_manager_routes(page)
    await asyncio.sleep(0)
    with pytest.raises(LinkedInScraperException, match="close_session"):
        async with guard.without_campaign_manager_writes(page):
            pytest.fail("a read must not run unguarded")
