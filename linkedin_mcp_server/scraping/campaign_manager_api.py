"""Passive capture of Campaign Manager API responses, plus one analytics replay."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import asyncio
import logging

import anyio
import anyio.lowlevel

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.pacing import note_throttle_signal
from linkedin_mcp_server.scraping.campaign_manager_payload import (
    CM_API_PREFIX,
    CmCall,
    format_restli_date_range,
    parse_cm_call,
    restli_date_range,
    restli_list,
)

logger = logging.getLogger(__name__)

_NOISE = frozenset(
    f"campaignManager{name}"
    for name in (
        "LegoWidgets",
        "UICustomizations",
        "GlobalAlerts",
        "Notifications",
        "UnifiedNotifications",
        "LegalTermTracking",
    )
) | {"pemberlyChameleonConfigs"}
_ANALYTICS_RESOURCE = "campaignManagerAdAnalytics"
_ANALYTICS_FINDERS = frozenset({"statistics", "analytics"})
_FORWARDED_HEADERS = frozenset(
    {
        "accept",
        "content-type",
        "csrf-token",
        "x-http-method-override",
        "x-li-lang",
        "x-li-page-instance",
        "x-li-track",
        "x-restli-protocol-version",
    }
)
# Ids that scope an analytics query to part of an account, by entity kind.
SCOPE_ID_PARAMS = {
    "campaignGroupIds": "campaign_group",
    "campaignIds": "campaign",
    "creativeIds": "creative",
}
# Rest.li structure characters stay literal, as the app sends them.
_RESTLI_SAFE = "(),:"

# Only MEMBER_JOB_FUNCTION was seen answering; the others are the facets the
# demographics view offers and are unverified.
DEMOGRAPHIC_PIVOTS = frozenset(
    {
        "MEMBER_JOB_FUNCTION",
        "MEMBER_SENIORITY",
        "MEMBER_INDUSTRY",
        "MEMBER_COMPANY_SIZE",
        "MEMBER_COUNTRY_V2",
    }
)
LEVEL_PIVOTS = {
    "campaign_group": "CAMPAIGN_GROUP",
    "campaign": "CAMPAIGN",
    "creative": "CREATIVE",
}


@dataclass(frozen=True, slots=True)
class CmResponse:
    call: CmCall
    status: int
    payload: Any

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@dataclass(frozen=True, slots=True)
class _AnalyticsTemplate:
    url: str
    method: str
    headers: dict[str, str]
    # The Rest.li query: the form body of a tunneled POST, the URL query of a GET.
    body: dict[str, str]


def analytics_call_matches(call: CmCall, *, q: str, pivot: str | None) -> bool:
    """Whether an analytics call is the one a reader wants for ``pivot``.

    ``q=statistics`` must be the reporting table's single-pivot, whole-period
    projection; ``q=analytics`` is the demographics finder, any pivot when
    ``pivot`` is None.
    """
    params = call.params
    if call.resource != _ANALYTICS_RESOURCE or call.finder != q:
        return False
    if restli_date_range(params.get("dateRange", "")) is None:
        return False
    if q == "statistics":
        return (
            restli_list(params.get("pivots", "")) == [pivot]
            and params.get("timeGranularity") == "ALL"
            and "impressions" in params.get("fields", "")
        )
    return pivot is None or params.get("pivot") == pivot


def period_order(params: dict[str, str]) -> tuple[date, date]:
    """Sort key putting the latest ``dateRange`` last; the UI shows the latest.

    The UI also asks for the previous period to compare.
    """
    window = restli_date_range(params.get("dateRange", ""))
    return (window[1], window[0]) if window else (date.min, date.min)


def _is_read(method: str, headers: dict[str, str]) -> bool:
    """GET, or POST tunneling a GET; ``headers`` keys are casefolded."""
    method = method.upper()
    override = headers.get("x-http-method-override", "")
    return method == "GET" or (method == "POST" and override.casefold() == "get")


class CampaignManagerCapture:
    """Collect Campaign Manager API responses emitted by one page."""

    def __init__(self, page: Any):
        self._page = page
        self.responses: list[CmResponse] = []
        self.seen_resources: set[str] = set()
        self._templates: dict[tuple[str, ...], _AnalyticsTemplate] = {}
        self._service_worker_seen = False
        self._event = asyncio.Event()
        self._pending: list[asyncio.Task[None]] = []
        # Event emitters remove listeners by the registered callable identity.
        self._response_listener = self._on_response
        self._request_listener = self._on_request

    async def __aenter__(self) -> CampaignManagerCapture:
        self._page.on("response", self._response_listener)
        self._page.on("request", self._request_listener)
        return self

    async def __aexit__(self, exc_type: object, *_exc: object) -> None:
        for event, listener in (
            ("response", self._response_listener),
            ("request", self._request_listener),
        ):
            try:
                self._page.remove_listener(event, listener)
            except Exception:
                pass
        await self._drain_pending()
        if self._service_worker_seen and exc_type is None:
            # The write guard's route cannot see what a service worker serves.
            raise LinkedInScraperException(
                "Campaign Manager answered through a service worker, which this "
                "tool's write protection cannot see, so nothing was reported."
            )

    def _on_response(self, response: Any) -> None:
        if not urlparse(response.url).path.startswith(CM_API_PREFIX):
            return
        if getattr(response, "from_service_worker", False) is True:
            self._service_worker_seen = True
        request = response.request
        call = parse_cm_call(
            response.url, request.method, request.headers or {}, request.post_data
        )
        if call is None or call.resource in _NOISE:
            return
        self.seen_resources.add(call.resource)
        status = getattr(response, "status", 0)
        if status == 429:
            note_throttle_signal("http_429")
            return

        first: CmCall = call

        async def read() -> None:
            call = first
            if request.method.upper() == "POST" and call.finder is None:
                # A provisional header set can lack the override; the full set
                # says whether the body is a tunneled query.
                try:
                    headers = await request.all_headers()
                except Exception:
                    headers = {}
                call = (
                    parse_cm_call(
                        response.url, request.method, headers, request.post_data
                    )
                    or call
                )
            try:
                payload = await response.json()
            except Exception:
                payload = None
            self.responses.append(CmResponse(call, status, payload))
            self._event.set()

        self._pending.append(asyncio.create_task(read()))

    def _on_request(self, request: Any) -> None:
        method = request.method.upper()
        if method not in {"GET", "POST"}:
            return
        url = request.url
        if urlparse(url).path.rstrip("/").rsplit("/", 1)[-1] != _ANALYTICS_RESOURCE:
            return

        async def read() -> None:
            try:
                raw_headers = await request.all_headers()
            except Exception:
                return
            headers = {
                key.casefold(): value
                for key, value in raw_headers.items()
                if key.casefold() in _FORWARDED_HEADERS
            }
            # A replay must stay a read, so only a read is kept to replay.
            if not _is_read(method, headers):
                return
            call = parse_cm_call(url, method, raw_headers, request.post_data)
            if call is None or call.finder not in _ANALYTICS_FINDERS:
                return
            if method == "POST":
                body = _first_values(request.post_data or "")
                if "q" not in body:
                    return
            else:
                body = _first_values(urlparse(url).query)
            key = (
                method,
                body.get("q", ""),
                body.get("pivots", body.get("pivot", "")),
                body.get("timeGranularity", ""),
                call.params.get("fields", ""),
                body.get("dateRange", ""),
                # An account-wide query and an id-scoped chunk are distinct.
                body.get("accountIds", ""),
                *(body.get(param, "") for param in SCOPE_ID_PARAMS),
            )
            # One template per distinct query keeps the set bounded by the page.
            self._templates.setdefault(
                key, _AnalyticsTemplate(url, method, headers, body)
            )

        self._pending.append(asyncio.create_task(read()))

    def matching(
        self, predicate: Callable[[CmResponse], bool], after: int = 0
    ) -> list[CmResponse]:
        return [item for item in self.responses[after:] if predicate(item)]

    async def wait_for(
        self,
        predicate: Callable[[CmResponse], bool],
        *,
        after: int = 0,
        timeout: float = 12.0,
        throttle: bool = True,
    ) -> CmResponse:
        """Wait for the first captured response, from ``after`` on, that matches.

        ``throttle=False`` is for a wait whose miss has another explanation
        (an optional request the page may not make): no throttle evidence.
        """
        self._event.clear()
        try:
            async with asyncio.timeout(timeout):
                while True:
                    found = self.matching(predicate, after)
                    if found:
                        return found[0]
                    await self._event.wait()
                    self._event.clear()
        except TimeoutError as exc:
            seen = ", ".join(sorted(self.seen_resources)) or "none"
            logger.warning("No matching Campaign Manager response; seen: %s", seen)
            if throttle:
                # A half signal: one missing reply is also what a slow proxy
                # looks like; two within ten minutes make a throttle.
                note_throttle_signal("payload_timeout", half=True)
            raise LinkedInScraperException(
                "Campaign Manager did not return the expected data in time "
                f"(responses seen: {seen})."
            ) from exc

    async def settle(self, timeout: float = 1.0) -> None:
        """Let response reads already emitted by the browser finish parsing."""
        active = [task for task in self._pending if not task.done()]
        if active:
            await asyncio.wait(active, timeout=timeout)

    def analytics_template(
        self, *, q: str, pivot: str | None
    ) -> _AnalyticsTemplate | None:
        """The page's own analytics request for ``pivot``.

        An account-wide (``accountIds``) query wins over an id-scoped one, which
        may cover only the ids on screen; then the latest period wins.
        ``pivot=None`` accepts any demographics pivot, since a replay replaces it.
        """
        candidates = [
            template
            for template in self._templates.values()
            if analytics_call_matches(template_call(template), q=q, pivot=pivot)
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda template: (
                "accountIds" in template.body,
                period_order(template.body),
            ),
        )

    async def replay_analytics(
        self,
        template: _AnalyticsTemplate,
        *,
        date_range: tuple[date, date] | None,
        pivot: str | None,
    ) -> Any:
        """Repeat the page's own analytics query with another period or pivot.

        Only ``dateRange`` and, for demographics, ``pivot`` change: the
        resource, ids, projection and headers stay the page's.
        """
        q = template.body.get("q")
        if (
            urlparse(template.url).path.rstrip("/").rsplit("/", 1)[-1]
            != _ANALYTICS_RESOURCE
            or q not in _ANALYTICS_FINDERS
        ):
            raise LinkedInScraperException(
                "Refused to replay a request that is not Campaign Manager analytics."
            )
        if not _is_read(template.method, template.headers):
            raise LinkedInScraperException(
                "Refused to replay a Campaign Manager request that is not a read."
            )
        body = dict(template.body)
        if date_range is not None:
            body["dateRange"] = format_restli_date_range(*date_range)
        if pivot is not None:
            if q != "analytics" or pivot not in DEMOGRAPHIC_PIVOTS:
                raise LinkedInScraperException(
                    f"Refused to replay analytics with pivot {pivot!r}."
                )
            body["pivot"] = pivot
        encoded = urlencode(body, safe=_RESTLI_SAFE)
        if template.method.upper() == "GET":
            parsed = urlparse(template.url)
            url = parsed._replace(query=encoded).geturl()
            result = await self._fetch(url, "GET", template.headers, None)
        else:
            result = await self._fetch(template.url, "POST", template.headers, encoded)
        status = result.get("status")
        if status == 429:
            note_throttle_signal("http_429")
        if not isinstance(status, int) or not 200 <= status < 300:
            raise LinkedInScraperException(
                "LinkedIn refused the Campaign Manager analytics read"
                f" (HTTP {status if isinstance(status, int) else 'unknown'})."
            )
        return result.get("payload")

    async def _fetch(
        self, url: str, method: str, headers: dict[str, str], body: str | None
    ) -> dict[str, Any]:
        result = await self._page.evaluate(
            """async ({ url, method, headers, body }) => {
                /* linkedin-mcp:cm-analytics-replay */
                try {
                    const init = { method, credentials: 'same-origin', headers };
                    if (body !== null) init.body = body;
                    const response = await fetch(url, init);
                    let payload = null;
                    try { payload = await response.json(); } catch (_) {}
                    return { status: response.status, payload };
                } catch (_) {
                    return { status: 0, payload: null };
                }
            }""",
            {"url": url, "method": method, "headers": headers, "body": body},
        )
        return result if isinstance(result, dict) else {}

    async def _drain_pending(self) -> None:
        if not self._pending:
            return
        try:
            await asyncio.wait(self._pending, timeout=2.0)
        finally:
            for task in self._pending:
                if not task.done():
                    task.cancel()
            with anyio.CancelScope(shield=True):
                try:
                    await asyncio.wait(self._pending, timeout=1.0)
                finally:
                    for task in self._pending:
                        if task.done() and not task.cancelled():
                            task.exception()
        await anyio.lowlevel.checkpoint()


def _first_values(query: str) -> dict[str, str]:
    return {
        key: values[0]
        for key, values in parse_qs(query, keep_blank_values=True).items()
    }


def template_call(template: _AnalyticsTemplate) -> CmCall:
    """The template's query as a call, for the same matching as a response."""
    return CmCall(
        resource=_ANALYTICS_RESOURCE,
        entity_id=None,
        finder=template.body.get("q"),
        params={**_first_values(urlparse(template.url).query), **template.body},
    )
