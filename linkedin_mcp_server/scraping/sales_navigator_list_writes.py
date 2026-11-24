"""Create, fill and delete Sales Navigator account lists.

Each write loads one Sales Navigator page through ``PageNavigator`` (a charged
page load) and reads its target from the responses that page receives: the
account-lists page for a create, the list's own page for an add or a delete,
whose ``salesApiLists/<id>`` record must name an account list (and, for a
delete, carry exactly the expected name) before anything is sent.

The write itself is the one request the app would make for it, sent from the
page with the headers the app's own Sales Navigator requests carry. That keeps
it DOM-free (no visible text, no layout selectors) and makes an add one
request however many accounts it carries. ``sales_navigator_list_guard``
holds the page for the whole call, so the app's own writes are aborted, and
admits only that one planned request. Success is reported only from Sales
Navigator's answer to it; an answer that does not arrive is an unknown
outcome, reported with ``retry_safe`` false and never sent again.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
    LinkedInScraperException,
)
from linkedin_mcp_server.exceptions import ActionLimitError
from linkedin_mcp_server.pacing import (
    SALES_LIST_WRITES,
    JobStore,
    admit_write,
    cooldown_resume_at,
    note_throttle_signal,
    record_write,
)
from linkedin_mcp_server.scraping.identifiers import (
    SALES_ACCOUNT_LISTS_URL,
    normalize_sales_company_id,
    sales_account_list_url,
)
from linkedin_mcp_server.scraping.restli import envelope_record
from linkedin_mcp_server.scraping.sales_navigator_list_guard import (
    PlannedListWrite,
    explicit_list_write,
    without_sales_list_writes,
)
from linkedin_mcp_server.scraping.sales_navigator_list_payload import (
    ACCOUNT_LIST_TYPE,
    LISTS,
    MAX_ACCOUNTS_PER_CALL,
    add_request,
    create_request,
    created_list_id,
    delete_request,
)
from linkedin_mcp_server.scraping.sales_navigator_payload import (
    AccountList,
    SN_API_PREFIX,
    build_account_list,
)

logger = logging.getLogger(__name__)

_FORWARDED_HEADERS = frozenset(
    {
        "accept",
        "csrf-token",
        "x-li-identity",
        "x-li-lang",
        "x-li-page-instance",
        "x-li-track",
        "x-restli-protocol-version",
    }
)
_LISTS_PATH = "/sales/lists/company"
_LIST_PATH = "/sales/accounts/dashboard"
_SN_API_URL = "https://www.linkedin.com" + SN_API_PREFIX

_FETCH_JS = """async ({ url, method, headers, body }) => {
    /* linkedin-mcp:sn-list-write */
    try {
        const init = { method, credentials: 'same-origin', headers };
        if (body !== null) init.body = body;
        const response = await fetch(url, init);
        let payload = null;
        try { payload = await response.json(); } catch (_) {}
        return {
            status: response.status,
            payload,
            restliId: response.headers.get('x-restli-id'),
        };
    } catch (_) {
        return { status: 0, payload: null, restliId: null };
    }
}"""


@dataclass(slots=True)
class PlannedAccounts:
    """The accounts a call was given: one row per input, the ids to send."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)
    invalid: str | None = None


def plan_accounts(values: Sequence[str]) -> PlannedAccounts:
    """Normalize and de-duplicate the accounts, in the order given."""
    plan = PlannedAccounts()
    for value in values:
        try:
            company_id = normalize_sales_company_id(value)
        except InvalidReferenceError:
            plan.rows.append({"input": value, "status": "invalid_id"})
            plan.invalid = (
                "An account is not a Sales Navigator company id; pass ids exactly "
                "as search_sales_navigator_accounts returned them."
            )
            continue
        if company_id in plan.ids:
            plan.rows.append({"input": value, "id": company_id, "status": "duplicate"})
            continue
        plan.ids.append(company_id)
        plan.rows.append({"input": value, "id": company_id, "status": "planned"})
    if len(plan.ids) > MAX_ACCOUNTS_PER_CALL:
        plan.invalid = (
            f"At most {MAX_ACCOUNTS_PER_CALL} accounts per call; split the rest "
            "into further calls."
        )
    return plan


def list_write_result(
    tool: str,
    status: str,
    message: str,
    *,
    dry_run: bool,
    retry_safe: bool = True,
    list_id: str | None = None,
    account_list: dict[str, Any] | None = None,
    accounts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The one result shape of the three list-write tools."""
    target = dict(account_list or {})
    if list_id is not None:
        target.setdefault("id", list_id)
    result: dict[str, Any] = {
        "url": (
            sales_account_list_url(target["id"])
            if target.get("id")
            else SALES_ACCOUNT_LISTS_URL
        ),
        "tool": tool,
        "status": status,
        "message": message,
        "dry_run": dry_run,
        "retry_safe": retry_safe,
    }
    if target:
        result["list"] = target
    if accounts is not None:
        result["accounts"] = accounts
    return result


@dataclass(frozen=True, slots=True)
class _Answer:
    """Sales Navigator's answer to the one request sent, if it got one."""

    sent: bool
    status: int
    payload: Any = None
    restli_id: str | None = None
    refused: str | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def _set_statuses(rows: list[dict[str, Any]], status: str, detail: str = "") -> None:
    for row in rows:
        if row["status"] in ("planned", "would_add", "not_sent"):
            row["status"] = status
            if detail:
                row["detail"] = detail


class _Stopped(Exception):
    """A pacing stop before anything was sent: the cap or a cooldown."""


class SalesNavigatorListWriter:
    """Own the account-list writes; the reader owns the page loads."""

    def __init__(self, reader: Any):
        self._reader = reader

    @property
    def _page(self) -> Any:
        return self._reader._session.page

    async def _open(
        self, url: str, *, list_id: str | None
    ) -> tuple[dict[str, str] | None, AccountList | None]:
        """Load ``url`` and return the app's request headers and the list record.

        The headers are None when the page sent no Sales Navigator request
        carrying a CSRF token; only a write needs them.

        ``list_id`` None loads the account-lists page and waits for its first
        list page; otherwise the list's page and its ``salesApiLists/<id>``
        record.
        """
        reader = self._reader
        templates: list[dict[str, str]] = []

        def remember(request: Any) -> None:
            if (
                not templates
                and request.method.upper() == "GET"
                and SN_API_PREFIX in request.url
            ):
                headers = {
                    key.casefold(): value
                    for key, value in (request.headers or {}).items()
                }
                if "csrf-token" in headers:
                    templates.append(
                        {k: v for k, v in headers.items() if k in _FORWARDED_HEADERS}
                    )

        def is_record(response: Any) -> bool:
            call = response.call
            if list_id is None:
                return (
                    response.ok
                    and call.resource == LISTS
                    and call.entity_id is None
                    and call.params.get("listType") == ACCOUNT_LIST_TYPE
                )
            return response.ok and call.resource == LISTS and call.entity_id == list_id

        from linkedin_mcp_server.scraping.sales_navigator import _Refusal

        page = self._page
        page.on("request", remember)
        try:
            async with reader._capture(LISTS) as capture:
                found = await reader._open(
                    capture,
                    url,
                    expected_path=_LISTS_PATH if list_id is None else _LIST_PATH,
                    ready=is_record,
                    refused=(
                        None
                        if list_id is None
                        else _Refusal("list_id", frozenset({LISTS}), list_id)
                    ),
                )
        finally:
            try:
                page.remove_listener("request", remember)
            except Exception:
                pass
        record = None
        if list_id is not None and found is not None:
            record = build_account_list(envelope_record(found.payload))
            if record is None or record.get("id") != list_id:
                raise LinkedInScraperException(
                    "Sales Navigator returned the list in a shape this tool does "
                    "not recognise, so nothing was changed."
                )
        return (templates[0] if templates else None), record

    def _admit(self) -> None:
        """Refuse the write now when a cooldown or a cap says so."""
        now = datetime.now(timezone.utc)
        resume_at = cooldown_resume_at(JobStore(), now)
        if resume_at is not None:
            raise _Stopped(
                "The account is paused after LinkedIn pushed back, until "
                f"{resume_at.isoformat(timespec='seconds')}; nothing was sent."
            )
        try:
            admit_write(JobStore(), SALES_LIST_WRITES, now)
        except ActionLimitError as exc:
            raise _Stopped(f"{exc} Nothing was sent.") from exc

    async def _send(
        self, planned: PlannedListWrite, headers: dict[str, str]
    ) -> _Answer:
        """Send ``planned`` once, inside its window, and read the answer."""
        url = _SN_API_URL + planned.resource
        if planned.entity_id is not None:
            url += f"/{planned.entity_id}"
        if planned.action is not None:
            url += f"?action={planned.action}"
        body = None
        send_headers = dict(headers)
        if planned.body is not None:
            import json

            body = json.dumps(planned.body, separators=(",", ":"))
            send_headers["content-type"] = "application/json"
        raw: Any = None
        async with explicit_list_write(self._page, planned) as window:
            try:
                raw = await self._page.evaluate(
                    _FETCH_JS,
                    {
                        "url": url,
                        "method": planned.method,
                        "headers": send_headers,
                        "body": body,
                    },
                )
            except Exception:
                logger.warning("The Sales Navigator list write failed", exc_info=True)
                raw = None
            finally:
                if window.admitted:
                    record_write(
                        JobStore(), SALES_LIST_WRITES, datetime.now(timezone.utc)
                    )
        if not window.admitted:
            return _Answer(
                sent=False,
                status=0,
                refused=window.refused[0] if window.refused else None,
            )
        result = raw if isinstance(raw, dict) else {}
        status = result.get("status")
        status = status if isinstance(status, int) else 0
        if status == 429:
            note_throttle_signal("http_429")
        restli_id = result.get("restliId")
        return _Answer(
            sent=True,
            status=status,
            payload=result.get("payload"),
            restli_id=restli_id if isinstance(restli_id, str) else None,
        )

    async def _write(
        self,
        planned: PlannedListWrite,
        headers: dict[str, str] | None,
        on_unknown: Callable[[str], dict[str, Any]],
        on_refused: Callable[[str], dict[str, Any]],
        on_not_sent: Callable[[str], dict[str, Any]],
    ) -> _Answer | dict[str, Any]:
        """The answer to ``planned``, or the finished result when there is none."""
        if headers is None:
            return on_not_sent(
                "Sales Navigator sent no request this tool could take its "
                "headers from, so nothing was sent."
            )
        try:
            self._admit()
        except _Stopped as exc:
            return on_not_sent(str(exc))
        answer = await self._send(planned, headers)
        if not answer.sent:
            return on_not_sent(
                "The request did not match the plan and was stopped in the "
                f"browser ({answer.refused}); nothing was sent."
                if answer.refused
                else "Nothing was sent."
            )
        if answer.status == 0 or answer.status >= 500 or answer.status == 429:
            what = (
                "no answer arrived"
                if answer.status == 0
                else f"Sales Navigator answered HTTP {answer.status}"
            )
            return on_unknown(what)
        if not answer.ok:
            return on_refused(f"Sales Navigator refused it (HTTP {answer.status})")
        return answer

    async def create_account_list(
        self,
        name: str,
        *,
        description: str | None = None,
        accounts: Sequence[str] = (),
        confirm_send: bool,
    ) -> dict[str, Any]:
        tool = "create_sales_navigator_account_list"
        rows = [
            {"input": company_id, "id": company_id, "status": "planned"}
            for company_id in accounts
        ]
        planned_list: dict[str, Any] = {"name": name}
        if description:
            planned_list["description"] = description
        if not confirm_send:
            _set_statuses(rows, "would_add")
            requests = 1 + bool(rows)
            return list_write_result(
                tool,
                "dry_run",
                f"Nothing was created. confirm_send true sends {requests} "
                f"request{'s' if requests > 1 else ''}: one creating the list"
                + (", one adding the accounts marked would_add." if rows else "."),
                dry_run=True,
                account_list=planned_list,
                accounts=rows,
            )
        page = self._page
        async with without_sales_list_writes(page):
            headers, _ = await self._open(SALES_ACCOUNT_LISTS_URL, list_id=None)

            def not_sent(message: str) -> dict[str, Any]:
                _set_statuses(rows, "not_sent")
                return list_write_result(
                    tool,
                    "not_sent",
                    message,
                    dry_run=False,
                    account_list=planned_list,
                    accounts=rows,
                )

            def unknown(what: str) -> dict[str, Any]:
                _set_statuses(rows, "not_sent")
                return list_write_result(
                    tool,
                    "create_unconfirmed",
                    f"The list was sent but {what}. Check "
                    "list_sales_navigator_account_lists before trying again; a "
                    "repeat could create the list twice.",
                    dry_run=False,
                    retry_safe=False,
                    account_list=planned_list,
                    accounts=rows,
                )

            def refused(what: str) -> dict[str, Any]:
                _set_statuses(rows, "not_sent")
                return list_write_result(
                    tool,
                    "refused_by_linkedin",
                    f"{what}; no list was created.",
                    dry_run=False,
                    retry_safe=False,
                    account_list=planned_list,
                    accounts=rows,
                )

            answer = await self._write(
                create_request(name, description), headers, unknown, refused, not_sent
            )
            if isinstance(answer, dict):
                return answer
            list_id = created_list_id(answer.restli_id, answer.payload)
            if list_id is None:
                return unknown("its answer named no list id")
            planned_list = {"id": list_id, **planned_list}
            if not rows:
                return list_write_result(
                    tool,
                    "created",
                    "Sales Navigator created the list.",
                    dry_run=False,
                    retry_safe=False,
                    account_list=planned_list,
                    accounts=rows,
                )
            result = await self._add(
                tool, list_id, rows, headers, account_list=planned_list
            )
            result["message"] = "Sales Navigator created the list. " + result["message"]
            if result["status"] == "added":
                result["status"] = "created"
            # The list exists whatever happened to the accounts.
            result["retry_safe"] = False
            return result

    async def add_accounts(
        self, list_id: str, accounts: Sequence[str], *, confirm_send: bool
    ) -> dict[str, Any]:
        tool = "add_accounts_to_sales_navigator_list"
        rows = [
            {"input": company_id, "id": company_id, "status": "planned"}
            for company_id in accounts
        ]
        page = self._page
        async with without_sales_list_writes(page):
            headers, record = await self._open(
                sales_account_list_url(list_id), list_id=list_id
            )
            account_list = dict(record or {"id": list_id})
            refusal = _not_an_account_list(account_list)
            if refusal is not None:
                _set_statuses(rows, "not_sent")
                return list_write_result(
                    tool,
                    "wrong_list",
                    refusal,
                    dry_run=not confirm_send,
                    account_list=account_list,
                    accounts=rows,
                )
            if not confirm_send:
                _set_statuses(rows, "would_add")
                return list_write_result(
                    tool,
                    "dry_run",
                    "Nothing was added. confirm_send true sends one request "
                    "adding the accounts marked would_add to this list.",
                    dry_run=True,
                    account_list=account_list,
                    accounts=rows,
                )
            return await self._add(
                tool, list_id, rows, headers, account_list=account_list
            )

    async def _add(
        self,
        tool: str,
        list_id: str,
        rows: list[dict[str, Any]],
        headers: dict[str, str] | None,
        *,
        account_list: dict[str, Any],
    ) -> dict[str, Any]:
        ids = [row["id"] for row in rows if row["status"] == "planned"]

        def finish(status: str, message: str, *, retry_safe: bool) -> dict[str, Any]:
            return list_write_result(
                tool,
                status,
                message,
                dry_run=False,
                retry_safe=retry_safe,
                account_list=account_list,
                accounts=rows,
            )

        def not_sent(message: str) -> dict[str, Any]:
            _set_statuses(rows, "not_sent")
            return finish("not_sent", message, retry_safe=True)

        def unknown(what: str) -> dict[str, Any]:
            _set_statuses(rows, "unconfirmed")
            return finish(
                "add_unconfirmed",
                f"The accounts were sent but {what}. Read the list with "
                "get_sales_navigator_account_list before trying again.",
                retry_safe=False,
            )

        def refused(what: str) -> dict[str, Any]:
            _set_statuses(rows, "refused", what)
            return finish(
                "refused_by_linkedin",
                f"{what}; no account was added.",
                retry_safe=False,
            )

        answer = await self._write(
            add_request(list_id, ids), headers, unknown, refused, not_sent
        )
        if isinstance(answer, dict):
            return answer
        _set_statuses(rows, "added")
        return finish(
            "added",
            "Sales Navigator accepted the accounts for this list.",
            retry_safe=False,
        )

    async def delete_account_list(
        self, list_id: str, *, expected_name: str, confirm_send: bool
    ) -> dict[str, Any]:
        tool = "delete_sales_navigator_account_list"
        page = self._page
        async with without_sales_list_writes(page):
            headers, record = await self._open(
                sales_account_list_url(list_id), list_id=list_id
            )
            account_list = dict(record or {"id": list_id})

            def finish(
                status: str, message: str, *, retry_safe: bool = True
            ) -> dict[str, Any]:
                return list_write_result(
                    tool,
                    status,
                    message,
                    dry_run=not confirm_send,
                    retry_safe=retry_safe,
                    account_list=account_list,
                )

            refusal = _not_an_account_list(account_list)
            if refusal is not None:
                return finish("wrong_list", refusal)
            if account_list.get("name") != expected_name:
                return finish(
                    "name_mismatch",
                    "This list's name is not expected_name, so nothing was "
                    "deleted. Read the list again and pass its exact name.",
                )
            if not confirm_send:
                return finish(
                    "dry_run",
                    "Nothing was deleted. confirm_send true sends one request "
                    "deleting this list; its accounts are not affected.",
                )
            answer = await self._write(
                delete_request(list_id),
                headers,
                lambda what: finish(
                    "delete_unconfirmed",
                    f"The delete was sent but {what}. Check "
                    "list_sales_navigator_account_lists before trying again.",
                    retry_safe=False,
                ),
                lambda what: finish(
                    "refused_by_linkedin",
                    f"{what}; the list was not deleted.",
                    retry_safe=False,
                ),
                lambda message: finish("not_sent", message),
            )
            if isinstance(answer, dict):
                return answer
            return finish(
                "deleted", "Sales Navigator deleted the list.", retry_safe=False
            )


def _not_an_account_list(account_list: dict[str, Any]) -> str | None:
    list_type = account_list.get("listType")
    if list_type != ACCOUNT_LIST_TYPE:
        return (
            "This id is not one of your Sales Navigator account lists "
            f"(list type {list_type or 'unknown'}), so nothing was changed."
        )
    return None
