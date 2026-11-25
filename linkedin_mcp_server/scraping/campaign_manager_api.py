"""Passive capture of Campaign Manager API responses, plus one analytics replay."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import asyncio
import json
import re

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.pacing import note_throttle_signal
from linkedin_mcp_server.scraping.api_capture import (
    ApiResponseCapture,
    CapturedResponse,
)
from linkedin_mcp_server.scraping.campaign_manager_guard import (
    is_campaign_manager_read,
)
from linkedin_mcp_server.scraping.campaign_manager_payload import (
    BASE_METRICS,
    CM_API_PREFIX,
    CmCall,
    format_restli_date_range,
    parse_cm_call,
    restli_date_range,
    restli_list,
)

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
# A second pivot the reporting table adds to a level query; its rows sum per entity.
_SECOND_PIVOTS = frozenset({"OBJECTIVE_TYPE"})
_FIELD_NAME = re.compile(r"[A-Za-z]+")


CmResponse = CapturedResponse[CmCall]
# What ends every wait at once: an exception for a response the tool cannot go on past.
Refusal = Callable[[CmResponse], LinkedInScraperException | None]


@dataclass(frozen=True, slots=True)
class CmWriteResponse:
    """LinkedIn's answer to a non-read request the app sent."""

    call: CmCall
    method: str
    restli_method: str | None
    body: Any
    status: int
    payload: Any
    # ``x-restli-id``: the id of the entity a create made.
    created_id: str | None

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

    ``q=statistics`` must be a whole-period query pivoted by ``pivot``, alone
    or with objective type, projecting at least one base metric (a sparkline
    projects only key results); metrics it leaves out are absent, not zero;
    ``q=analytics`` is the demographics finder, any pivot when ``pivot`` is None.
    Scope (account or ids) is the caller's check.
    """
    params = call.params
    if call.resource != _ANALYTICS_RESOURCE or call.finder != q:
        return False
    if restli_date_range(params.get("dateRange", "")) is None:
        return False
    if q == "statistics":
        pivots = restli_list(params.get("pivots", ""))
        return (
            pivot in pivots
            and len(set(pivots)) == len(pivots)
            and set(pivots) - {pivot} <= _SECOND_PIVOTS
            and params.get("timeGranularity") == "ALL"
            and not BASE_METRICS.isdisjoint(
                _FIELD_NAME.findall(params.get("fields", ""))
            )
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


class CampaignManagerCapture(ApiResponseCapture[CmCall]):
    """Collect Campaign Manager API responses emitted by one page."""

    label = "Campaign Manager"

    def __init__(self, page: Any, *, refuse: Refusal | None = None):
        super().__init__(page)
        self._refuse = refuse
        self._refused: LinkedInScraperException | None = None
        self.writes: list[CmWriteResponse] = []
        self._templates: dict[tuple[str, ...], _AnalyticsTemplate] = {}
        self._service_worker_seen = False
        self._write_event = asyncio.Event()

    def _note_throttle(self, signal: str, **kwargs: bool) -> None:
        # Resolved in this module, where callers patch the signal.
        note_throttle_signal(signal, **kwargs)

    def _extra_listeners(self) -> tuple[tuple[str, Callable[..., None]], ...]:
        return (("request", self._on_request),)

    async def __aexit__(self, exc_type: object, *_exc: object) -> None:
        await super().__aexit__(exc_type, *_exc)
        refused = self._refusal()
        # A caller may have taken the wait's refusal for a miss; it still wins,
        # but never over a cancellation or a write LinkedIn answered.
        if refused is not None and not self.writes:
            if exc_type is None:
                raise refused
            error = _exc[0]
            if isinstance(error, Exception) and error is not refused:
                raise refused from error
        if self._service_worker_seen and exc_type is None:
            # The write guard's route cannot see what a service worker serves.
            raise LinkedInScraperException(
                "Campaign Manager answered through a service worker, which this "
                "tool's write protection cannot see, so nothing was reported."
            )

    def _refusal(self) -> LinkedInScraperException | None:
        if self._refused is None and self._refuse is not None:
            for response in self.responses:
                self._refused = self._refuse(response)
                if self._refused is not None:
                    break
        return self._refused

    async def wait_for(
        self,
        predicate: Callable[[CmResponse], bool],
        *,
        after: int = 0,
        timeout: float = 12.0,
        throttle: bool | Callable[[], bool] = True,
        wanted: Iterable[str] = (),
    ) -> CmResponse:
        """As the base wait, but a response ``refuse`` names raises at once.

        Not once a write was answered: that write's result stands.
        """
        refuse = self._refuse
        if refuse is None or self.writes:
            return await super().wait_for(
                predicate,
                after=after,
                timeout=timeout,
                throttle=throttle,
                wanted=wanted,
            )
        if (refused := self._refusal()) is not None:
            raise refused
        found = await super().wait_for(
            lambda r: predicate(r) or (not self.writes and refuse(r) is not None),
            after=after,
            timeout=timeout,
            throttle=throttle,
            wanted=wanted,
        )
        if not self.writes and (refused := self._refusal()) is not None:
            raise refused
        return found

    def _parse(self, response: Any) -> CmCall | None:
        if not urlparse(response.url).path.startswith(CM_API_PREFIX):
            return None
        if getattr(response, "from_service_worker", False) is True:
            self._service_worker_seen = True
        request = response.request
        call = parse_cm_call(
            response.url, request.method, request.headers or {}, request.post_data
        )
        if call is None or call.resource in _NOISE:
            return None
        return call

    async def _refine(self, response: Any, call: CmCall) -> CmCall | None:
        """The call as a read, or None once a write is recorded in ``writes``."""
        request = response.request
        method = request.method.upper()
        headers = request.headers or {}
        if method != "GET":
            # A provisional header set can lack the override; the full set
            # says whether the body is a tunneled query.
            try:
                headers = await request.all_headers()
            except Exception:
                pass
            call = (
                parse_cm_call(response.url, method, headers, request.post_data) or call
            )
        if is_campaign_manager_read(method, headers, call.resource):
            return call
        await self._record_write(response, call, method, headers)
        return None

    async def _record_write(
        self, response: Any, call: CmCall, method: str, headers: dict[str, str]
    ) -> None:
        lowered = {key.casefold(): value for key, value in headers.items()}
        try:
            text = response.request.post_data
            body = json.loads(text) if text else None
        except ValueError:
            body = None
        try:
            payload = await response.json()
        except Exception:
            payload = None
        try:
            answer = {
                key.casefold(): value
                for key, value in (await response.all_headers()).items()
            }
        except Exception:
            answer = {}
        self.writes.append(
            CmWriteResponse(
                call=call,
                method=method,
                restli_method=lowered.get("x-restli-method"),
                body=body,
                status=getattr(response, "status", 0),
                payload=payload,
                created_id=answer.get("x-restli-id"),
            )
        )
        self._write_event.set()

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

        self._spawn(read())

    @property
    def write_count(self) -> int:
        return len(self.writes)

    async def wait_for_write(
        self,
        predicate: Callable[[CmWriteResponse], bool],
        *,
        after: int = 0,
        timeout: float = 20.0,
    ) -> CmWriteResponse | None:
        """The first captured write answer from ``after`` on that matches.

        None on timeout and no throttle signal: a save that never came is the
        caller's verdict, not evidence of throttling.
        """
        self._write_event.clear()
        try:
            async with asyncio.timeout(timeout):
                while True:
                    for item in self.writes[after:]:
                        if predicate(item):
                            return item
                    await self._write_event.wait()
                    self._write_event.clear()
        except TimeoutError:
            return None

    def analytics_template(
        self,
        *,
        q: str,
        pivot: str | None,
        accept: Callable[[CmCall], bool] = lambda _call: True,
    ) -> _AnalyticsTemplate | None:
        """The page's own analytics request for ``pivot`` that ``accept`` takes.

        An account-wide (``accountIds``) query wins over an id-scoped one; then
        the latest period wins. ``pivot=None`` accepts any demographics pivot,
        since a replay replaces it.
        """
        candidates = [
            template
            for template in self._templates.values()
            if analytics_call_matches(call := template_call(template), q=q, pivot=pivot)
            and accept(call)
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
