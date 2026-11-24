"""The invite-to-follow workflows against a scripted dialog.

The fake page plays the app: it answers the navigation with the dialog's
responses, ticks rows by profile id, and on Invite sends one batch built from
what is ticked through the page's routes, exactly as the browser would. Every
value is synthetic.
"""

from __future__ import annotations

import base64
import json
from typing import Any, cast

import pytest

from linkedin_mcp_server.scraping import page_invites as pi
from linkedin_mcp_server.scraping.page_invite_guard import _guard_for

COMPANY = "100000701"
API = "https://www.linkedin.com/voyager/api/"
COMMUNITY = f"urn%3Ali%3Afs_normalized_company%3A{COMPANY}"


def _encoded(kind: int, member: int) -> str:
    """An id in LinkedIn's encoded shape: header, member id, own bytes."""
    raw = bytes([0, kind, 0, 0]) + member.to_bytes(4, "big") + b"\x5a\x11\x22\x33"
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _profile(member: int) -> str:
    return _encoded(0x2A, member)


def _lead(member: int) -> str:
    return _encoded(0x2C, member)


MEMBERS = [700000 + i for i in range(25)]
IDS = [_profile(member) for member in MEMBERS]


def _urn(profile_id: str) -> str:
    return f"urn:li:fsd_profile:{profile_id}"


class _Request:
    def __init__(self, url: str, method: str = "GET", body: Any = None):
        self.url = url
        self.method = method
        self.post_data = json.dumps(body) if body is not None else None
        self.headers = {"x-restli-method": "batch_create"} if method == "POST" else {}

    async def all_headers(self) -> dict[str, str]:
        return self.headers


class _Response:
    def __init__(self, request: _Request, payload: Any, status: int = 200):
        self.url = request.url
        self.request = request
        self.status = status
        self._payload = payload

    async def json(self) -> Any:
        return self._payload


class _Route:
    def __init__(self, request: _Request):
        self.request = request
        self.passed: bool | None = None

    async def fallback(self) -> None:
        self.passed = True

    async def abort(self) -> None:
        self.passed = False


class _Locator:
    def __init__(self, page: _DialogPage):
        self._page = page

    async def click(self) -> None:
        self._page.events.append("search_box.click")


class _Keyboard:
    def __init__(self, page: _DialogPage):
        self._page = page

    async def type(self, text: str, delay: int | None = None) -> None:
        self._page.events.append(f"type:{text}")
        self._page.on_type(text)


class _DialogPage:
    """The invite dialog as far as the workflow can see it."""

    def __init__(
        self,
        *,
        rows: int = 3,
        invitable: dict[str, bool] | None = None,
        credits: int = 100,
        admin: bool = True,
        answer: tuple[int, Any] | None = (201, None),
        extra_tick: str | None = None,
    ):
        self.url = "about:blank"
        self.rows = IDS[:rows]
        self.invitable = invitable or {}
        self.credits = credits
        self.admin = admin
        self.answer = answer
        self.extra_tick = extra_tick
        self.listeners: dict[str, list[Any]] = {}
        self.routes: list[tuple[str, Any]] = []
        self.checked: list[str] = []
        self.loaded = 0
        self.events: list[str] = []
        self.sent: list[list[str]] = []
        self.keyboard = _Keyboard(self)

    # Playwright surface -------------------------------------------------
    def on(self, event: str, callback: Any) -> None:
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event: str, callback: Any) -> None:
        self.listeners[event].remove(callback)

    async def route(self, pattern: str, handler: Any) -> None:
        self.routes.append((pattern, handler))

    def locator(self, selector: str) -> _Locator:
        return _Locator(self)

    async def evaluate(self, program: str, arg: Any = None) -> Any:
        if "page-invite-state" in program:
            return {
                "open": True,
                "rows": self.rows[: self.loaded],
                "checked": list(self.checked),
                "checkedTotal": len(self.checked),
            }
        if "page-invite-tick" in program:
            self.events.append(f"tick:{arg}")
            if arg not in self.rows[: self.loaded]:
                return "not_found"
            self.checked.append(arg)
            if self.extra_tick:
                self.checked.append(self.extra_tick)
                self.extra_tick = None
            return "ticked"
        if "page-invite-press" in program:
            self.events.append("press")
            await self._send()
            return "clicked"
        if "page-invite-more" in program:
            self.events.append("more")
            if self.loaded >= len(self.rows):
                return "none"
            start = self.loaded
            self.loaded = min(len(self.rows), start + 10)
            self._emit_page(start, 10)
            return "clicked"
        raise AssertionError(f"unexpected program: {program[:60]}")

    # The app --------------------------------------------------------------
    def _emit(self, path: str, payload: Any, *, method="GET", body=None, status=200):
        request = _Request(API + path, method, body)
        for callback in list(self.listeners.get("response", [])):
            callback(_Response(request, payload, status))

    def _page_payload(self, ids: list[str], start: int) -> dict:
        return {
            "data": {
                "elements": [{"suggestedProfile": _urn(i)} for i in ids],
                "paging": {"start": start, "count": len(ids)},
            },
            "included": [
                {
                    "entityUrn": _urn(i),
                    "firstName": "Example",
                    "lastName": "Person",
                    "publicIdentifier": f"example-person-{IDS.index(i)}"
                    if i in IDS
                    else "example-dashboard",
                }
                for i in ids
            ],
        }

    def _emit_page(self, start: int, count: int) -> None:
        ids = self.rows[start : start + count]
        self._emit(
            f"voyagerRelationshipsDashCommunityInviteeSuggestions?q=community"
            f"&community={COMMUNITY}&start={start}&count={count}",
            self._page_payload(ids, start),
        )
        self._emit(
            "voyagerRelationshipsDashCommunityInviteeSuggestionStatus"
            f"?q=communityAndInvitees&community={COMMUNITY}",
            {
                "data": {
                    "elements": [
                        {
                            "suggestedProfile": _urn(i),
                            "ableToBeInvited": self.invitable.get(i, True),
                        }
                        for i in ids
                    ]
                },
                "included": [],
            },
        )

    def open_dialog(self, url: str) -> None:
        if not self.admin:
            self.url = (
                "https://www.linkedin.com/company/no-access/?idOrUniversalName="
                + COMPANY
            )
            return
        self.url = url
        # The dashboard's own invite card, which must not be mixed in.
        self._emit(
            "voyagerRelationshipsDashCommunityInviteeSuggestions?q=community"
            f"&community=urn%3Ali%3Afsd_company%3A{COMPANY}&start=0&count=5",
            self._page_payload(["ACoAAdashboard"], 0),
        )
        self._emit(
            "voyagerRelationshipsDashCommunityInviterStatistics?q=community"
            f"&community={COMMUNITY}",
            {
                "data": {
                    "elements": [
                        {
                            "remainingCredit": self.credits,
                            "initialCreditGrant": 100,
                            "nextCreditGrantOn": {"year": 2026, "month": 12, "day": 1},
                        }
                    ]
                },
                "included": [],
            },
        )
        self.loaded = min(len(self.rows), 20)
        self._emit_page(0, 20)

    def on_type(self, text: str) -> None:
        self._emit(
            "graphql?queryId=voyagerSearchDashReusableTypeahead.abc"
            f"&variables=(type:CONNECTIONS,keywords:{text},start:0,count:40,query:())",
            {
                "data": {
                    "data": {
                        "searchDashReusableTypeaheadByType": {
                            "elements": [
                                {
                                    "title": {"text": "Example Found"},
                                    "subtitle": {"text": "Example headline"},
                                    "target": {"*profile": _urn(IDS[24])},
                                }
                            ]
                        }
                    }
                },
                "included": [],
            },
        )

    async def _send(self) -> None:
        body = {
            "elements": [
                {
                    "inviteeMember": _urn(i),
                    "genericInvitationType": "ORGANIZATION",
                    "trackingId": "t",
                }
                for i in self.checked
            ]
        }
        request = _Request(API + "voyagerRelationshipsDashInvitations", "POST", body)
        route = _Route(request)
        for _pattern, handler in self.routes:
            await handler(route)
        if not route.passed:
            return
        self.sent.append(list(self.checked))
        if self.answer is None:
            return
        status, payload = self.answer
        if payload is None:
            payload = {
                "data": {"elements": [{"status": 201} for _ in self.checked]},
                "included": [],
            }
        self._emit(
            "voyagerRelationshipsDashInvitations",
            payload,
            method="POST",
            body=body,
            status=status,
        )


class _Navigator:
    def __init__(self, page: _DialogPage):
        self.page = page
        self.loads: list[str] = []

    async def _navigate_to_page(self, url: str) -> None:
        self.loads.append(url)
        self.page.open_dialog(url)


class _Session:
    def __init__(self, page: _DialogPage):
        self.page = page

    async def check_rate_limit(self) -> None:
        return None


@pytest.fixture
def writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    recorded: list[str] = []
    monkeypatch.setattr(
        pi, "record_write", lambda _store, kind, _now: recorded.append(kind)
    )
    monkeypatch.setattr(pi, "JobStore", lambda *a, **k: None)
    monkeypatch.setattr(pi, "_pacing_headroom", lambda _now: (20, None))
    monkeypatch.setattr(pi.random, "uniform", lambda _a, _b: 0)
    monkeypatch.setattr(pi, "_SEND_TIMEOUT", 0.2)
    monkeypatch.setattr(pi, "_SEARCH_SETTLE_SECONDS", 0.05)
    return recorded


def _actions(page: _DialogPage) -> tuple[pi.PageInviteActions, _Navigator]:
    navigator = _Navigator(page)
    return pi.PageInviteActions(
        cast(Any, _Session(page)), cast(Any, navigator)
    ), navigator


async def _invite(page: _DialogPage, people: list[str], *, confirm: bool) -> dict:
    actions, _ = _actions(page)
    return await actions.invite_to_follow(COMPANY, people, confirm_send=confirm)


def _statuses(result: dict) -> dict[str, str]:
    return {p["input"]: p["status"] for p in result["people"]}


async def test_the_list_reads_the_dialog_and_never_the_dashboard_card(writes):
    page = _DialogPage(rows=3, invitable={IDS[1]: False})
    actions, navigator = _actions(page)
    result = await actions.list_follow_candidates(COMPANY, limit=20)

    assert navigator.loads == [pi.invite_dialog_url(COMPANY)]
    assert [c["profileId"] for c in result["candidates"]] == IDS[:3]
    assert [c["invitable"] for c in result["candidates"]] == [True, False, True]
    assert result["credits"] == {
        "remaining": 100,
        "total": 100,
        "nextGrantOn": "2026-12-01",
    }
    assert result["paging"] == {"count": 3, "more": False, "reason": "complete"}
    assert result["candidates"][0]["profileUrl"] == (
        "https://www.linkedin.com/in/example-person-0/"
    )
    assert "tick" not in " ".join(page.events)


async def test_the_list_pages_with_show_more_up_to_the_limit(writes):
    page = _DialogPage(rows=25)
    actions, _ = _actions(page)
    result = await actions.list_follow_candidates(COMPANY, limit=22)

    assert page.events == ["more"]
    assert [c["profileId"] for c in result["candidates"]] == IDS[:22]
    assert result["paging"] == {"count": 22, "more": True, "reason": "limit"}


async def test_the_list_searches_by_name_in_the_dialog(writes):
    page = _DialogPage(rows=3)
    actions, _ = _actions(page)
    result = await actions.list_follow_candidates(COMPANY, keywords="exa")

    assert page.events == ["search_box.click", "type:exa"]
    assert [c["profileId"] for c in result["candidates"]] == [IDS[24]]
    assert result["paging"]["reason"] == "search"


async def test_a_page_the_account_does_not_administer_is_said_plainly(writes):
    page = _DialogPage(admin=False)
    actions, _ = _actions(page)
    with pytest.raises(pi.NotPageAdminError, match="not an admin of company page"):
        await actions.list_follow_candidates(COMPANY)

    result = await _invite(_DialogPage(admin=False), [IDS[0]], confirm=True)
    assert result["status"] == "not_page_admin"
    assert result["retry_safe"] is True
    assert writes == []


async def test_a_dry_run_ticks_and_sends_nothing_and_counts_credits(writes):
    page = _DialogPage(rows=3, invitable={IDS[2]: False}, credits=1)
    result = await _invite(
        page, [IDS[0], IDS[1], IDS[2], "ACoAAmissing"], confirm=False
    )

    assert page.events == []
    assert page.sent == []
    assert writes == []
    assert result["status"] == "dry_run"
    assert result["retry_safe"] is True
    assert _statuses(result) == {
        IDS[0]: "would_invite",
        IDS[1]: "no_credits",
        IDS[2]: "not_invitable",
        "ACoAAmissing": "not_invitable",
    }
    assert result["credits"]["needed"] == 1


async def test_the_pacing_cap_holds_people_back(writes, monkeypatch):
    monkeypatch.setattr(pi, "_pacing_headroom", lambda _now: (1, None))
    result = await _invite(_DialogPage(rows=3), [IDS[0], IDS[1]], confirm=False)
    assert _statuses(result) == {IDS[0]: "would_invite", IDS[1]: "pacing_cap"}


async def test_a_confirmed_invite_sends_exactly_the_people_asked(writes):
    page = _DialogPage(rows=25)
    result = await _invite(page, [IDS[21], IDS[3]], confirm=True)

    assert page.events == ["more", f"tick:{IDS[21]}", f"tick:{IDS[3]}", "press"]
    assert page.sent == [[IDS[21], IDS[3]]]
    assert result["status"] == "sent"
    assert result["retry_safe"] is False
    assert _statuses(result) == {IDS[21]: "invited", IDS[3]: "invited"}
    assert writes == [pi.PAGE_INVITES, pi.PAGE_INVITES]


async def test_an_extra_ticked_row_stops_before_invite_is_pressed(writes):
    page = _DialogPage(rows=3, extra_tick=IDS[2])
    result = await _invite(page, [IDS[0]], confirm=True)

    assert "press" not in page.events
    assert page.sent == []
    assert result["status"] == "selection_mismatch"
    assert result["retry_safe"] is True
    assert writes == []


async def test_the_guard_stops_a_batch_that_differs_from_the_plan(writes, monkeypatch):
    page = _DialogPage(rows=3)
    actions, _ = _actions(page)
    # A dialog that slips one more profile into the batch than was ticked.
    real_send = page._send

    async def send() -> None:
        page.checked.append(IDS[2])
        await real_send()

    monkeypatch.setattr(page, "_send", send)
    result = await actions.invite_to_follow(COMPANY, [IDS[0]], confirm_send=True)

    assert page.sent == []
    assert result["status"] == "blocked_by_guard"
    assert result["retry_safe"] is True
    assert writes == []


async def test_an_unanswered_send_is_unconfirmed_and_never_repeated(writes):
    page = _DialogPage(rows=3, answer=None)
    result = await _invite(page, [IDS[0], IDS[1]], confirm=True)

    assert page.events.count("press") == 1
    assert page.sent == [[IDS[0], IDS[1]]]
    assert result["status"] == "send_unconfirmed"
    assert result["retry_safe"] is False
    assert _statuses(result) == {IDS[0]: "unconfirmed", IDS[1]: "unconfirmed"}
    assert writes == [pi.PAGE_INVITES, pi.PAGE_INVITES]


async def test_per_person_answers_are_reported_in_request_order(writes):
    answer = (
        200,
        {"data": {"elements": [{"status": 201}, {"status": 422}]}, "included": []},
    )
    page = _DialogPage(rows=3, answer=answer)
    result = await _invite(page, [IDS[0], IDS[1]], confirm=True)

    assert result["status"] == "sent"
    assert _statuses(result) == {IDS[0]: "invited", IDS[1]: "refused"}


async def test_a_refused_batch_is_reported_as_refused(writes):
    page = _DialogPage(rows=3, answer=(400, {"status": 400}))
    result = await _invite(page, [IDS[0]], confirm=True)

    assert result["status"] == "refused_by_linkedin"
    assert result["retry_safe"] is False
    assert _statuses(result) == {IDS[0]: "refused"}


async def test_no_credits_left_sends_nothing(writes):
    page = _DialogPage(rows=3, credits=0)
    result = await _invite(page, [IDS[0]], confirm=True)

    assert page.events == []
    assert result["status"] == "nothing_to_invite"
    assert _statuses(result) == {IDS[0]: "no_credits"}


async def test_the_guard_holds_only_during_the_call(writes):
    page = _DialogPage(rows=3)
    await _invite(page, [IDS[0]], confirm=False)
    assert _guard_for(page).holders == 0


async def test_every_identifier_form_resolves_to_the_same_dialog_row(writes):
    member = MEMBERS[21]
    forms = [
        IDS[21],
        f"urn:li:fsd_profile:{IDS[21]}",
        _lead(member),
        f"urn:li:fs_salesProfile:({_lead(member)},NAME_SEARCH,abc)",
        f"https://www.linkedin.com/sales/lead/{_lead(member)},NAME_SEARCH,abc",
        f"urn:li:member:{member}",
        "https://www.linkedin.com/in/example-person-21/",
        "Example-Person-21",
    ]
    for form in forms:
        result = await _invite(_DialogPage(rows=25), [form], confirm=False)
        (person,) = result["people"]
        assert person["profileId"] == IDS[21], form
        assert person["status"] == "would_invite", form


async def test_a_name_never_selects_anyone(writes):
    # The dialog rows are all named "Example Person"; a name is not an id.
    with pytest.raises(pi.InvalidReferenceError):
        await _invite(_DialogPage(rows=3), ["Example Person"], confirm=False)
    result = await _invite(_DialogPage(rows=3), ["example-person"], confirm=False)
    (person,) = result["people"]
    assert person["status"] == "not_invitable"
    assert "profileId" not in person


async def test_two_forms_of_one_person_invite_them_once(writes):
    page = _DialogPage(rows=3)
    result = await _invite(page, [IDS[1], f"urn:li:member:{MEMBERS[1]}"], confirm=True)
    assert page.sent == [[IDS[1]]]
    assert [p["status"] for p in result["people"]] == ["invited", "duplicate"]


async def test_who_cannot_be_invited_is_said_per_person_not_for_the_batch(writes):
    page = _DialogPage(rows=3, invitable={IDS[0]: False})
    result = await _invite(
        page, [IDS[0], f"urn:li:member:{MEMBERS[24]}", IDS[1]], confirm=True
    )
    assert result["status"] == "sent"
    assert page.sent == [[IDS[1]]]
    details = [(p["status"], p.get("detail")) for p in result["people"]]
    assert details == [
        ("not_invitable", "linkedin_declined"),
        ("not_invitable", "not_a_candidate"),
        ("invited", None),
    ]


async def test_a_list_not_read_to_its_end_says_so(writes, monkeypatch):
    monkeypatch.setattr(pi, "MAX_INVITE_CANDIDATES", 20)
    result = await _invite(
        _DialogPage(rows=25), [f"urn:li:member:{MEMBERS[24]}"], confirm=False
    )
    (person,) = result["people"]
    assert person["status"] == "not_invitable"
    assert person["detail"] == "not_in_candidates_read (20 read, max_pages)"
