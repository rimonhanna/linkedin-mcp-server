"""Tests for the passive Sales Navigator response capture (synthetic data)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import logging

from linkedin_mcp_server.scraping.sales_navigator_api import SalesNavigatorCapture

SN = "https://www.linkedin.com/sales-api/"


def _response(
    path: str, payload: Any = None, *, method: str = "GET", status: int = 200
) -> SimpleNamespace:
    request = SimpleNamespace(method=method, url=SN + path)
    return SimpleNamespace(
        url=SN + path,
        status=status,
        request=request,
        json=AsyncMock(return_value=payload),
    )


def _emit(page: Any, response: SimpleNamespace) -> None:
    for callback in list(page.listeners.get("request", [])):
        callback(response.request)
    for callback in list(page.listeners.get("response", [])):
        callback(response)


async def test_reads_the_body_of_a_get_for_a_listed_resource(mock_page):
    response = _response("salesApiLists?q=listType&listType=ACCOUNT", {"elements": []})

    async with SalesNavigatorCapture(
        mock_page, resources=frozenset({"salesApiLists"})
    ) as capture:
        _emit(mock_page, response)
        await capture.settle()

    (captured,) = capture.responses
    assert captured.payload == {"elements": []}
    assert captured.call.resource == "salesApiLists"
    assert captured.call.finder == "listType"
    response.json.assert_awaited_once()


async def test_keeps_other_calls_with_their_status_and_never_reads_them(mock_page):
    boot = _response("salesApiEntitlements", {"secret": 1}, status=403)
    post = _response("salesApiWebAppBoot?action=recordBoot", {}, method="POST")
    listed_post = _response("salesApiLists?action=x", {}, method="POST")

    async with SalesNavigatorCapture(
        mock_page, resources=frozenset({"salesApiLists"})
    ) as capture:
        for response in (boot, post, listed_post):
            _emit(mock_page, response)
        await capture.settle()

    assert [(r.call.resource, r.status, r.payload) for r in capture.responses] == [
        ("salesApiEntitlements", 403, None),
        ("salesApiWebAppBoot", 200, None),
        ("salesApiLists", 200, None),
    ]
    for response in (boot, post, listed_post):
        response.json.assert_not_awaited()


async def test_ignores_responses_outside_the_sales_api(mock_page):
    other = SimpleNamespace(
        url="https://www.linkedin.com/voyager/api/me",
        status=200,
        request=SimpleNamespace(
            method="GET", url="https://www.linkedin.com/voyager/api/me"
        ),
        json=AsyncMock(return_value={}),
    )

    async with SalesNavigatorCapture(mock_page, resources=frozenset()) as capture:
        _emit(mock_page, other)
        await capture.settle()

    assert capture.responses == []
    assert mock_page.listeners["response"] == []
    assert mock_page.listeners["request"] == []


async def test_logs_a_non_get_call_by_resource_and_action_only(mock_page, caplog):
    post = _response(
        "salesApiCoach?action=suggest&query=(keywords:private%20words)",
        method="POST",
    )
    get = _response("salesApiLeadSearch?q=searchQuery&query=(keywords:hidden)")

    with caplog.at_level(logging.DEBUG, "linkedin_mcp_server.scraping"):
        async with SalesNavigatorCapture(mock_page, resources=frozenset()) as capture:
            _emit(mock_page, post)
            _emit(mock_page, get)
            await capture.settle()

    lines = [
        record.getMessage()
        for record in caplog.records
        if record.name.endswith("sales_navigator_api")
    ]
    assert lines == ["Sales Navigator page sent POST salesApiCoach action=suggest"]
    assert "private" not in caplog.text and "hidden" not in caplog.text
