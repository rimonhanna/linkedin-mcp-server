"""The account-list writes: what is sent, when nothing is, and how answers read.

The page is a fake that runs every request through the real guard's route
handler, so a request the guard refuses never reaches the scripted Sales
Navigator answer, exactly as an aborted request never reaches LinkedIn. An add
or a delete is the writer's own fetch (``evaluate``); a create is the app's
request, sent when the fake form's Create button is pressed, from what was
typed into it.
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
CREATE_URL = "https://www.linkedin.com/sales-api/salesApiLists"


class _Response:
    def __init__(self, answer: dict[str, Any]):
        self.status = answer["status"]
        self._text = _text(answer)
        self._restli_id = answer.get("restliId")

    async def text(self) -> str:
        return self._text or ""

    async def header_value(self, name: str) -> str | None:
        return self._restli_id if name == "x-restli-id" else None


class _Request:
    def __init__(self, url: str, method: str, body: str | None, headers: dict):
        self.url = url
        self.method = method
        self.post_data = body
        self._headers = headers
        self.answer: dict[str, Any] | None = None

    async def all_headers(self) -> dict[str, str]:
        return self._headers

    async def response(self) -> _Response | None:
        return None if self.answer is None else _Response(self.answer)


class _Route:
    def __init__(self, request: _Request):
        self.request = request
        self.passed: bool | None = None

    async def fallback(self) -> None:
        self.passed = True

    async def abort(self) -> None:
        self.passed = False


def _text(answer: dict[str, Any]) -> str | None:
    if "text" in answer:
        return answer["text"]
    payload = answer.get("payload")
    return None if payload is None else json.dumps(payload)


class _Keyboard:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    async def press(self, key: str) -> None:
        self.pressed.append(key)


class _Locator:
    def __init__(self, page: _Page, selector: str):
        self._page = page
        self._selector = selector

    @property
    def first(self) -> _Locator:
        return self

    async def count(self) -> int:
        return 0 if self._selector in self._page.missing else 1

    async def wait_for(self, *, state: str, timeout: float) -> None:
        if self._selector in self._page.missing or not self._page.form_open:
            raise TimeoutError(self._selector)

    async def click(self) -> None:
        if self._selector == writes._CREATE_OPENER:
            self._page.form_open = True
        elif self._selector == writes._CREATE_BUTTON:
            self._page.submits += 1
            await self._page.app_creates()

    async def press_sequentially(self, text: str, *, delay: float) -> None:
        self._page.values[self._selector] = (
            self._page.values.get(self._selector, "") + text
        )
        if self._page.typing is not None:
            self._page.values[self._selector] = self._page.typing(text)

    async def input_value(self) -> str:
        return self._page.values.get(self._selector, "")

    async def is_enabled(self) -> bool:
        return bool(self._page.values.get(writes._NAME_FIELD))


class _Page:
    """Routes every write through the guard, then answers it."""

    def __init__(self, answers: list[dict[str, Any]]):
        self.routes: list[Any] = []
        self.answers = list(answers)
        self.sent: list[dict[str, Any]] = []
        self.tamper: Any = None
        self.app_body: Any = None
        self.typing: Any = None
        self.missing: set[str] = set()
        self.form_open = False
        self.submits = 0
        self.values: dict[str, str] = {}
        self.keyboard = _Keyboard()
        self.raise_after_admit: BaseException | None = None

    async def route(self, pattern: str, handler: Any) -> None:
        self.routes.append(handler)

    def on(self, event: str, handler: Any) -> None:
        pass

    def remove_listener(self, event: str, handler: Any) -> None:
        pass

    def locator(self, selector: str) -> _Locator:
        return _Locator(self, selector)

    async def _through_guard(self, arg: dict[str, Any]) -> _Request | None:
        request = _Request(arg["url"], arg["method"], arg["body"], arg["headers"])
        route = _Route(request)
        await self.routes[0](route)
        if not route.passed:
            return None
        self.sent.append(arg)
        if self.raise_after_admit is not None:
            raise self.raise_after_admit
        request.answer = self.answers.pop(0)
        return request

    async def evaluate(self, _script: str, arg: dict[str, Any]) -> Any:
        if self.tamper is not None:
            arg = self.tamper(arg)
        request = await self._through_guard(arg)
        if request is None or request.answer is None:
            return {"status": 0, "text": None, "restliId": None}
        answer = request.answer
        return {
            "status": answer["status"],
            "text": _text(answer),
            "restliId": answer.get("restliId"),
        }

    async def app_creates(self) -> None:
        """What the app does on Create: post the form as it holds it."""
        body: dict[str, Any] = {
            "name": self.values.get(writes._NAME_FIELD, ""),
            "listType": "ACCOUNT",
            "role": "OWNER",
        }
        if self.values.get(writes._DESCRIPTION_FIELD):
            body["description"] = self.values[writes._DESCRIPTION_FIELD]
        if self.app_body is not None:
            body = self.app_body(body)
        await self._through_guard(
            {
                "url": CREATE_URL,
                "method": "POST",
                "body": json.dumps(body),
                "headers": {},
            }
        )


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
    record: Any = None,
    headers: dict[str, str] | None = HEADERS,
):
    """A writer whose page loads return ``record`` (or ``record(list_id)``)."""
    writer = writes.SalesNavigatorListWriter(_Reader(page))
    opened: list[str] = []

    async def _open(url: str, *, list_id: str | None):
        opened.append(url)
        if list_id is None:
            return headers, None
        return headers, record(list_id) if callable(record) else record

    monkeypatch.setattr(writer, "_open", _open)
    return writer, opened


def _rows(*values: str) -> list[dict[str, Any]]:
    return writes.plan_accounts(list(values)).rows


def _created(list_id: str, name: str = NAME) -> dict[str, Any]:
    return {"id": list_id, "name": name, "listType": "ACCOUNT", "entityCount": 0}


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
    result = await writer.add_accounts(LIST, _rows("100000001"), confirm_send=True)
    assert result["status"] == "not_sent"
    assert result["accounts"][0]["status"] == "not_sent"
    assert page.sent == []


async def test_no_app_headers_means_nothing_is_sent(ledger, monkeypatch):
    page = _Page([])
    writer, _ = _writer(page, monkeypatch, record=_account_list(), headers=None)
    result = await writer.add_accounts(LIST, _rows("100000001"), confirm_send=True)
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
    result = await writer.add_accounts(LIST, _rows("100000001"), confirm_send=True)
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
        LIST, _rows("100000001", "100000002"), confirm_send=True
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
    result = await writer.add_accounts(LIST, _rows("100000001"), confirm_send=False)
    assert result["status"] == "dry_run"
    assert result["accounts"] == [
        {"input": "100000001", "id": "100000001", "status": "would_add"}
    ]
    assert page.sent == []


async def test_add_refuses_a_list_that_is_not_an_account_list(ledger, monkeypatch):
    page = _Page([])
    writer, _ = _writer(page, monkeypatch, record=_account_list(listType="LEAD"))
    result = await writer.add_accounts(LIST, _rows("100000001"), confirm_send=True)
    assert result["status"] == "wrong_list"
    assert page.sent == []


async def test_an_add_without_an_answer_is_unconfirmed(ledger, monkeypatch):
    page = _Page([{"status": 0, "payload": None, "restliId": None}])
    writer, _ = _writer(page, monkeypatch, record=_account_list())
    result = await writer.add_accounts(LIST, _rows("100000001"), confirm_send=True)
    assert result["status"] == "add_unconfirmed"
    assert result["retry_safe"] is False
    assert result["accounts"][0]["status"] == "unconfirmed"


# Create


async def test_a_create_dry_run_touches_no_page(ledger, monkeypatch):
    page = _Page([])
    writer, opened = _writer(page, monkeypatch)
    result = await writer.create_account_list(
        NAME, accounts=_rows("100000001"), confirm_send=False
    )
    assert result["status"] == "dry_run"
    assert result["list"] == {"name": NAME}
    assert result["accounts"][0]["status"] == "would_add"
    assert opened == []
    assert page.sent == []


async def test_a_create_is_the_apps_own_request_from_its_form_then_read_back(
    ledger, monkeypatch
):
    page = _Page(
        [
            {"status": 201, "payload": {"id": 7530000000000000399}, "restliId": None},
            _saved(("100000001", "SUCCESS")),
        ]
    )
    writer, opened = _writer(page, monkeypatch, record=_created)
    result = await writer.create_account_list(
        NAME, description=" Example ", accounts=_rows("100000001"), confirm_send=True
    )
    assert result["status"] == "created"
    assert result["accounts_status"] == "added"
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
    assert opened[1].endswith("listId=7530000000000000399")
    assert _spent(ledger) == 2


async def test_a_created_lists_id_is_read_exactly_from_the_answers_text(
    ledger, monkeypatch
):
    # 7530989538853441537 is no double: a JavaScript number rounds it.
    page = _Page(
        [{"status": 201, "text": '{"id":7530989538853441537}', "restliId": None}]
    )
    writer, _ = _writer(page, monkeypatch, record=_created)
    result = await writer.create_account_list(NAME, confirm_send=True)
    assert result["list"]["id"] == "7530989538853441537", (
        "the created list's id must be the exact digits of the answer's text"
    )
    assert result["status"] == "created"


async def test_a_created_list_is_never_reported_safe_to_retry(ledger, monkeypatch):
    page = _Page([{"status": 201, "payload": None, "restliId": "100000399"}])
    writer, _ = _writer(page, monkeypatch, record=_created)
    result = await writer.create_account_list(NAME, confirm_send=True)
    assert result["status"] == "created"
    assert result["retry_safe"] is False, (
        "a created list must report retry_safe false: a repeat creates it twice"
    )
    assert "accounts_status" not in result


async def test_a_create_whose_answer_names_no_list_is_unconfirmed(ledger, monkeypatch):
    page = _Page([{"status": 201, "payload": {}, "restliId": None}])
    writer, opened = _writer(page, monkeypatch, record=_created)
    result = await writer.create_account_list(
        NAME, accounts=_rows("100000001"), confirm_send=True
    )
    assert result["status"] == "create_unconfirmed"
    assert result["retry_safe"] is False
    assert result["accounts"][0]["status"] == "not_sent"
    assert len(page.sent) == 1
    assert len(opened) == 1


async def test_a_create_the_lists_page_does_not_show_is_unconfirmed(
    ledger, monkeypatch
):
    page = _Page([{"status": 201, "payload": None, "restliId": "100000399"}])
    writer, _ = _writer(
        page, monkeypatch, record=lambda list_id: _created(list_id, "Other list")
    )
    result = await writer.create_account_list(
        NAME, accounts=_rows("100000001"), confirm_send=True
    )
    assert result["status"] == "create_unconfirmed"
    assert result["list"]["id"] == "100000399"
    assert result["retry_safe"] is False
    assert len(page.sent) == 1


async def test_a_failed_add_after_a_create_still_reports_the_list_created(
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
    writer, _ = _writer(page, monkeypatch, record=_created)
    result = await writer.create_account_list(
        NAME, accounts=_rows("100000001"), confirm_send=True
    )
    assert result["status"] == "created", (
        "a create whose add failed must still say created, the list exists"
    )
    assert result["accounts_status"] == "refused_by_linkedin"
    assert result["list"]["id"] == "100000399"
    assert result["message"].startswith("Sales Navigator created the list.")
    assert result["accounts"][0]["detail"] == (
        "Sales Navigator refused it (HTTP 400: Invalid input)"
    )
    assert result["retry_safe"] is False


@pytest.mark.parametrize(
    "missing", [writes._CREATE_OPENER, writes._NAME_FIELD], ids=["button", "form"]
)
async def test_no_create_form_means_nothing_is_sent(ledger, monkeypatch, missing):
    page = _Page([])
    page.missing.add(missing)
    writer, _ = _writer(page, monkeypatch, record=_created)
    result = await writer.create_account_list(
        NAME, accounts=_rows("100000001"), confirm_send=True
    )
    assert result["status"] == "not_sent"
    assert result["retry_safe"] is True
    assert result["accounts"][0]["status"] == "not_sent"
    assert page.sent == []
    assert _spent(ledger) == 0


async def test_a_form_that_holds_other_text_is_never_submitted(ledger, monkeypatch):
    page = _Page([])
    page.typing = lambda text: text[:-1]
    writer, _ = _writer(page, monkeypatch, record=_created)
    result = await writer.create_account_list(NAME, confirm_send=True)
    assert page.submits == 0, "a form holding other text must never be submitted"
    assert result["status"] == "not_sent"
    assert page.keyboard.pressed == ["Escape"]
    assert page.sent == []


async def test_an_app_request_other_than_the_plan_is_stopped_in_the_browser(
    ledger, monkeypatch
):
    page = _Page([{"status": 201, "payload": None, "restliId": "100000399"}])
    page.app_body = lambda body: {**body, "listType": "LEAD"}
    writer, _ = _writer(page, monkeypatch, record=_created)
    result = await writer.create_account_list(NAME, confirm_send=True)
    assert result["status"] == "not_sent", (
        "the app's create request must be stopped unless it is exactly the plan"
    )
    assert "body differs from the plan" in result["message"]
    assert result["retry_safe"] is True
    assert page.sent == []
    assert _spent(ledger) == 0


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
        LIST, _rows("100000001", "100000002", "100000003"), confirm_send=True
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
        LIST, _rows("100000001", "100000002"), confirm_send=True
    )
    assert result["status"] == "add_unconfirmed"
    assert result["retry_safe"] is False
    assert [a["status"] for a in result["accounts"]] == ["added", "unconfirmed"]


async def test_a_2xx_without_a_readable_answer_is_unconfirmed(ledger, monkeypatch):
    page = _Page([{"status": 200, "payload": {}, "restliId": None}])
    writer, _ = _writer(page, monkeypatch, record=_account_list())
    result = await writer.add_accounts(LIST, _rows("100000001"), confirm_send=True)
    assert result["status"] == "add_unconfirmed"
    assert result["accounts"][0]["status"] == "unconfirmed"


async def test_a_failed_urn_is_refused_with_the_answers_status():
    statuses = writes.add_statuses(
        {
            "value": {
                "successful": [],
                "failed": ["urn:li:fs_salesCompany:100000001"],
                "listEntityActionResponses": [],
                "status": "FORBIDDEN",
            }
        },
        ["100000001", "100000002"],
    )
    assert statuses == {
        "100000001": ("refused", "FORBIDDEN"),
        "100000002": ("unconfirmed", None),
    }, "an account in failed[] must be refused with the answer's status"


async def test_each_row_keeps_the_callers_input_and_its_duplicates(ledger, monkeypatch):
    page = _Page([_saved(("100000001", "SUCCESS"))])
    writer, _ = _writer(page, monkeypatch, record=_account_list())
    result = await writer.add_accounts(
        LIST,
        _rows("urn:li:fs_salesCompany:100000001", "100000001"),
        confirm_send=True,
    )
    assert result["accounts"] == [
        {
            "input": "urn:li:fs_salesCompany:100000001",
            "id": "100000001",
            "status": "added",
        },
        {"input": "100000001", "id": "100000001", "status": "duplicate"},
    ]
    assert _body(page.sent[0])["companyIds"] == [100000001]


# Errors after a request left, at the tool layer


async def _tool(name: str):
    from typing import cast

    from fastmcp import FastMCP
    from fastmcp.tools import FunctionTool

    from linkedin_mcp_server.tools.sales_navigator_lists import (
        register_sales_navigator_list_tools,
    )

    mcp = FastMCP("test")
    register_sales_navigator_list_tools(mcp)
    tool = await mcp.get_tool(name)
    assert tool is not None
    return cast(FunctionTool, tool).fn


class _Extractor:
    def __init__(self, writer: writes.SalesNavigatorListWriter):
        self.create_sales_navigator_account_list = writer.create_account_list
        self.add_accounts_to_sales_navigator_list = writer.add_accounts
        self.delete_sales_navigator_account_list = writer.delete_account_list


@pytest.fixture
def tool_ledger(ledger, monkeypatch):
    from linkedin_mcp_server.tools import sales_navigator_lists as tools

    monkeypatch.setattr(tools, "JobStore", lambda *a, **k: ledger)
    return ledger


async def test_an_error_after_an_add_left_is_unconfirmed_never_a_tool_error(
    tool_ledger, monkeypatch, mock_context
):
    page = _Page([_saved(("100000001", "SUCCESS"))])
    writer, _ = _writer(page, monkeypatch, record=_account_list())

    def broken(*_args: Any) -> Any:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(writes, "add_statuses", broken)
    fn = await _tool("add_accounts_to_sales_navigator_list")
    result = await fn(
        LIST, ["100000001"], True, mock_context, extractor=_Extractor(writer)
    )
    assert result["status"] == "add_unconfirmed", (
        "an error after an admitted add must be add_unconfirmed, not a tool error"
    )
    assert result["retry_safe"] is False
    assert result["accounts"][0]["status"] == "unconfirmed"
    assert _spent(tool_ledger) == 1


async def test_an_error_reading_a_created_list_back_is_create_unconfirmed(
    tool_ledger, monkeypatch, mock_context
):
    page = _Page([{"status": 201, "payload": None, "restliId": "100000399"}])

    def unreadable(list_id: str) -> Any:
        raise RuntimeError("synthetic")

    writer, _ = _writer(page, monkeypatch, record=unreadable)
    fn = await _tool("create_sales_navigator_account_list")
    result = await fn(
        NAME, True, mock_context, accounts=["100000001"], extractor=_Extractor(writer)
    )
    assert result["status"] == "create_unconfirmed"
    assert result["list"]["id"] == "100000399"
    assert result["accounts"][0]["status"] == "not_sent"
    assert result["retry_safe"] is False
    assert _spent(tool_ledger) == 1


async def test_an_error_after_the_add_of_a_created_list_keeps_it_created(
    tool_ledger, monkeypatch, mock_context
):
    page = _Page(
        [
            {"status": 201, "payload": None, "restliId": "100000399"},
            _saved(("100000001", "SUCCESS")),
        ]
    )
    writer, _ = _writer(page, monkeypatch, record=_created)

    def broken(*_args: Any) -> Any:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(writes, "add_statuses", broken)
    fn = await _tool("create_sales_navigator_account_list")
    result = await fn(
        NAME, True, mock_context, accounts=["100000001"], extractor=_Extractor(writer)
    )
    assert result["status"] == "created"
    assert result["accounts_status"] == "add_unconfirmed"
    assert result["accounts"][0]["status"] == "unconfirmed"
    assert result["retry_safe"] is False
    assert _spent(tool_ledger) == 2


async def test_a_cancellation_after_a_request_left_still_counts_it(ledger, monkeypatch):
    import asyncio

    page = _Page([])
    page.raise_after_admit = asyncio.CancelledError()
    writer, _ = _writer(page, monkeypatch, record=_account_list())
    with pytest.raises(asyncio.CancelledError):
        await writer.delete_account_list(LIST, expected_name=NAME, confirm_send=True)
    assert len(page.sent) == 1
    assert _spent(ledger) == 1, "a request that left must be counted on cancel"


async def test_a_stalled_last_progress_note_does_not_lose_the_result(
    tool_ledger, monkeypatch, mock_context
):
    import anyio

    page = _Page([{"status": 204, "payload": None, "restliId": None}])
    writer, _ = _writer(page, monkeypatch, record=_account_list())

    async def progress(*, progress: int, **_kwargs: Any) -> None:
        if progress == 100:
            await anyio.sleep(60)

    mock_context.report_progress = progress
    fn = await _tool("delete_sales_navigator_account_list")
    with anyio.fail_after(2):
        result = await fn(LIST, NAME, True, mock_context, extractor=_Extractor(writer))
    assert result["status"] == "deleted"


async def test_a_create_with_accounts_needs_room_for_both_requests(
    tool_ledger, monkeypatch, mock_context
):
    from linkedin_mcp_server.exceptions import ActionLimitError
    from linkedin_mcp_server.tools import sales_navigator_lists as tools

    monkeypatch.setenv("SALES_LIST_WRITES_MAX", "2")
    pacing.record_write(tool_ledger, SALES_LIST_WRITES, datetime.now(timezone.utc))
    monkeypatch.setattr(tools, "raise_tool_error", lambda exc, tool: _reraise(exc))
    page = _Page([])
    writer, opened = _writer(page, monkeypatch, record=_created)
    fn = await _tool("create_sales_navigator_account_list")
    with pytest.raises(ActionLimitError):
        await fn(
            NAME,
            True,
            mock_context,
            accounts=["100000001"],
            extractor=_Extractor(writer),
        )
    assert opened == [], "a create with accounts must reserve both requests first"


def _reraise(exc: BaseException) -> Any:
    raise exc


# Reading the target


class _Capture:
    async def __aenter__(self) -> _Capture:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


def _response(entity_id: str | None, payload: Any = None, **params: str) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        ok=True,
        payload=payload,
        call=SimpleNamespace(
            resource="salesApiLists", entity_id=entity_id, params=params
        ),
    )


class _OpeningReader(_Reader):
    def __init__(self, page: _Page, found: Any):
        super().__init__(page)
        self.found = found
        self.ready: Any = None

    def _capture(self, *_resources: str) -> _Capture:
        return _Capture()

    async def _open(self, capture: Any, url: str, *, ready: Any, **_kw: Any) -> Any:
        self.ready = ready
        return self.found


async def test_the_list_page_waits_for_that_lists_own_record():
    reader = _OpeningReader(_Page([]), _response(LIST, _account_list()))
    writer = writes.SalesNavigatorListWriter(reader)
    _, record = await writer._open("https://example.invalid", list_id=LIST)
    assert record is not None and record["id"] == LIST
    assert reader.ready(_response(LIST)) is True
    assert reader.ready(_response("100000302")) is False, (
        "another list's record must not count as the list's page being ready"
    )


async def test_a_record_for_another_list_is_never_written_to():
    from linkedin_mcp_server.core.exceptions import LinkedInScraperException

    other = _account_list(id="100000302")
    reader = _OpeningReader(_Page([]), _response(LIST, other))
    writer = writes.SalesNavigatorListWriter(reader)
    with pytest.raises(LinkedInScraperException, match="does not recognise"):
        await writer._open("https://example.invalid", list_id=LIST)
