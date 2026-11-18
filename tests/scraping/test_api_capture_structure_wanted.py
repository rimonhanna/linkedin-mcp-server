"""A failure's notes keep every response of the resources the read awaited (synthetic data)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.error_diagnostics import build_issue_diagnostics
from linkedin_mcp_server.response_structure import (
    MAX_RECORDS,
    MAX_WANTED,
    ResponseLog,
    params_shape,
)
from linkedin_mcp_server.scraping.campaign_manager_api import CampaignManagerCapture

CM = "https://www.linkedin.com/campaign-manager-api/"
TUNNEL = {
    "x-http-method-override": "GET",
    "content-type": "application/x-www-form-urlencoded",
}


def _get(path: str, payload: object) -> SimpleNamespace:
    request = SimpleNamespace(
        method="GET",
        url=CM + path,
        headers={},
        post_data=None,
        all_headers=AsyncMock(return_value={}),
    )
    return SimpleNamespace(
        url=CM + path,
        status=200,
        headers={"content-type": "application/json"},
        request=request,
        json=AsyncMock(return_value=payload),
    )


def _tunneled(path: str, body: str) -> SimpleNamespace:
    response = _get(path, {"elements": [], "paging": {}})
    response.request.method = "POST"
    response.request.headers = TUNNEL
    response.request.post_data = body
    response.request.all_headers = AsyncMock(return_value=TUNNEL)
    return response


def _emit(page, *responses: SimpleNamespace) -> None:
    for response in responses:
        for callback in list(page.listeners.get("request", [])):
            callback(response.request)
        for callback in list(page.listeners.get("response", [])):
            callback(response)


@pytest.fixture
def notes_env(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr(
        "linkedin_mcp_server.scraping.api_capture.note_throttle_signal",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.scraping.campaign_manager_api.note_throttle_signal",
        lambda *args, **kwargs: None,
    )


def _records(notes: str) -> list[dict]:
    block = notes.split("```json\n", 1)[1].split("\n```", 1)[0]
    return [json.loads(line) for line in block.splitlines() if line]


async def test_an_early_response_of_the_awaited_resource_survives_later_noise(
    mock_page, notes_env
):
    record = _get("campaignManagerCampaigns/100000301", {"data": {"id": "x"}})
    noise = [
        _get(f"campaignManagerOther?q=n{index}", {"elements": []})
        for index in range(MAX_RECORDS + 10)
    ]
    async with CampaignManagerCapture(mock_page) as capture:
        _emit(mock_page, record, *noise)
        await capture.settle()
        with pytest.raises(LinkedInScraperException) as raised:
            await capture.wait_for(
                lambda r: False, timeout=0.01, wanted=("campaignManagerCampaigns",)
            )

    notes = Path(
        build_issue_diagnostics(raised.value, context="get_campaign")[
            "issue_template_path"
        ]
    ).read_text()

    records = _records(notes)
    assert records[0]["resource"] == "campaignManagerCampaigns"
    assert records[0]["skeleton"] == {"data": {"id": "str"}}
    assert len(records) == MAX_RECORDS + 1
    assert f"- Shown: {MAX_RECORDS + 1} of {MAX_RECORDS + 11} seen" in notes
    assert "the read waited for is shown: campaignManagerCampaigns" in notes
    assert "100000301" not in notes


def test_the_log_shows_at_most_max_wanted_older_records_of_a_resource():
    log = ResponseLog("Example app")
    for index in range(MAX_WANTED + 5):
        log.add({"resource": "wanted", "index": index})
    for index in range(MAX_RECORDS):
        log.add({"resource": "other", "index": index})
    log.want(["wanted"])

    shown = log.shown()

    assert len(shown) == MAX_WANTED + MAX_RECORDS
    assert shown[0] == {"resource": "wanted", "index": 5}
    assert shown[MAX_WANTED] == {"resource": "other", "index": 0}


def test_without_a_wanted_resource_the_log_shows_the_recent_records():
    log = ResponseLog("Example app")
    for index in range(MAX_RECORDS + 3):
        log.add({"resource": "x", "index": index})
    assert log.shown() == list(log.records)


async def test_a_tunneled_analytics_query_is_recorded_as_structure(
    mock_page, notes_env
):
    body = (
        "q=statistics&pivots=List(CAMPAIGN)&timeGranularity=ALL"
        "&dateRange=(start:(day:1,month:10,year:2026),end:(day:31,month:10,year:2026))"
        "&accountIds=List(100000001)"
        "&fields=baseMetrics:(impressions,clicks),pivotValues"
        "&searchText=Example%20secret%20words"
    )
    async with CampaignManagerCapture(mock_page) as capture:
        _emit(mock_page, _tunneled("campaignManagerAdAnalytics", body))
        await capture.settle()
    error = LinkedInScraperException("Campaign Manager loaded analytics, but none.")

    notes = Path(
        build_issue_diagnostics(error, context="get_campaign_analytics")[
            "issue_template_path"
        ]
    ).read_text()

    (record,) = _records(notes)
    assert record["params"] == {
        "q": "statistics",
        "pivots": "List(CAMPAIGN)",
        "timeGranularity": "ALL",
        "dateRange": "(start:(day:N,month:N,year:N),end:(day:N,month:N,year:N))",
        "accountIds": "List(N)",
        "fields": "baseMetrics:(impressions,clicks),pivotValues",
        "searchText": "str",
    }
    for secret in ("100000001", "secret"):
        assert secret not in notes, secret


def test_params_shape_drops_id_like_keys_and_types_other_values():
    assert params_shape({"100000001": "x", "count": "25", "ids": ["a"]}) == {
        "count": "N",
        "ids": "list",
    }
    assert params_shape(None) is None
