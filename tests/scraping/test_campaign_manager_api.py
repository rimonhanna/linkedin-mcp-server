"""Network-boundary tests for passive Campaign Manager capture (synthetic data)."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

import pytest

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.scraping import campaign_manager_api as api_module
from linkedin_mcp_server.scraping.campaign_manager_api import CampaignManagerCapture

CM = "https://www.linkedin.com/campaign-manager-api/"
TUNNEL = {
    "x-http-method-override": "GET",
    "content-type": "application/x-www-form-urlencoded",
}
REPORTING_FIELDS = (
    "fields=baseMetrics:(costInLocalCurrency,impressions,clicks),pivotValues"
)
CURRENT = "(start:(day:1,month:10,year:2026),end:(day:31,month:10,year:2026))"
PREVIOUS = "(start:(day:1,month:9,year:2026),end:(day:30,month:9,year:2026))"


def _response(
    path: str,
    payload: Any = None,
    *,
    method: str = "GET",
    body: str | None = None,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> SimpleNamespace:
    sent = headers if headers is not None else (TUNNEL if method == "POST" else {})
    request = SimpleNamespace(
        method=method,
        url=CM + path,
        headers=sent,
        post_data=body,
        all_headers=AsyncMock(
            return_value={
                **TUNNEL,
                **sent,
                "csrf-token": "synthetic-token",
                "cookie": "must-not-be-forwarded",
            }
            if method == "POST"
            else {**sent, "csrf-token": "synthetic-token"}
        ),
    )
    return SimpleNamespace(
        url=CM + path,
        status=status,
        request=request,
        json=AsyncMock(return_value=payload),
    )


def _emit(page: Any, *responses: SimpleNamespace) -> None:
    for response in responses:
        for callback in list(page.listeners.get("request", [])):
            callback(response.request)
        for callback in list(page.listeners.get("response", [])):
            callback(response)


def _analytics(body: str, fields: str = REPORTING_FIELDS) -> SimpleNamespace:
    return _response(
        f"campaignManagerAdAnalytics?{fields}",
        {"elements": [], "paging": {"count": 10, "start": 0}},
        method="POST",
        body=body,
    )


def _statistics_body(date_range: str, pivot: str = "CAMPAIGN") -> str:
    return (
        f"q=statistics&pivots=List({pivot})&timeGranularity=ALL"
        f"&dateRange={date_range}&accountIds=List(100000001)"
    )


async def test_noise_is_ignored_and_data_is_kept(mock_page):
    async with CampaignManagerCapture(mock_page) as capture:
        _emit(
            mock_page,
            _response("campaignManagerLegoWidgets?q=x", {"elements": []}),
            _response("pemberlyChameleonConfigs", {}),
            _response("campaignManagerReportingAccounts?q=findAll", {"elements": []}),
        )
        found = await capture.wait_for(lambda r: True, timeout=1)
        await capture.settle()

    assert found.call.resource == "campaignManagerReportingAccounts"
    assert [r.call.resource for r in capture.responses] == [
        "campaignManagerReportingAccounts"
    ]
    assert capture.seen_resources == {"campaignManagerReportingAccounts"}
    assert mock_page.listeners["response"] == []
    assert mock_page.listeners["request"] == []


async def test_429_notes_a_throttle_and_keeps_nothing(mock_page):
    with patch.object(api_module, "note_throttle_signal") as signal:
        async with CampaignManagerCapture(mock_page) as capture:
            _emit(
                mock_page,
                _response("campaignManagerReportingAccounts?q=findAll", status=429),
            )
            await capture.settle()
    signal.assert_called_once_with("http_429")
    assert capture.responses == []


async def test_tunneled_body_keys_the_response(mock_page):
    async with CampaignManagerCapture(mock_page) as capture:
        _emit(mock_page, _analytics(_statistics_body(CURRENT)))
        found = await capture.wait_for(
            lambda r: r.call.params.get("pivots") == "List(CAMPAIGN)", timeout=1
        )
    assert found.call.finder == "statistics"
    assert found.call.params["dateRange"] == CURRENT
    assert found.call.params["fields"].startswith("baseMetrics:")


async def test_provisional_headers_fall_back_to_the_full_set(mock_page):
    response = _analytics(_statistics_body(CURRENT))
    response.request.headers = {}
    async with CampaignManagerCapture(mock_page) as capture:
        _emit(mock_page, response)
        found = await capture.wait_for(lambda r: True, timeout=1)
    assert found.call.finder == "statistics"


async def test_template_is_the_latest_reporting_query(mock_page):
    async with CampaignManagerCapture(mock_page) as capture:
        _emit(
            mock_page,
            _analytics(_statistics_body(PREVIOUS)),
            _analytics(_statistics_body(CURRENT)),
            # A sparkline: the latest period too, but not the reporting projection.
            _analytics(
                _statistics_body(
                    "(start:(day:1,month:11,year:2026),end:(day:2,month:11,year:2026))"
                ),
                fields="fields=dateRange,objectiveMetrics:(keyResults),pivotValues",
            ),
            _analytics(_statistics_body(CURRENT, pivot="CAMPAIGN_GROUP")),
        )
        await capture.settle()
        template = capture.analytics_template(q="statistics", pivot="CAMPAIGN")

    assert template is not None
    assert template.body["dateRange"] == CURRENT
    assert template.body["pivots"] == "List(CAMPAIGN)"
    assert "fields" not in template.body
    assert template.headers == {
        **TUNNEL,
        "csrf-token": "synthetic-token",
    }


async def test_replay_changes_only_the_date_range(mock_page):
    mock_page.evaluate = AsyncMock(
        return_value={"status": 200, "payload": {"elements": []}}
    )
    async with CampaignManagerCapture(mock_page) as capture:
        _emit(mock_page, _analytics(_statistics_body(CURRENT)))
        await capture.settle()
        template = capture.analytics_template(q="statistics", pivot="CAMPAIGN")
        assert template is not None
        payload = await capture.replay_analytics(
            template, date_range=(date(2026, 8, 1), date(2026, 8, 31)), pivot=None
        )

    assert payload == {"elements": []}
    script, args = mock_page.evaluate.await_args_list[-1].args
    assert "/* linkedin-mcp:cm-analytics-replay */" in script
    assert args["method"] == "POST"
    assert args["url"] == template.url
    assert args["headers"] == template.headers
    sent = {key: value[0] for key, value in parse_qs(args["body"]).items()}
    original = {
        key: value[0] for key, value in parse_qs(_statistics_body(CURRENT)).items()
    }
    changed = {key for key in sent if sent[key] != original.get(key)}
    assert changed == {"dateRange"}
    assert sent["dateRange"] == (
        "(start:(day:1,month:8,year:2026),end:(day:31,month:8,year:2026))"
    )
    assert "List(100000001)" in args["body"]


async def test_demographic_replay_changes_pivot_and_refuses_unknown(mock_page):
    mock_page.evaluate = AsyncMock(
        return_value={"status": 200, "payload": {"elements": []}}
    )
    body = (
        f"q=analytics&pivot=MEMBER_JOB_FUNCTION&timeGranularity=ALL"
        f"&dateRange={CURRENT}&accountIds=List(100000001)"
    )
    async with CampaignManagerCapture(mock_page) as capture:
        _emit(mock_page, _analytics(body), _analytics(_statistics_body(CURRENT)))
        await capture.settle()
        template = capture.analytics_template(q="analytics", pivot=None)
        statistics = capture.analytics_template(q="statistics", pivot="CAMPAIGN")
        assert template is not None and statistics is not None
        await capture.replay_analytics(
            template, date_range=None, pivot="MEMBER_SENIORITY"
        )
        with pytest.raises(LinkedInScraperException, match="pivot"):
            await capture.replay_analytics(
                template, date_range=None, pivot="MEMBER_SECRET"
            )
        with pytest.raises(LinkedInScraperException, match="pivot"):
            await capture.replay_analytics(
                statistics, date_range=None, pivot="MEMBER_SENIORITY"
            )

    sent = parse_qs(mock_page.evaluate.await_args_list[0].args[1]["body"])
    assert sent["pivot"] == ["MEMBER_SENIORITY"]
    assert sent["dateRange"] == [CURRENT]
    assert mock_page.evaluate.await_count == 1


async def test_replay_refuses_another_resource(mock_page):
    mock_page.evaluate = AsyncMock()
    template = api_module._AnalyticsTemplate(
        url=CM + "campaignManagerAccounts/100000001/billingConfiguration",
        method="POST",
        headers={},
        body={"q": "statistics"},
    )
    async with CampaignManagerCapture(mock_page) as capture:
        with pytest.raises(LinkedInScraperException, match="not Campaign Manager"):
            await capture.replay_analytics(template, date_range=None, pivot=None)
    mock_page.evaluate.assert_not_awaited()


@pytest.mark.parametrize("status", [400, 429, 0])
async def test_non_2xx_replay_raises(mock_page, status):
    mock_page.evaluate = AsyncMock(return_value={"status": status, "payload": None})
    with patch.object(api_module, "note_throttle_signal") as signal:
        async with CampaignManagerCapture(mock_page) as capture:
            _emit(mock_page, _analytics(_statistics_body(CURRENT)))
            await capture.settle()
            template = capture.analytics_template(q="statistics", pivot="CAMPAIGN")
            assert template is not None
            with pytest.raises(LinkedInScraperException, match=f"HTTP {status}"):
                await capture.replay_analytics(
                    template,
                    date_range=(date(2026, 8, 1), date(2026, 8, 2)),
                    pivot=None,
                )
    assert (signal.call_count == 1) is (status == 429)


async def test_plain_get_template_replays_as_get(mock_page):
    mock_page.evaluate = AsyncMock(return_value={"status": 200, "payload": {}})
    query = (
        f"q=statistics&pivots=List(CAMPAIGN)&timeGranularity=ALL&dateRange={CURRENT}"
        "&accountIds=List(100000009)&fields=baseMetrics:(impressions),pivotValues"
    )
    async with CampaignManagerCapture(mock_page) as capture:
        _emit(mock_page, _response(f"campaignManagerAdAnalytics?{query}", {}))
        await capture.settle()
        template = capture.analytics_template(q="statistics", pivot="CAMPAIGN")
        assert template is not None
        await capture.replay_analytics(
            template, date_range=(date(2026, 8, 1), date(2026, 8, 2)), pivot=None
        )
    args = mock_page.evaluate.await_args_list[-1].args[1]
    assert args["method"] == "GET"
    assert args["body"] is None
    sent = parse_qs(args["url"].split("?", 1)[1])
    assert sent["dateRange"] == [
        "(start:(day:1,month:8,year:2026),end:(day:2,month:8,year:2026))"
    ]
    assert sent["fields"] == ["baseMetrics:(impressions),pivotValues"]


async def test_wait_timeout_names_resources_and_half_signals(mock_page):
    with patch.object(api_module, "note_throttle_signal") as signal:
        async with CampaignManagerCapture(mock_page) as capture:
            _emit(mock_page, _response("campaignManagerHeader", {}))
            with pytest.raises(LinkedInScraperException, match="campaignManagerHeader"):
                await capture.wait_for(lambda r: False, timeout=0.05)
            with pytest.raises(LinkedInScraperException):
                await capture.wait_for(lambda r: False, timeout=0.05, throttle=False)
    signal.assert_called_once_with("payload_timeout", half=True)


async def test_untunneled_post_template_is_not_kept_and_not_replayed(mock_page):
    mock_page.evaluate = AsyncMock()
    write = _response(
        f"campaignManagerAdAnalytics?{REPORTING_FIELDS}",
        method="POST",
        body=_statistics_body(CURRENT),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    write.request.all_headers = AsyncMock(
        return_value={"content-type": "application/x-www-form-urlencoded"}
    )
    template = api_module._AnalyticsTemplate(
        url=CM + f"campaignManagerAdAnalytics?{REPORTING_FIELDS}",
        method="POST",
        headers={"content-type": "application/x-www-form-urlencoded"},
        body={"q": "statistics", "dateRange": CURRENT},
    )
    async with CampaignManagerCapture(mock_page) as capture:
        _emit(mock_page, write)
        await capture.settle()
        assert capture.analytics_template(q="statistics", pivot="CAMPAIGN") is None
        with pytest.raises(LinkedInScraperException, match="not a read"):
            await capture.replay_analytics(template, date_range=None, pivot=None)
    mock_page.evaluate.assert_not_awaited()


async def test_template_prefers_the_account_wide_query(mock_page):
    scoped = _statistics_body(CURRENT).replace(
        "accountIds=List(100000001)", "campaignIds=List(100000301)"
    )
    async with CampaignManagerCapture(mock_page) as capture:
        _emit(
            mock_page,
            _analytics(_statistics_body(PREVIOUS)),
            _analytics(scoped),
        )
        await capture.settle()
        template = capture.analytics_template(q="statistics", pivot="CAMPAIGN")
    assert template is not None
    assert template.body["accountIds"] == "List(100000001)"


async def test_a_service_worker_response_fails_the_read_closed(mock_page):
    response = _response("campaignManagerReportingAccounts?q=findAll", {})
    response.from_service_worker = True
    with pytest.raises(LinkedInScraperException, match="service worker"):
        async with CampaignManagerCapture(mock_page) as capture:
            _emit(mock_page, response)
            await capture.wait_for(lambda r: True, timeout=1)


def _write(
    path: str,
    body: str | None,
    *,
    restli: str,
    status: int = 201,
    answer: dict[str, str] | None = None,
) -> SimpleNamespace:
    headers = {"content-type": "application/json", "X-RestLi-Method": restli}
    response = _response(
        path, {"id": 1}, method="POST", body=body, status=status, headers=headers
    )
    response.request.all_headers = AsyncMock(return_value=headers)
    response.all_headers = AsyncMock(return_value=answer or {})
    return response


async def test_a_write_answer_is_kept_apart_from_reads(mock_page):
    create = _write(
        "campaignManagerCampaigns",
        '{"status": "DRAFT"}',
        restli="CREATE",
        answer={"X-RestLi-Id": "100000301"},
    )
    async with CampaignManagerCapture(mock_page) as capture:
        _emit(
            mock_page,
            _response("campaignManagerReportingAccounts?q=findAll", {"elements": []}),
            create,
        )
        found = await capture.wait_for_write(
            lambda w: w.call.resource == "campaignManagerCampaigns", timeout=1
        )
        await capture.settle()
    assert found is not None
    assert found.ok
    assert (found.method, found.restli_method) == ("POST", "CREATE")
    assert found.body == {"status": "DRAFT"}
    assert found.created_id == "100000301"
    assert capture.write_count == 1
    assert [r.call.resource for r in capture.responses] == [
        "campaignManagerReportingAccounts"
    ]


async def test_a_read_only_post_is_a_read(mock_page):
    response = _response(
        "campaignManagerEntityRecommendations",
        {"elements": []},
        method="POST",
        body="{}",
        headers={"content-type": "application/json"},
    )
    response.request.all_headers = AsyncMock(
        return_value={"content-type": "application/json"}
    )
    async with CampaignManagerCapture(mock_page) as capture:
        _emit(mock_page, response)
        await capture.wait_for(lambda r: True, timeout=1)
    assert capture.writes == []


async def test_wait_for_write_skips_earlier_writes_and_times_out_quietly(mock_page):
    patch_url = "campaignManagerCampaigns/100000301"
    with patch.object(api_module, "note_throttle_signal") as signal:
        async with CampaignManagerCapture(mock_page) as capture:
            _emit(mock_page, _write(patch_url, "{}", restli="PARTIAL_UPDATE"))
            await capture.settle()
            after = capture.write_count
            missed = await capture.wait_for_write(
                lambda w: True, after=after, timeout=0.05
            )
            _emit(
                mock_page,
                _write(patch_url, "not json", restli="PARTIAL_UPDATE", status=400),
            )
            found = await capture.wait_for_write(lambda w: True, after=after, timeout=1)
    assert missed is None
    signal.assert_not_called()
    assert found is not None
    assert (found.status, found.ok, found.body) == (400, False, None)
    assert found.created_id is None
