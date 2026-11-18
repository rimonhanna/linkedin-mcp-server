"""Resolve a feed activity URN to the share or ugcPost URN of its post.

Company post links and get_company_posts name a post by its feed activity
(``urn:li:activity:<n>``); an ad references the post itself (``urn:li:share``
or ``urn:li:ugcPost``). This opens the activity's ``/feed/update/`` page, one
charged page load, and reads LinkedIn's own Voyager answers passively.

PROVISIONAL: no live capture of that page's answers has been recorded yet.
The one shape read is Voyager's update metadata, an object whose
``backendUrn`` is the activity and whose ``shareUrn`` is the post. Anything
else is ``post id could not be read`` with the structure of every answer in
the failure notes, so one live run shows the real shape.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlparse

import re

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.scraping.api_capture import (
    ApiResponseCapture,
    CapturedResponse,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

VOYAGER_API_PREFIX = "/voyager/api/"
_POST_URN = re.compile(r"urn:li:(?:share|ugcPost):[0-9]+")
_READY_TIMEOUT = 12.0
# Bounds the walk over one answer; Voyager pages are a few hundred objects.
_MAX_NODES = 20_000

Refusal = tuple[str, str]


@dataclass(frozen=True, slots=True)
class VoyagerCall:
    resource: str
    finder: str | None
    method: str
    params: Mapping[str, str] = field(default_factory=dict)
    entity_id: str | None = None


def parse_voyager_call(url: str, method: str) -> VoyagerCall | None:
    """A Voyager API request, or None for any other URL.

    ``resource`` is the first path segment (``graphql``, ``feed`` ...); a
    GraphQL call's ``finder`` is its query name without the hash suffix.
    """
    parsed = urlparse(url)
    if not parsed.path.startswith(VOYAGER_API_PREFIX):
        return None
    segments = [s for s in parsed.path[len(VOYAGER_API_PREFIX) :].split("/") if s]
    if not segments:
        return None
    params: dict[str, str] = {}
    for pair in parsed.query.split("&"):
        if pair:
            key, _, value = pair.partition("=")
            params.setdefault(key, value)
    query_id = unquote(params.get("queryId", ""))
    finder = (query_id.split(".", 1)[0] or None) if query_id else params.get("q")
    return VoyagerCall(
        resource="/".join(segments[:2]) if segments[0] != "graphql" else "graphql",
        finder=finder,
        method=method.upper(),
        params=params,
    )


class FeedUpdateCapture(ApiResponseCapture[VoyagerCall]):
    """Collect the Voyager answers a ``/feed/update/`` page makes; GET bodies only."""

    label = "LinkedIn feed update"

    def _parse(self, response: Any) -> VoyagerCall | None:
        return parse_voyager_call(response.url, response.request.method)

    def _wants_body(self, call: VoyagerCall) -> bool:
        return call.method == "GET"


def _objects(value: Any) -> Iterator[Mapping[str, Any]]:
    stack = [value]
    seen = 0
    while stack and seen < _MAX_NODES:
        item = stack.pop()
        seen += 1
        if isinstance(item, Mapping):
            yield item
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def post_urns_for_activity(payload: Any, activity: str) -> set[str]:
    """Post URNs of every object whose own ``backendUrn`` is exactly ``activity``.

    PROVISIONAL shape (Voyager update metadata): ``{backendUrn: activity,
    shareUrn: urn:li:share|ugcPost:<n>}``. A ``shareUrn`` of any other form is
    not a post URN and is not returned.
    """
    found: set[str] = set()
    for item in _objects(payload):
        if item.get("backendUrn") != activity:
            continue
        share = item.get("shareUrn")
        if isinstance(share, str) and _POST_URN.fullmatch(share):
            found.add(share)
    return found


def _update_url(activity: str) -> str:
    return f"https://www.linkedin.com/feed/update/{activity}/"


async def resolve_activity_post(
    session: ScrapingSession, navigator: PageNavigator, activity: str
) -> str | Refusal:
    """The share or ugcPost URN of ``activity``'s post, or ``(status, message)``.

    ``control_unavailable`` (post id could not be read) gets failure notes
    from the caller with this capture's answer structure.
    """
    found: set[str] = set()

    def names_post(response: CapturedResponse[VoyagerCall]) -> bool:
        if response.ok:
            found.update(post_urns_for_activity(response.payload, activity))
        return bool(found)

    async with FeedUpdateCapture(session.page) as capture:
        after = len(capture.responses)
        await navigator._navigate_to_page(_update_url(activity))
        await session.check_rate_limit()
        try:
            await capture.wait_for(
                names_post,
                after=after,
                timeout=_READY_TIMEOUT,
                throttle=False,
                wanted=("graphql", "feed/updates", "feed/updatesV2"),
            )
        except LinkedInScraperException:
            pass
        await capture.settle()
        for response in capture.matching(lambda r: r.ok, after):
            found.update(post_urns_for_activity(response.payload, activity))
    if len(found) == 1:
        return found.pop()
    if found:
        return (
            "post_not_found",
            "LinkedIn names more than one post for that activity, so it cannot "
            "be told which one to add. Pass the post's urn:li:share or "
            "urn:li:ugcPost URN instead. Nothing was changed.",
        )
    return (
        "control_unavailable",
        "The post id could not be read from LinkedIn's page for that activity. "
        "Pass the post's urn:li:share or urn:li:ugcPost URN instead. Nothing "
        "was changed.",
    )
