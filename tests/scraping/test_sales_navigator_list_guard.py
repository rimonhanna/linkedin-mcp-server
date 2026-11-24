"""The guard that lets an account-list write send only its one planned request."""

from __future__ import annotations

import json
from typing import Any

import pytest

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.scraping import sales_navigator_list_guard as guard
from linkedin_mcp_server.scraping.sales_navigator_list_payload import (
    add_request,
    delete_request,
)

API = "https://www.linkedin.com/sales-api/"
LIST = "100000301"
PLANNED = add_request(LIST, ["100000001"])
DELETE = delete_request(LIST)
DELETE_PATH = f"salesApiLists/{LIST}?unsaveEntity=false&doUnsaveLeadsUnderAccount=false"


class _Request:
    def __init__(
        self,
        path: str,
        body: Any = None,
        *,
        method: str = "POST",
        headers: dict[str, str] | None = None,
    ):
        self.method = method
        self.url = API + path
        self.post_data = None if body is None else json.dumps(body)
        self._headers = headers or {}

    async def all_headers(self) -> dict[str, str]:
        return self._headers


class _Route:
    def __init__(self, request: _Request):
        self.request = request
        self.outcome: str | None = None

    async def fallback(self) -> None:
        self.outcome = "sent"

    async def abort(self) -> None:
        self.outcome = "aborted"


class _Page:
    def __init__(self) -> None:
        self.routes: list[tuple[str, Any]] = []

    async def route(self, pattern: str, handler: Any) -> None:
        self.routes.append((pattern, handler))


def _add(body: Any = None, *, path: str = "salesApiCompanies?action=save"):
    return _Request(path, PLANNED.body if body is None else body)


async def _send(page: _Page, request: _Request) -> str | None:
    (_pattern, handler), *_ = page.routes
    route = _Route(request)
    await handler(route)
    return route.outcome


async def test_outside_the_tools_the_route_lets_everything_through():
    page = _Page()
    await guard.register_sales_list_routes(page)
    assert page.routes[0][0] == "**/sales-api/**"
    assert await _send(page, _add()) == "sent"


async def test_while_a_tool_holds_the_page_every_write_is_aborted():
    page = _Page()
    async with guard.without_sales_list_writes(page):
        assert await _send(page, _add()) == "aborted"
        delete = _Request(f"salesApiLists/{LIST}", method="DELETE")
        assert await _send(page, delete) == "aborted"
        get = _Request(f"salesApiLists/{LIST}", method="GET")
        assert await _send(page, get) == "sent"
    assert await _send(page, _add()) == "sent"


async def test_a_post_tunnelling_a_delete_is_a_write():
    page = _Page()
    tunnel = _Request(DELETE_PATH, headers={"X-HTTP-Method-Override": "DELETE"})
    async with guard.without_sales_list_writes(page):
        assert await _send(page, tunnel) == "aborted"


async def test_a_post_tunnelling_a_get_is_a_read():
    page = _Page()
    read = _Request(
        f"salesApiLists/{LIST}", {}, headers={"x-http-method-override": "GET"}
    )
    async with guard.without_sales_list_writes(page):
        assert await _send(page, read) == "sent"


async def test_the_boot_record_passes_only_outside_a_window():
    page = _Page()
    boot = _Request("salesApiWebAppBoot?action=recordBoot", {})
    async with guard.without_sales_list_writes(page):
        assert await _send(page, boot) == "sent"
        other = _Request("salesApiWebAppBoot?action=somethingElse", {})
        assert await _send(page, other) == "aborted"
        async with guard.explicit_list_write(page, PLANNED) as window:
            assert await _send(page, boot) == "aborted"
    assert window.admitted == 0


@pytest.mark.parametrize(
    ("request_", "reason"),
    [
        (
            _add({"companyIds": [100000001], "lists": ["100000302"]}),
            "body differs from the plan",
        ),
        (
            _add({"companyIds": [100000001, 100000002], "lists": [LIST]}),
            "body differs from the plan",
        ),
        (_add(path="salesApiCompanies?action=unsave"), "query differs from the plan"),
        (
            _add(path="salesApiCompanies?action=save&action=unsave"),
            "query differs from the plan",
        ),
        (_add(path="salesApiCompanies"), "query differs from the plan"),
        (
            _Request("salesApiCompanies?action=save", method="DELETE"),
            "method DELETE is not the planned POST",
        ),
        (
            _add(path="salesApiLists?action=save"),
            "not the planned resource",
        ),
    ],
)
async def test_the_window_aborts_anything_but_the_exact_plan(request_, reason):
    page = _Page()
    async with guard.without_sales_list_writes(page):
        async with guard.explicit_list_write(page, PLANNED) as window:
            assert await _send(page, request_) == "aborted"
    assert window.admitted == 0
    assert window.refused == [reason]


async def test_the_window_admits_the_plan_once():
    page = _Page()
    async with guard.without_sales_list_writes(page):
        async with guard.explicit_list_write(page, PLANNED) as window:
            assert await _send(page, _add()) == "sent"
            assert await _send(page, _add()) == "aborted"
        assert await _send(page, _add()) == "aborted"
    assert window.admitted == 1
    assert window.refused == ["a write was already sent"]


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        (DELETE_PATH.replace(LIST, "100000302"), "not the planned list"),
        (
            DELETE_PATH.replace("unsaveEntity=false", "unsaveEntity=true"),
            "query differs from the plan",
        ),
        (f"salesApiLists/{LIST}", "query differs from the plan"),
    ],
)
async def test_a_delete_window_admits_only_that_list_keeping_its_accounts(path, reason):
    page = _Page()
    async with guard.without_sales_list_writes(page):
        async with guard.explicit_list_write(page, DELETE) as window:
            assert await _send(page, _Request(path, method="DELETE")) == "aborted"
            assert await _send(page, _Request(DELETE_PATH, method="DELETE")) == "sent"
    assert window.refused == [reason]
    assert window.admitted == 1


async def test_a_window_needs_the_page_held():
    page = _Page()
    with pytest.raises(LinkedInScraperException, match="outside its protection"):
        async with guard.explicit_list_write(page, PLANNED):
            pass


async def test_a_failed_registration_refuses_every_later_hold():
    class _Failing(_Page):
        async def route(self, pattern: str, handler: Any) -> None:
            raise RuntimeError("route failed")

    page = _Failing()
    with pytest.raises(RuntimeError):
        await guard.register_sales_list_routes(page)
    with pytest.raises(LinkedInScraperException, match="registration failed"):
        async with guard.without_sales_list_writes(page):
            pass
