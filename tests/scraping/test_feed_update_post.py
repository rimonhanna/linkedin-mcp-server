"""Resolving a feed activity to its post URN (synthetic data only)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

from linkedin_mcp_server.response_structure import current_log
from linkedin_mcp_server.scraping import feed_update_post as module
from linkedin_mcp_server.scraping.feed_update_post import (
    parse_voyager_call,
    post_urns_for_activity,
    resolve_activity_post,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

ACTIVITY = "urn:li:activity:100000801"
SHARE = "urn:li:share:100000701"
GRAPHQL = (
    "https://www.linkedin.com/voyager/api/graphql?variables=(x:1)"
    "&queryId=voyagerFeedDashUpdates.0123456789abcdef"
)


def _metadata(backend: str = ACTIVITY, share: Any = SHARE) -> dict[str, Any]:
    # PROVISIONAL candidate shape: Voyager update metadata.
    return {
        "$type": "com.example.UpdateMetadata",
        "backendUrn": backend,
        "shareUrn": share,
    }


def _normalized(*included: dict[str, Any]) -> dict[str, Any]:
    return {"data": {"$type": "com.example.Data"}, "included": list(included)}


def _response(url: str, payload: Any, *, status: int = 200, method: str = "GET"):
    request = SimpleNamespace(method=method, url=url, headers={}, post_data=None)
    return SimpleNamespace(
        url=url,
        status=status,
        request=request,
        headers={"content-type": "application/vnd.linkedin.normalized+json+2.1"},
        json=AsyncMock(return_value=payload),
    )


class _Page:
    def __init__(self, responses: list[Any]) -> None:
        self.url = "https://www.linkedin.com/feed/"
        self.listeners: dict[str, list[Any]] = {}
        self.responses = responses
        self.visited: list[str] = []

    def on(self, event: str, callback: Any) -> None:
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event: str, callback: Any) -> None:
        self.listeners[event].remove(callback)

    async def navigate(self, url: str) -> None:
        self.visited.append(url)
        self.url = url
        for response in self.responses:
            for callback in list(self.listeners.get("response", [])):
                callback(response)


async def _resolve(page: _Page) -> Any:
    session = ScrapingSession(cast(Any, page))
    with (
        patch.object(
            PageNavigator, "_navigate_to_page", AsyncMock(side_effect=page.navigate)
        ),
        patch.object(ScrapingSession, "check_rate_limit", new_callable=AsyncMock),
        patch.object(module, "_READY_TIMEOUT", 0.2),
    ):
        return await resolve_activity_post(session, PageNavigator(session), ACTIVITY)


def test_a_graphql_call_is_named_by_its_query():
    call = parse_voyager_call(GRAPHQL, "get")
    assert call is not None
    assert (call.resource, call.finder, call.method) == (
        "graphql",
        "voyagerFeedDashUpdates",
        "GET",
    )
    assert parse_voyager_call("https://www.linkedin.com/feed/", "GET") is None


def test_provisional_shape_reads_the_post_of_that_exact_activity():
    payload = _normalized(
        _metadata(backend="urn:li:activity:100000802", share="urn:li:share:9"),
        _metadata(),
    )
    assert post_urns_for_activity(payload, ACTIVITY) == {SHARE}


def test_provisional_shape_ignores_a_share_urn_that_is_not_a_post():
    payload = _normalized(_metadata(share="urn:li:activity:100000801"))
    assert post_urns_for_activity(payload, ACTIVITY) == set()
    payload = _normalized(_metadata(share=None))
    assert post_urns_for_activity(payload, ACTIVITY) == set()


async def test_provisional_shape_resolves_on_the_update_page():
    page = _Page([_response(GRAPHQL, _normalized(_metadata()))])

    assert await _resolve(page) == SHARE
    assert page.visited == [f"https://www.linkedin.com/feed/update/{ACTIVITY}/"]


async def test_an_unknown_shape_is_refused_and_its_structure_recorded():
    unknown = _normalized({"entityUrn": ACTIVITY, "postUrn": SHARE})
    page = _Page([_response(GRAPHQL, unknown)])

    result = await _resolve(page)

    assert result[0] == "control_unavailable"
    assert "post id could not be read" in result[1]
    log = current_log()
    assert log is not None and log.label == "LinkedIn feed update"
    (record,) = log.shown()
    assert record["resource"] == "graphql"
    assert record["finder"] == "voyagerFeedDashUpdates"
    # Value-free: the URNs themselves never reach the notes.
    assert ACTIVITY not in json.dumps(record) and SHARE not in json.dumps(record)
    assert "included" in record["skeleton"]


async def test_two_posts_for_one_activity_are_refused():
    payload = _normalized(_metadata(), _metadata(share="urn:li:ugcPost:100000702"))
    page = _Page([_response(GRAPHQL, payload)])

    result = await _resolve(page)

    assert result[0] == "post_not_found"
