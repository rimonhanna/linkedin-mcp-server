"""The guard that lets the invite dialog send only the planned batch."""

from __future__ import annotations

import json
from typing import Any

import pytest

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.scraping import page_invite_guard as guard

COMPANY = "100000701"
URL = "https://www.linkedin.com/voyager/api/voyagerRelationshipsDashInvitations"


class _Request:
    def __init__(
        self, body: Any, *, method: str = "POST", restli="batch_create", url=URL
    ):
        self.method = method
        self.url = url
        self.post_data = json.dumps(body)
        self._headers = {"x-restli-method": restli} if restli else {}

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


def _batch(*ids: str, kind: str = "ORGANIZATION") -> dict:
    return {
        "elements": [
            {
                "inviteeMember": f"urn:li:fsd_profile:{i}",
                "genericInvitationType": kind,
                "trackingId": "t",
            }
            for i in ids
        ]
    }


async def _send(page: _Page, request: _Request) -> str | None:
    (_pattern, handler), *_ = page.routes
    route = _Route(request)
    await handler(route)
    return route.outcome


async def test_outside_the_tools_the_route_lets_everything_through():
    page = _Page()
    await guard.register_page_invite_routes(page)
    assert await _send(page, _Request(_batch("ACoAAone1"))) == "sent"


async def test_while_a_tool_holds_the_page_every_invitation_is_aborted():
    page = _Page()
    async with guard.without_page_invites(page):
        assert await _send(page, _Request(_batch("ACoAAone1"))) == "aborted"
        get = _Request(None, method="GET", restli=None)
        assert await _send(page, get) == "sent"
    assert await _send(page, _Request(_batch("ACoAAone1"))) == "sent"


@pytest.mark.parametrize(
    ("request_", "reason"),
    [
        (_Request(_batch("ACoAAone1")), "invitees differ from the plan"),
        (
            _Request(_batch("ACoAAone1", "ACoAAtwo2", "ACoAAthr3")),
            "invitees differ from the plan",
        ),
        (
            _Request(_batch("ACoAAone1", "ACoAAone1")),
            "invitees differ from the plan",
        ),
        (_Request(_batch("ACoAAone1", "ACoAAtwo2", kind="EVENT")), "not an invitation"),
        (_Request(_batch("ACoAAone1", "ACoAAtwo2"), restli=None), "not a batch create"),
    ],
)
async def test_the_window_aborts_anything_but_the_exact_plan(request_, reason):
    page = _Page()
    async with guard.without_page_invites(page):
        async with guard.explicit_page_invite(
            page, company_id=COMPANY, planned=["ACoAAone1", "ACoAAtwo2"]
        ) as window:
            assert await _send(page, request_) == "aborted"
    assert window.admitted == []
    assert window.refused and window.refused[0].startswith(reason)


async def test_the_window_admits_the_plan_once_in_any_order():
    page = _Page()
    async with guard.without_page_invites(page):
        async with guard.explicit_page_invite(
            page, company_id=COMPANY, planned=["ACoAAone1", "ACoAAtwo2"]
        ) as window:
            first = _Request(_batch("ACoAAtwo2", "ACoAAone1"))
            assert await _send(page, first) == "sent"
            assert await _send(page, first) == "aborted"
        assert await _send(page, first) == "aborted"
    assert window.admitted == [["ACoAAone1", "ACoAAtwo2"]]
    assert window.refused == ["a batch was already sent"]


async def test_a_window_needs_the_page_held():
    page = _Page()
    with pytest.raises(LinkedInScraperException, match="outside its protection"):
        async with guard.explicit_page_invite(
            page, company_id=COMPANY, planned=["ACoAAone1"]
        ):
            pass


OTHER = (
    "https://www.linkedin.com/voyager/api/voyagerRelationshipsDashMemberRelationships"
)


async def test_the_route_covers_every_voyager_request():
    page = _Page()
    await guard.register_page_invite_routes(page)
    assert [pattern for pattern, _ in page.routes] == ["**/voyager/api/**"]


async def test_outside_the_window_only_invitations_are_held():
    page = _Page()
    async with guard.without_page_invites(page):
        assert await _send(page, _Request(_batch("ACoAAone1"), url=OTHER)) == "sent"


async def test_the_window_aborts_the_plan_sent_to_any_other_endpoint():
    page = _Page()
    async with guard.without_page_invites(page):
        async with guard.explicit_page_invite(
            page, company_id=COMPANY, planned=["ACoAAone1"]
        ) as window:
            other = _Request(_batch("ACoAAone1"), url=OTHER)
            assert await _send(page, other) == "aborted", "only the endpoint passes"
            graphql = _Request(
                {"variables": {}}, restli=None, url=OTHER.rsplit("/", 1)[0] + "/graphql"
            )
            assert await _send(page, graphql) == "aborted"
    assert window.admitted == []
    assert window.refused == ["not the invitations endpoint"] * 2
