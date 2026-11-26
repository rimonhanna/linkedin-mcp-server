"""The guard against the dialog's real request: admitted when planned, and a
refusal says what it stopped. URL and body are a sanitised copy of the live
capture; ids are placeholders."""

from __future__ import annotations

import json
import logging
from typing import Any

from linkedin_mcp_server.scraping import page_invite_guard as guard

COMPANY = "100000701"
INVITEE = "ACoAAPlaceholder0000000000000000000000"
BASE = "https://www.linkedin.com/voyager/api/voyagerRelationshipsDashInvitations"
OBSERVED = f"{BASE}?inviter=(organizationUrn:urn%3Ali%3Afsd_company%3A{COMPANY})"
BODY = json.dumps(
    {
        "elements": [
            {
                "inviteeMember": f"urn:li:fsd_profile:{INVITEE}",
                "genericInvitationType": "ORGANIZATION",
            }
        ]
    }
)


class _Request:
    def __init__(self, url: str, body: str = BODY) -> None:
        self.method = "POST"
        self.url = url
        self.post_data = body

    async def all_headers(self) -> dict[str, str]:
        return {"X-RestLi-Method": "BATCH_CREATE"}


class _Route:
    def __init__(self, request: _Request) -> None:
        self.request = request
        self.outcome: str | None = None

    async def fallback(self) -> None:
        self.outcome = "sent"

    async def abort(self) -> None:
        self.outcome = "aborted"


class _Page:
    def __init__(self) -> None:
        self.handler: Any = None

    async def route(self, _pattern: str, handler: Any) -> None:
        self.handler = handler


async def _through(url: str, planned: list[str]):
    page = _Page()
    async with guard.without_page_invites(page):
        async with guard.explicit_page_invite(
            page, company_id=COMPANY, planned=planned
        ) as window:
            route = _Route(_Request(url))
            await page.handler(route)
    return route.outcome, window


async def test_the_observed_request_for_the_plan_is_admitted():
    outcome, window = await _through(OBSERVED, [INVITEE])
    assert (outcome, window.refused) == ("sent", []), window.refused
    assert window.admitted == [[INVITEE]]


async def test_the_observed_request_for_another_page_is_refused():
    other = OBSERVED.replace(COMPANY, "9")
    outcome, window = await _through(other, [INVITEE])
    assert outcome == "aborted"
    assert window.refused == ["inviter is not this page"]


async def test_a_refusal_is_logged_with_reason_url_and_body(caplog):
    other = OBSERVED.replace(COMPANY, "9")
    with caplog.at_level(logging.WARNING, logger=guard.__name__):
        outcome, _window = await _through(other, [INVITEE])
    assert outcome == "aborted"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, warnings
    assert "inviter is not this page" in warnings[0], warnings[0]
    assert other in warnings[0], "the warning must name the URL"
    assert INVITEE in warnings[0], "the warning must carry a body excerpt"


async def test_a_refusal_outside_a_window_is_logged(caplog):
    page = _Page()
    with caplog.at_level(logging.WARNING, logger=guard.__name__):
        async with guard.without_page_invites(page):
            route = _Route(_Request(OBSERVED))
            await page.handler(route)
    assert route.outcome == "aborted"
    assert any(
        "outside an invite window" in r.getMessage() and OBSERVED in r.getMessage()
        for r in caplog.records
    )


async def test_a_failing_log_still_aborts(monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("log handler failed")

    monkeypatch.setattr(guard.logger, "warning", broken)
    outcome, _window = await _through(OBSERVED.replace(COMPANY, "9"), [INVITEE])
    assert outcome == "aborted"
