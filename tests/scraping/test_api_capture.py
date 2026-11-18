"""Tests for the shared passive API response capture (synthetic data)."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import asyncio

import pytest

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.scraping import api_capture as capture_module
from linkedin_mcp_server.scraping.api_capture import (
    ApiResponseCapture,
    CapturedResponse,
)

API = "https://www.linkedin.com/example-api/"


@dataclass(frozen=True, slots=True)
class _Call:
    resource: str
    method: str


class _Capture(ApiResponseCapture[_Call]):
    label = "Example app"

    def _parse(self, response: Any) -> _Call | None:
        if not response.url.startswith(API):
            return None
        return _Call(response.url[len(API) :], response.request.method)

    def _wants_body(self, call: _Call) -> bool:
        return call.method == "GET"


def _response(
    resource: str, payload: Any = None, *, method: str = "GET", status: int = 200
) -> SimpleNamespace:
    return SimpleNamespace(
        url=API + resource,
        status=status,
        request=SimpleNamespace(method=method, url=API + resource),
        json=AsyncMock(return_value=payload),
    )


def _emit(page: Any, *responses: SimpleNamespace) -> None:
    for response in responses:
        for callback in list(page.listeners.get("response", [])):
            callback(response)


async def test_a_parsed_response_is_kept_and_others_ignored(mock_page):
    async with _Capture(mock_page) as capture:
        _emit(
            mock_page,
            SimpleNamespace(
                url="https://www.linkedin.com/elsewhere",
                status=200,
                request=SimpleNamespace(method="GET"),
                json=AsyncMock(),
            ),
            _response("exampleItems", {"elements": []}),
        )
        found = await capture.wait_for(lambda r: True, timeout=1)

    assert found == CapturedResponse(
        _Call("exampleItems", "GET"), 200, {"elements": []}
    )
    assert found.ok
    assert capture.seen_resources == {"exampleItems"}


async def test_429_notes_a_throttle_and_keeps_nothing(mock_page):
    with patch.object(capture_module, "note_throttle_signal") as signal:
        async with _Capture(mock_page) as capture:
            _emit(mock_page, _response("exampleItems", status=429))
            await capture.settle()
    signal.assert_called_once_with("http_429")
    assert capture.responses == []
    assert capture.seen_resources == {"exampleItems"}


async def test_a_body_not_wanted_is_never_read(mock_page):
    write = _response("exampleItems", {"secret": 1}, method="POST")
    async with _Capture(mock_page) as capture:
        _emit(mock_page, write)
        await capture.settle()
    write.json.assert_not_awaited()
    assert capture.responses == [
        CapturedResponse(_Call("exampleItems", "POST"), 200, None)
    ]


async def test_refine_returning_none_keeps_the_response_out(mock_page):
    class _Dropping(_Capture):
        async def _refine(self, response: Any, call: _Call) -> _Call | None:
            return None

    async with _Dropping(mock_page) as capture:
        _emit(mock_page, _response("exampleItems", {}))
        await capture.settle()
    assert capture.responses == []
    assert capture.seen_resources == {"exampleItems"}


async def test_wait_for_timeout_names_the_app_and_notes_half_a_throttle(mock_page):
    with patch.object(capture_module, "note_throttle_signal") as signal:
        async with _Capture(mock_page) as capture:
            _emit(mock_page, _response("exampleBoot", {}))
            with pytest.raises(
                LinkedInScraperException,
                match=r"Example app did not return the expected data in time "
                r"\(responses seen: exampleBoot\)",
            ):
                await capture.wait_for(
                    lambda r: r.call.resource == "exampleItems", timeout=0.05
                )
    signal.assert_called_once_with("payload_timeout", half=True)


async def test_wait_for_without_throttle_notes_nothing(mock_page):
    with patch.object(capture_module, "note_throttle_signal") as signal:
        async with _Capture(mock_page) as capture:
            with pytest.raises(LinkedInScraperException, match="responses seen: none"):
                await capture.wait_for(lambda r: True, timeout=0.05, throttle=False)
    signal.assert_not_called()


async def test_matching_starts_after_the_given_index(mock_page):
    async with _Capture(mock_page) as capture:
        _emit(mock_page, _response("a", 1), _response("b", 2), _response("a", 3))
        await capture.settle()
    found = capture.matching(lambda r: r.call.resource == "a", after=1)
    assert [item.payload for item in found] == [3]


async def test_every_listener_is_removed_on_exit(mock_page):
    class _Listening(_Capture):
        def _extra_listeners(self):
            return (("request", self._on_request),)

        def _on_request(self, request: Any) -> None:
            pass

    async with _Listening(mock_page):
        assert len(mock_page.listeners["response"]) == 1
        assert len(mock_page.listeners["request"]) == 1
    assert mock_page.listeners["response"] == []
    assert mock_page.listeners["request"] == []


async def test_pending_reads_are_drained_on_exit(mock_page):
    gate = asyncio.Event()
    finished: list[str] = []

    class _Spawning(_Capture):
        def _extra_listeners(self):
            return (("request", self._on_request),)

        def _on_request(self, request: Any) -> None:
            async def read() -> None:
                await gate.wait()
                finished.append("done")

            self._spawn(read())

    async with _Spawning(mock_page):
        for callback in mock_page.listeners["request"]:
            callback(SimpleNamespace())
        asyncio.get_running_loop().call_later(0.01, gate.set)
    assert finished == ["done"]


async def test_a_read_that_never_finishes_is_cancelled_on_exit(mock_page):
    started = asyncio.Event()

    class _Hanging(_Capture):
        async def _refine(self, response: Any, call: _Call) -> _Call | None:
            started.set()
            await asyncio.Event().wait()
            return call

    async with _Hanging(mock_page) as capture:
        _emit(mock_page, _response("exampleItems", {}))
        await started.wait()
    assert capture._pending
    assert all(task.cancelled() for task in capture._pending)
