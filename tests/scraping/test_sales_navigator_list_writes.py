"""The account-list writes: what is sent, when nothing is, and how answers read.

The page is a fake whose ``evaluate`` runs the write through the real guard's
route handler, so a request the guard refuses never reaches the scripted
Sales Navigator answer, exactly as an aborted request never reaches LinkedIn.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import pytest

from linkedin_mcp_server import pacing
from linkedin_mcp_server.pacing import SALES_LIST_WRITES, JobStore
from linkedin_mcp_server.scraping import sales_navigator_list_writes as writes

LIST = "100000301"
NAME = "Example list"
HEADERS = {
    "csrf-token": "ajax:0000000000000000000",
    "x-restli-protocol-version": "2.0.0",
}


class _Request:
    def __init__(self, url: str, method: str, body: str | None, headers: dict):
        self.url = url
        self.method = method
        self.post_data = body
        self._headers = headers

    async def all_headers(self) -> dict[str, str]:
        return self._headers


class _Route:
    def __init__(self, request: _Request):
        self.request = request
        self.passed: bool | None = None

    async def fallback(self) -> None:
        self.passed = True

    async def abort(self) -> None:
        self.passed = False


class _Page:
    """Routes the writer's fetch through the guard, then answers it."""

    def __init__(self, answers: list[dict[str, Any]]):
        self.routes: list[Any] = []
        self.answers = list(answers)
        self.sent: list[dict[str, Any]] = []
        self.tamper: Any = None

    async def route(self, pattern: str, handler: Any) -> None:
        self.routes.append(handler)

    async def evaluate(self, _script: str, arg: dict[str, Any]) -> Any:
        if self.tamper is not None:
            arg = self.tamper(arg)
        request = _Request(arg["url"], arg["method"], arg["body"], arg["headers"])
        route = _Route(request)
        await self.routes[0](route)
        if not route.passed:
            return {"status": 0, "payload": None, "restliId": None}
        self.sent.append(arg)
        return self.answers.pop(0)


class _Session:
    def __init__(self, page: _Page):
        self.page = page


class _Reader:
    def __init__(self, page: _Page):
        self._session = _Session(page)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "jobs")
    monkeypatch.setattr(writes, "JobStore", lambda *a, **k: store)
    return store


def _writer(
    page: _Page,
    monkeypatch,
    *,
    record: dict[str, Any] | None = None,
    headers: dict[str, str] | None = HEADERS,
):
    writer = writes.SalesNavigatorListWriter(_Reader(page))
    opened: list[str] = []

    async def _open(url: str, *, list_id: str | None):
        opened.append(url)
        return headers, record

    monkeypatch.setattr(writer, "_open", _open)
    return writer, opened


def _account_list(**extra: Any) -> dict[str, Any]:
    return {"id": LIST, "name": NAME, "listType": "ACCOUNT", "entityCount": 2, **extra}


def _spent(store: JobStore) -> int:
    budget = pacing.load_account_budget(store, datetime.now(timezone.utc))
    return budget.ledger.spent_kind(
        SALES_LIST_WRITES, datetime.now(timezone.utc), 24 * 3600
    )


def _saved(*entries: tuple[str, str]) -> dict[str, Any]:
    return {
        "status": 200,
        "payload": {
            "value": {
                "successful": [],
                "failed": [],
                "listEntityActionResponses": [
                    {"status": status, "entity": f"urn:li:fs_salesCompany:{company}"}
                    for company, status in entries
                ],
            }
        },
        "restliId": None,
    }


def _body(sent: dict[str, Any]) -> Any:
    return json.loads(sent["body"]) if sent["body"] else None


def _path(sent: dict[str, Any]) -> str:
    parsed = urlparse(sent["url"])
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


# Delete


async def test_delete_sends_only_the_exact_list_after_the_name_matches(
    ledger, monkeypatch
):
    page = _Page([{"status": 204, "payload": None, "restliId": None}])
    writer, _ = _writer(page, monkeypatch, record=_account_list())
    result = await writer.delete_account_list(
        LIST, expected_name=NAME, confirm_send=True
    )
    assert result["status"] == "deleted"
    assert result["retry_safe"] is False
    assert [(s["method"], _path(s)) for s in page.sent] == [
        (
            "DELETE",
            f"/sales-api/salesApiLists/{LIST}"
            "?unsaveEntity=false&doUnsaveLeadsUnderAccount=false",
        )
    ]
    assert _spent(ledger) == 1


@pytest.mark.parametrize(
    "expected", ["Example list ", "example list", "Example", "Example list 2"]
)
async def test_delete_refuses_a_name_that_is_not_exactly_the_lists(
    ledger, monkeypatch, expected
):
    page = _Page([{"status": 204, "payload": None, "restliId": None}])
    writer, _ = _writer(page, monkeypatch, record=_account_list())
    result = await writer.delete_account_list(
        LIST, expected_name=expected, confirm_send=True
    )
    assert result["status"] == "name_mismatch"
    assert result["retry_safe"] is True
    assert page.sent == []
    assert _spent(ledger) == 0


async def test_delete_refuses_a_lead_list(ledger, monkeypatch):
    page = _Page([])
    writer, _ = _writer(page, monkeypatch, record=_account_list(listType="LEAD"))
    result = await writer.delete_account_list(
        LIST, expected_name=NAME, confirm_send=True
    )
    assert result["status"] == "wrong_list"
    assert page.sent == []


async def test_a_delete_dry_run_sends_nothing(ledger, monkeypatch):
    page = _Page([])
    writer, opened = _writer(page, monkeypatch, record=_account_list())
    result = await writer.delete_account_list(
        LIST, expected_name=NAME, confirm_send=False
    )
    assert result["status"] == "dry_run"
    assert result["dry_run"] is True
    assert result["list"]["name"] == NAME
    assert page.sent == []
    assert len(opened) == 1
    assert _spent(ledger) == 0


@pytest.mark.parametrize(
    ("answer", "status"),
    [
        ({"status": 0, "payload": None, "restliId": None}, "delete_unconfirmed"),
        ({"status": 502, "payload": None, "restliId": None}, "delete_unconfirmed"),
        ({"status": 429, "payload": None, "restliId": None}, "delete_unconfirmed"),
        ({"status": 403, "payload": None, "restliId": None}, "refused_by_linkedin"),
    ],
)
async def test_an_answer_that_is_not_success_is_never_reported_as_one(
    ledger, monkeypatch, answer, status
):
    page = _Page([answer])
    writer, _ = _writer(page, monkeypatch, record=_account_list())
    result = await writer.delete_account_list(
        LIST, expected_name=NAME, confirm_send=True
    )
    assert result["status"] == status
    assert result["retry_safe"] is False
    assert len(page.sent) == 1
    assert _spent(ledger) == 1


# Pacing


async def test_a_cooldown_stops_the_write_before_it_is_sent(ledger, monkeypatch):
    until = datetime.now(timezone.utc) + timedelta(minutes=30)
    monkeypatch.setattr(writes, "cooldown_resume_at", lambda store, now: until)
    page = _Page([])
    writer, _ = _writer(page, monkeypatch, record=_account_list())
    result = await writer.delete_account_list(
        LIST, expected_name=NAME, confirm_send=True
    )
    assert result["status"] == "not_sent"
    assert result["retry_safe"] is True
    assert "paused" in result["message"]
    assert page.sent == []


async def test_the_daily_cap_stops_the_write_before_it_is_sent(ledger, monkeypatch):
    monkeypatch.setenv("SALES_LIST_WRITES_MAX", "1")
    pacing.record_write(ledger, SALES_LIST_WRITES, datetime.now(timezone.utc))
    page = _Page([])
    writer, _ = _writer(page, monkeypatch, record=_account_list())
    result = await writer.add_accounts(LIST, ["100000001"], confirm_send=True)
    assert result["status"] == "not_sent"
    assert result["accounts"][0]["status"] == "not_sent"
    assert page.sent == []


async def test_no_app_headers_means_nothing_is_sent(ledger, monkeypatch):
    page = _Page([])
    writer, _ = _writer(page, monkeypatch, record=_account_list(), headers=None)
    result = await writer.add_accounts(LIST, ["100000001"], confirm_send=True)
    assert result["status"] == "not_sent"
    assert page.sent == []


# Guard


async def test_a_request_that_drifted_from_the_plan_is_stopped_in_the_browser(
    ledger, monkeypatch
):
    page = _Page([{"status": 200, "payload": {}, "restliId": None}])

    def other_list(arg: dict[str, Any]) -> dict[str, Any]:
        return {**arg, "body": arg["body"].replace(LIST, "100000302")}

    page.tamper = other_list
    writer, _ = _writer(page, monkeypatch, record=_account_list())
    result = await writer.add_accounts(LIST, ["100000001"], confirm_send=True)
    assert result["status"] == "not_sent"
    assert "body differs from the plan" in result["message"]
    assert result["retry_safe"] is True
    assert page.sent == []
    assert _spent(ledger) == 0


# Add


async def test_add_sends_one_request_with_exactly_the_planned_accounts(
    ledger, monkeypatch
):
    page = _Page([_saved(("100000001", "SUCCESS"), ("100000002", "SUCCESS"))])
    writer, _ = _writer(page, monkeypatch, record=_account_list())
    result = await writer.add_accounts(
        LIST, ["100000001", "100000002"], confirm_send=True
    )
    assert result["status"] == "added"
    assert [a["status"] for a in result["accounts"]] == ["added", "added"]
    (sent,) = page.sent
    assert sent["method"] == "POST"
    assert _body(sent) == {"companyIds": [100000001, 100000002], "lists": [LIST]}
    assert _path(sent) == "/sales-api/salesApiCompanies?action=save"
    assert _spent(ledger) == 1


async def test_an_add_dry_run_reports_each_account_and_sends_nothing(
    ledger, monkeypatch
):
    page = _Page([])
    writer, _ = _writer(page, monkeypatch, record=_account_list())
    result = await writer.add_accounts(LIST, ["100000001"], confirm_send=False)
    assert result["status"] == "dry_run"
    assert result["accounts"] == [
        {"input": "100000001", "id": "100000001", "status": "would_add"}
    ]
    assert page.sent == []


async def test_add_refuses_a_list_that_is_not_an_account_list(ledger, monkeypatch):
    page = _Page([])
    writer, _ = _writer(page, monkeypatch, record=_account_list(listType="LEAD"))
    result = await writer.add_accounts(LIST, ["100000001"], confirm_send=True)
    assert result["status"] == "wrong_list"
    assert page.sent == []


async def test_an_add_without_an_answer_is_unconfirmed(ledger, monkeypatch):
    page = _Page([{"status": 0, "payload": None, "restliId": None}])
    writer, _ = _writer(page, monkeypatch, record=_account_list())
    result = await writer.add_accounts(LIST, ["100000001"], confirm_send=True)
    assert result["status"] == "add_unconfirmed"
    assert result["retry_safe"] is False
    assert result["accounts"][0]["status"] == "unconfirmed"


# Create


async def test_a_create_dry_run_touches_no_page(ledger, monkeypatch):
    page = _Page([])
    writer, opened = _writer(page, monkeypatch)
    result = await writer.create_account_list(
        NAME, accounts=["100000001"], confirm_send=False
    )
    assert result["status"] == "dry_run"
    assert result["list"] == {"name": NAME}
    assert result["accounts"][0]["status"] == "would_add"
    assert opened == []
    assert page.sent == []


async def test_create_then_add_to_the_id_sales_navigator_answered(ledger, monkeypatch):
    page = _Page(
        [
            {"status": 201, "payload": {"id": 7530000000000000399}, "restliId": None},
            _saved(("100000001", "SUCCESS")),
        ]
    )
    writer, _ = _writer(page, monkeypatch)
    result = await writer.create_account_list(
        NAME, description=" Example ", accounts=["100000001"], confirm_send=True
    )
    assert result["status"] == "created"
    assert result["list"]["id"] == "7530000000000000399"
    assert result["accounts"][0]["status"] == "added"
    create, add = page.sent
    assert _body(create) == {
        "name": NAME,
        "listType": "ACCOUNT",
        "role": "OWNER",
        "description": "Example",
    }
    assert _body(add)["lists"] == ["7530000000000000399"]
    assert _spent(ledger) == 2


async def test_a_create_whose_answer_names_no_list_is_unconfirmed(ledger, monkeypatch):
    page = _Page([{"status": 201, "payload": {}, "restliId": None}])
    writer, _ = _writer(page, monkeypatch)
    result = await writer.create_account_list(
        NAME, accounts=["100000001"], confirm_send=True
    )
    assert result["status"] == "create_unconfirmed"
    assert result["retry_safe"] is False
    assert result["accounts"][0]["status"] == "not_sent"
    assert len(page.sent) == 1


async def test_a_failed_add_after_a_create_still_says_the_list_exists(
    ledger, monkeypatch
):
    page = _Page(
        [
            {"status": 201, "payload": None, "restliId": "100000399"},
            {
                "status": 400,
                "payload": {"status": 400, "message": "Invalid input"},
                "restliId": None,
            },
        ]
    )
    writer, _ = _writer(page, monkeypatch)
    result = await writer.create_account_list(
        NAME, accounts=["100000001"], confirm_send=True
    )
    assert result["status"] == "refused_by_linkedin"
    assert result["list"]["id"] == "100000399"
    assert result["message"].startswith("Sales Navigator created the list.")
    assert result["accounts"][0]["detail"] == (
        "Sales Navigator refused it (HTTP 400: Invalid input)"
    )
    assert result["retry_safe"] is False


# Planning


def test_accounts_are_normalized_and_deduplicated_in_order():
    plan = writes.plan_accounts(
        [
            "100000001",
            "urn:li:fs_salesCompany:100000001",
            "https://www.linkedin.com/sales/company/100000002",
        ]
    )
    assert plan.ids == ["100000001", "100000002"]
    assert [row["status"] for row in plan.rows] == ["planned", "duplicate", "planned"]
    assert plan.invalid is None


def test_an_unrecognised_account_or_too_many_refuse_the_call():
    assert writes.plan_accounts(["acme"]).invalid is not None
    too_many = [str(100000001 + i) for i in range(writes.MAX_ACCOUNTS_PER_CALL + 1)]
    assert "At most" in (writes.plan_accounts(too_many).invalid or "")


async def test_each_account_takes_the_status_sales_navigator_gave_it(
    ledger, monkeypatch
):
    page = _Page(
        [
            _saved(
                ("100000001", "SUCCESS"),
                ("100000002", "CONFLICT"),
                ("100000003", "EXCEED_LIMIT"),
            )
        ]
    )
    writer, _ = _writer(page, monkeypatch, record=_account_list())
    result = await writer.add_accounts(
        LIST, ["100000001", "100000002", "100000003"], confirm_send=True
    )
    assert result["status"] == "partly_added"
    assert [(a["status"], a.get("detail")) for a in result["accounts"]] == [
        ("added", None),
        ("already_in_list", None),
        ("refused", "EXCEED_LIMIT"),
    ]


async def test_an_account_the_answer_does_not_name_is_never_added(ledger, monkeypatch):
    page = _Page([_saved(("100000001", "SUCCESS"))])
    writer, _ = _writer(page, monkeypatch, record=_account_list())
    result = await writer.add_accounts(
        LIST, ["100000001", "100000002"], confirm_send=True
    )
    assert result["status"] == "add_unconfirmed"
    assert result["retry_safe"] is False
    assert [a["status"] for a in result["accounts"]] == ["added", "unconfirmed"]


async def test_a_2xx_without_a_readable_answer_is_unconfirmed(ledger, monkeypatch):
    page = _Page([{"status": 200, "payload": {}, "restliId": None}])
    writer, _ = _writer(page, monkeypatch, record=_account_list())
    result = await writer.add_accounts(LIST, ["100000001"], confirm_send=True)
    assert result["status"] == "add_unconfirmed"
    assert result["accounts"][0]["status"] == "unconfirmed"
