"""The shared capture's response records reach the failure notes (synthetic data)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.error_diagnostics import build_issue_diagnostics
from linkedin_mcp_server.scraping.api_capture import ApiResponseCapture

API = "https://www.linkedin.com/example-api/"


@dataclass(frozen=True, slots=True)
class _Call:
    resource: str
    entity_id: str | None
    finder: str | None


class _Capture(ApiResponseCapture[_Call]):
    label = "Example app"

    def _parse(self, response: Any) -> _Call | None:
        if not response.url.startswith(API):
            return None
        resource, _, entity = response.url[len(API) :].partition("/")
        return _Call(resource, entity or None, "search")


def _response(resource: str, payload: Any, *, status: int = 200) -> SimpleNamespace:
    return SimpleNamespace(
        url=API + resource,
        status=status,
        headers={"content-type": "application/json"},
        request=SimpleNamespace(
            method="GET", url=API + resource, headers={"accept": "application/json"}
        ),
        json=AsyncMock(return_value=payload),
    )


def _not_json(resource: str) -> SimpleNamespace:
    response = _response(resource, None)
    response.json = AsyncMock(side_effect=ValueError("not json"))
    return response


def _emit(page: Any, *responses: SimpleNamespace) -> None:
    for response in responses:
        for callback in list(page.listeners.get("response", [])):
            callback(response)


@pytest.fixture
def notes_env(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr(
        "linkedin_mcp_server.scraping.api_capture.note_throttle_signal",
        lambda *args, **kwargs: None,
    )


async def test_a_shape_refusal_writes_each_response_structure(mock_page, notes_env):
    payload = {
        "elements": [{"name": "Jane Q Example", "urn:li:member:5550123": 731942}],
        "paging": {"total": 8675309},
    }
    async with _Capture(mock_page) as capture:
        _emit(
            mock_page,
            _response("exampleItems/5550123", payload),
            _not_json("exampleOther"),
            _response("exampleThrottled", None, status=429),
        )
        await capture.settle()
    error = LinkedInScraperException("Example app returned a shape it refused.")

    diagnostics = build_issue_diagnostics(error, context="example_read")

    notes = Path(diagnostics["issue_template_path"]).read_text()
    assert "## Captured Example app API responses (structure only)" in notes
    assert "- Shown: 3 of 3 seen" in notes
    for secret in ("Jane Q Example", "5550123", "8675309", "731942"):
        assert secret not in notes, f"{secret!r} leaked into the notes"
    assert (
        '{"accept": "application/json", "body": "json", '
        '"content_type": "application/json", "entity_id": true, '
        '"finder": "search", "method": "GET", "resource": "exampleItems", '
        '"skeleton": {"elements": {"list[1]": {"<id-like key> x1": "int", '
        '"name": "str"}}, "paging": {"total": "int"}}, "status": 200}'
    ) in notes
    assert '"body": "not json"' in notes
    assert '"body": "not read"' in notes and '"status": 429' in notes


async def test_a_timeout_after_no_response_says_none_were_seen(mock_page, notes_env):
    async with _Capture(mock_page) as capture:
        with pytest.raises(LinkedInScraperException) as raised:
            await capture.wait_for(lambda r: True, timeout=0.01, throttle=False)

    diagnostics = build_issue_diagnostics(raised.value, context="example_read")

    notes = Path(diagnostics["issue_template_path"]).read_text()
    assert "- Shown: 0 of 0 seen" in notes


def test_a_failure_without_a_capture_has_no_response_section(notes_env):
    diagnostics = build_issue_diagnostics(RuntimeError("boom"), context="other")

    notes = Path(diagnostics["issue_template_path"]).read_text()
    assert "API responses (structure only)" not in notes
