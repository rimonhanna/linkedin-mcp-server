"""Create, fill and delete Sales Navigator account lists.

Each write loads one Sales Navigator page through ``PageNavigator`` (a charged
page load) and reads its target from the responses that page receives: the
account-lists page for a create, the list's own page for an add or a delete,
whose ``salesApiLists/<id>`` record must name an account list (and, for a
delete, carry exactly the expected name) before anything is sent.

A create goes through Sales Navigator's own form: the account-lists page's
create-list form is opened, the name and description typed, and its Create
button pressed. ``sales_navigator_list_guard`` holds the page for the whole
call, so every write is aborted, and the window around that press admits the
app's own request only when it is exactly the planned one. The new list is
then read back from its own page before it is reported created.

An add and a delete are sent from the page as a request shaped like the
app's own (see AGENTS.md, Scraping Rules, for why these two are not driven
through the app's controls), with the headers its Sales Navigator requests
carry, and the guard admits only that one planned request. For every write,
success is reported only from Sales Navigator's answer; an answer that does
not arrive, or an error after the request left, is an unknown outcome,
reported with ``retry_safe`` false and never sent again.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
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
from linkedin_mcp_server.scraping.contracts import before_the_reply_deadline
from linkedin_mcp_server.scraping.identifiers import (
    SALES_ACCOUNT_LISTS_URL,
    normalize_sales_company_id,
    sales_account_list_url,
)
from linkedin_mcp_server.scraping.restli import envelope_record
from linkedin_mcp_server.scraping.sales_navigator_list_guard import (
    ListWriteWindow,
    PlannedListWrite,
    explicit_list_write,
    without_sales_list_writes,
)
from linkedin_mcp_server.scraping.sales_navigator_list_payload import (
    ACCOUNT_LIST_TYPE,
    AccountStatus,
    COMPANIES,
    LISTS,
    MAX_ACCOUNTS_PER_CALL,
    add_request,
    add_statuses,
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

CREATE_TOOL = "create_sales_navigator_account_list"
ADD_TOOL = "add_accounts_to_sales_navigator_list"
DELETE_TOOL = "delete_sales_navigator_account_list"

# What the browser itself sets on a request; everything else the app's own
# request carried (its CSRF token, tracking and identity headers) is sent.
_BROWSER_HEADERS = frozenset(
    {
        "accept-encoding",
        "accept-language",
        "connection",
        "content-length",
        "content-type",
        "cookie",
        "host",
        "origin",
        "referer",
        "user-agent",
    }
)
# What the app's JSON.stringify body goes out as.
_BODY_TYPE = "text/plain;charset=UTF-8"
_LISTS_PATH = "/sales/lists/company"
_LIST_PATH = "/sales/accounts/dashboard"
_ORIGIN = "https://www.linkedin.com"

# The account-lists page's create-list form, by the app's own data-x
# attributes (untranslated), measured 2026-11-24.
_CREATE_OPENER = "button[data-x--list-nav--create-list-dropdown-trigger]"
_CREATE_BUTTON = "button[data-x--lists--create-modal-save-button]"
_CREATE_FORM = f"[role=dialog]:has({_CREATE_BUTTON})"
_NAME_FIELD = f"{_CREATE_FORM} input[data-x--text-input-field]"
_DESCRIPTION_FIELD = f"{_CREATE_FORM} textarea"
_FORM_TIMEOUT_MS = 10_000
# Typed key by key: a value set in one input event (``fill``) left the form's
# Create button disabled when measured.
_TYPE_DELAY_MS = 20
# How long the app has to send its request once Create is pressed, and
# Sales Navigator to answer a request that left.
_SEND_TIMEOUT = 10.0
_ANSWER_TIMEOUT = 30.0
_READ_BACK = (
    "Sales Navigator's answer named no account; found on the list when read back."
)

# The answer is read as text and parsed here: a JSON number past 2^53 (a
# list id is a 19-digit long) would be rounded by response.json() in the page.
_FETCH_JS = """async ({ url, method, headers, body }) => {
    /* linkedin-mcp:sn-list-write */
    try {
        const init = { method, credentials: 'same-origin', headers };
        if (body !== null) init.body = body;
        const response = await fetch(url, init);
        let text = null;
        try { text = await response.text(); } catch (_) {}
        return {
            status: response.status,
            text,
            restliId: response.headers.get('x-restli-id'),
        };
    } catch (_) {
        return { status: 0, text: null, restliId: null };
    }
}"""

Transmit = Callable[[ListWriteWindow], Awaitable[Any]]


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
    account_list: Mapping[str, Any] | None = None,
    accounts: list[dict[str, Any]] | None = None,
    accounts_status: str | None = None,
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
    if accounts_status is not None:
        result["accounts_status"] = accounts_status
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


def _parse(text: Any) -> Any:
    if not isinstance(text, str) or not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


class _Stopped(Exception):
    """A stop before anything was sent: the cap, a cooldown or the form."""


class ListWriteLedger:
    """What one tool call sent, for the tool's error path.

    The flows turn every answer into a result, but a Playwright error, a
    closed page or a failed read-back after a request left escapes them. The
    tool layer reads this ledger then: with a request admitted the outcome
    is ``*_unconfirmed`` (or ``created`` when the list was already read
    back), never an error the agent retries. Each admitted request was
    counted against the cap by its window already.
    """

    def __init__(self, tool: str) -> None:
        self.tool = tool
        self.windows: list[ListWriteWindow] = []
        self.rows: list[dict[str, Any]] = []
        self.account_list: dict[str, Any] = {}
        self.created = False

    @property
    def admitted(self) -> bool:
        return any(window.admitted for window in self.windows)

    def unconfirmed(self, error: BaseException) -> dict[str, Any]:
        """The result for ``error`` raised after a request was admitted."""
        stopped = (
            f"then the tool stopped on an unexpected error ({type(error).__name__})"
        )
        add_sent = any(
            window.admitted and window.planned.resource == COMPANIES
            for window in self.windows
        )
        if self.tool == ADD_TOOL or (self.created and add_sent):
            _set_statuses(self.rows, "unconfirmed")
        else:
            _set_statuses(self.rows, "not_sent")

        def result(status: str, message: str, **extra: Any) -> dict[str, Any]:
            return list_write_result(
                self.tool,
                status,
                message,
                dry_run=False,
                retry_safe=False,
                account_list=self.account_list,
                accounts=self.rows if self.tool != DELETE_TOOL else None,
                **extra,
            )

        if self.tool == CREATE_TOOL and self.created:
            return result(
                "created",
                "Sales Navigator created the list. "
                + (
                    f"The accounts were sent, {stopped}; read the list with "
                    "get_sales_navigator_account_list before adding them again."
                    if add_sent
                    else "The accounts were not sent: the tool stopped on an "
                    f"unexpected error ({type(error).__name__}) first."
                ),
                accounts_status="add_unconfirmed" if add_sent else "not_sent",
            )
        if self.tool == CREATE_TOOL:
            return result(
                "create_unconfirmed",
                f"The list was sent, {stopped}, so whether Sales Navigator "
                "created it is unknown. Check list_sales_navigator_account_lists "
                "before trying again; a repeat could create the list twice.",
            )
        if self.tool == ADD_TOOL:
            return result(
                "add_unconfirmed",
                f"The accounts were sent, {stopped}. Read the list with "
                "get_sales_navigator_account_list before trying again.",
            )
        return result(
            "delete_unconfirmed",
            f"The delete was sent, {stopped}. Check "
            "list_sales_navigator_account_lists before trying again.",
        )


_ledger: ContextVar[ListWriteLedger | None] = ContextVar(
    "sales_list_write_ledger", default=None
)


@contextmanager
def tracking_list_writes(tool: str) -> Iterator[ListWriteLedger]:
    """Collect the write windows one tool call opens into one ledger."""
    ledger = ListWriteLedger(tool)
    token = _ledger.set(ledger)
    try:
        yield ledger
    finally:
        _ledger.reset(token)


def _current_ledger() -> ListWriteLedger:
    # A writer called outside the tools (the policy scenarios) keeps a
    # ledger nobody reads.
    return _ledger.get() or ListWriteLedger("")


class SalesNavigatorListWriter:
    """Own the account-list writes; the reader owns the page loads."""

    def __init__(self, reader: Any):
        self._reader = reader
        self._session = reader._session

    async def _open(
        self, url: str, *, list_id: str | None
    ) -> tuple[dict[str, str] | None, AccountList | None]:
        """Load ``url`` and return the app's request headers and the list record.

        The headers are None when the page sent no Sales Navigator request
        carrying a CSRF token; only a sent request needs them.

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
                        {
                            k: v
                            for k, v in headers.items()
                            if k not in _BROWSER_HEADERS and not k.startswith("sec-")
                        }
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

        page = self._session.page
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

    def _fetch(self, planned: PlannedListWrite, headers: dict[str, str]) -> Transmit:
        """Send ``planned`` from the page, shaped like the app's request."""
        body = None
        send_headers = dict(headers)
        if planned.body is not None:
            body = json.dumps(planned.body, separators=(",", ":"), ensure_ascii=False)
            send_headers["content-type"] = _BODY_TYPE

        async def transmit(_window: ListWriteWindow) -> Any:
            return await self._session.page.evaluate(
                _FETCH_JS,
                {
                    "url": _ORIGIN + planned.path,
                    "method": planned.method,
                    "headers": send_headers,
                    "body": body,
                },
            )

        return transmit

    async def _fill_create_form(self, planned: PlannedListWrite) -> Any:
        """Open the create-list form and type the plan; its Create button.

        Raises ``_Stopped`` when the form is not there or does not hold
        exactly the plan; nothing is pressed then.
        """
        page = self._session.page
        opener = page.locator(_CREATE_OPENER)
        if await opener.count() != 1:
            raise _Stopped(
                "Sales Navigator did not show its create-list button, so nothing "
                "was sent."
            )
        await opener.first.click()
        name_field = page.locator(_NAME_FIELD).first
        try:
            await name_field.wait_for(state="visible", timeout=_FORM_TIMEOUT_MS)
        except Exception:
            raise _Stopped(
                "Sales Navigator did not open its create-list form, so nothing "
                "was sent."
            ) from None
        typed = [(name_field, planned.body["name"])]
        if "description" in planned.body:
            typed.append(
                (page.locator(_DESCRIPTION_FIELD).first, planned.body["description"])
            )
        for form_field, text in typed:
            await form_field.press_sequentially(text, delay=_TYPE_DELAY_MS)
            if await form_field.input_value() != text:
                await page.keyboard.press("Escape")
                raise _Stopped(
                    "Sales Navigator's create-list form did not take the name "
                    "and description as given, so nothing was sent."
                )
        button = page.locator(_CREATE_BUTTON).first
        if not await button.is_enabled():
            await page.keyboard.press("Escape")
            raise _Stopped(
                "Sales Navigator's create-list form did not accept the list, so "
                "nothing was sent."
            )
        return button

    @staticmethod
    def _press(button: Any) -> Transmit:
        """Press the form's Create button; the app sends its own request."""

        async def transmit(window: ListWriteWindow) -> Any:
            await button.click()
            async with asyncio.timeout(_SEND_TIMEOUT):
                await window.decided.wait()
            request = window.request
            if request is None:
                return None
            async with asyncio.timeout(_ANSWER_TIMEOUT):
                response = await request.response()
                if response is None:
                    return {"status": 0}
                return {
                    "status": response.status,
                    "text": await response.text(),
                    "restliId": await response.header_value("x-restli-id"),
                }

        return transmit

    async def _send(self, planned: PlannedListWrite, transmit: Transmit) -> _Answer:
        """Let ``planned`` through once, inside its window, and read the answer."""
        raw: Any = None
        async with explicit_list_write(self._session.page, planned) as window:
            _current_ledger().windows.append(window)
            try:
                # Ends ahead of the tool's deadline, so a request that left is
                # answered as unconfirmed instead of landing as a cancellation.
                with before_the_reply_deadline():
                    raw = await transmit(window)
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
            payload=_parse(result.get("text")),
            restli_id=restli_id if isinstance(restli_id, str) else None,
        )

    async def _write(
        self,
        planned: PlannedListWrite,
        transmit: Transmit | None,
        on_unknown: Callable[[str], dict[str, Any]],
        on_refused: Callable[[str], dict[str, Any]],
        on_not_sent: Callable[[str], dict[str, Any]],
        *,
        prepare: Callable[[], Awaitable[Transmit]] | None = None,
    ) -> _Answer | dict[str, Any]:
        """The answer to ``planned``, or the finished result when there is none.

        ``prepare``, run once the caps admit the write, readies the app's
        form and returns how to send it, in place of ``transmit``.
        """
        try:
            self._admit()
            if prepare is not None:
                transmit = await prepare()
        except _Stopped as exc:
            return on_not_sent(str(exc))
        if transmit is None:
            return on_not_sent(
                "Sales Navigator sent no request this tool could take its "
                "headers from, so nothing was sent."
            )
        answer = await self._send(planned, transmit)
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
            reason = _error_message(answer.payload)
            return on_refused(
                f"Sales Navigator refused it (HTTP {answer.status}"
                + (f": {reason}" if reason else "")
                + ")"
            )
        return answer

    async def create_account_list(
        self,
        name: str,
        *,
        description: str | None = None,
        accounts: Sequence[Mapping[str, Any]] = (),
        confirm_send: bool,
    ) -> dict[str, Any]:
        tool = CREATE_TOOL
        rows = [dict(row) for row in accounts]
        planned = create_request(name, description)
        planned_list: dict[str, Any] = {
            key: planned.body[key]
            for key in ("name", "description")
            if key in planned.body
        }
        to_add = any(row["status"] == "planned" for row in rows)
        if not confirm_send:
            _set_statuses(rows, "would_add")
            requests = 1 + to_add
            return list_write_result(
                tool,
                "dry_run",
                f"Nothing was created. confirm_send true sends {requests} "
                f"request{'s' if requests > 1 else ''}: one creating the list"
                + (", one adding the accounts marked would_add." if to_add else "."),
                dry_run=True,
                account_list=planned_list,
                accounts=rows,
            )
        ledger = _current_ledger()
        ledger.rows = rows
        ledger.account_list = planned_list
        page = self._session.page
        async with without_sales_list_writes(page):
            await self._open(SALES_ACCOUNT_LISTS_URL, list_id=None)

            def finish(status: str, message: str, **extra: Any) -> dict[str, Any]:
                return list_write_result(
                    tool,
                    status,
                    message,
                    dry_run=False,
                    account_list=ledger.account_list,
                    accounts=rows,
                    **extra,
                )

            def not_sent(message: str) -> dict[str, Any]:
                _set_statuses(rows, "not_sent")
                return finish("not_sent", message)

            def unknown(what: str) -> dict[str, Any]:
                _set_statuses(rows, "not_sent")
                return finish(
                    "create_unconfirmed",
                    f"The list was sent but {what}. Check "
                    "list_sales_navigator_account_lists before trying again; a "
                    "repeat could create the list twice.",
                    retry_safe=False,
                )

            def refused(what: str) -> dict[str, Any]:
                _set_statuses(rows, "not_sent")
                return finish(
                    "refused_by_linkedin",
                    f"{what}; no list was created.",
                    retry_safe=False,
                )

            async def prepare() -> Transmit:
                return self._press(await self._fill_create_form(planned))

            answer = await self._write(
                planned, None, unknown, refused, not_sent, prepare=prepare
            )
            if isinstance(answer, dict):
                return answer
            list_id = created_list_id(answer.restli_id, answer.payload)
            if list_id is None:
                return unknown("its answer named no list id")
            ledger.account_list = {"id": list_id, **planned_list}
            headers, record = None, None
            with before_the_reply_deadline() as read_back:
                headers, record = await self._open(
                    sales_account_list_url(list_id), list_id=list_id
                )
            if read_back.cancelled_caught:
                return unknown("the list's own page did not load in time")
            if (
                record is None
                or _not_an_account_list(dict(record)) is not None
                or record.get("name") != planned.body["name"]
            ):
                return unknown(
                    "the list's own page does not show it as the account list "
                    "that was asked for"
                )
            ledger.account_list = dict(record)
            ledger.created = True
            if not to_add:
                return finish(
                    "created",
                    "Sales Navigator created the list.",
                    retry_safe=False,
                )
            added = await self._add(
                tool, list_id, rows, headers, account_list=ledger.account_list
            )
            # The list exists whatever happened to the accounts.
            return finish(
                "created",
                "Sales Navigator created the list. " + added["message"],
                retry_safe=False,
                accounts_status=added["status"],
            )

    async def add_accounts(
        self,
        list_id: str,
        accounts: Sequence[Mapping[str, Any]],
        *,
        confirm_send: bool,
    ) -> dict[str, Any]:
        tool = ADD_TOOL
        rows = [dict(row) for row in accounts]
        ledger = _current_ledger()
        ledger.rows = rows
        ledger.account_list = {"id": list_id}
        page = self._session.page
        async with without_sales_list_writes(page):
            headers, record = await self._open(
                sales_account_list_url(list_id), list_id=list_id
            )
            account_list = dict(record or {"id": list_id})
            ledger.account_list = account_list
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

        planned = add_request(list_id, ids)
        answer = await self._write(
            planned,
            None if headers is None else self._fetch(planned, headers),
            unknown,
            refused,
            not_sent,
        )
        if isinstance(answer, dict):
            return answer
        statuses = add_statuses(answer.payload, ids)
        if statuses is None or all(s == "unconfirmed" for s, _ in statuses.values()):
            statuses = await self._confirm_by_reading(list_id, ids)
            if statuses is None:
                return unknown("its answer named no account")
        for row in rows:
            if row["status"] != "planned":
                continue
            status, detail = statuses[row["id"]]
            row["status"] = status
            if detail:
                row["detail"] = detail
        counts = Counter(statuses[i][0] for i in ids)
        if counts["unconfirmed"]:
            return finish(
                "add_unconfirmed",
                "Sales Navigator answered, but not for every account; read the "
                "list with get_sales_navigator_account_list before adding the "
                "unconfirmed ones again.",
                retry_safe=False,
            )
        if not counts["added"] and not counts["already_in_list"]:
            return finish(
                "refused_by_linkedin",
                "Sales Navigator added none of the accounts; see each one's detail.",
                retry_safe=False,
            )
        read_back = any(detail == _READ_BACK for _, detail in statuses.values())
        return finish(
            "added" if not counts["refused"] else "partly_added",
            (
                "Sales Navigator accepted the add and the list, read back, "
                "shows every account: "
                if read_back
                else "Sales Navigator answered for every account: "
            )
            + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(counts.items()))
            + ".",
            retry_safe=False,
        )

    async def _confirm_by_reading(
        self, list_id: str, ids: list[str]
    ) -> dict[str, tuple[AccountStatus, str | None]] | None:
        """Each account found on the list's first page is added, the rest unconfirmed.

        For a 2xx whose answer names no account. One page load: an account the
        first 25 rows do not show stays unconfirmed, never refused. None when
        the list could not be read.
        """
        result: dict[str, Any] | None = None
        with before_the_reply_deadline():
            try:
                # A deadline already past reads the first page and no other.
                result = await self._reader.get_account_list(list_id, deadline=0.0)
            except LinkedInScraperException as exc:
                logger.info("add read-back of list %s failed: %s", list_id, exc)
        if result is None:
            return None
        found = {row.get("id") for row in result.get("accounts") or []}
        return {
            company_id: (
                ("added", _READ_BACK) if company_id in found else ("unconfirmed", None)
            )
            for company_id in ids
        }

    async def delete_account_list(
        self, list_id: str, *, expected_name: str, confirm_send: bool
    ) -> dict[str, Any]:
        tool = DELETE_TOOL
        ledger = _current_ledger()
        ledger.account_list = {"id": list_id}
        page = self._session.page
        async with without_sales_list_writes(page):
            headers, record = await self._open(
                sales_account_list_url(list_id), list_id=list_id
            )
            account_list = dict(record or {"id": list_id})
            ledger.account_list = account_list

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
            planned = delete_request(list_id)
            answer = await self._write(
                planned,
                None if headers is None else self._fetch(planned, headers),
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
            # A 2xx alone is not the list gone: only the list's page refusing
            # that id (the reader's own refusal) is.
            gone = False
            with before_the_reply_deadline():
                try:
                    await self._open(sales_account_list_url(list_id), list_id=list_id)
                except InvalidReferenceError:
                    gone = True
            if not gone:
                return finish(
                    "delete_unconfirmed",
                    "Sales Navigator accepted the delete, but the list's page "
                    "did not confirm the list is gone. Check "
                    "list_sales_navigator_account_lists before trying again.",
                    retry_safe=False,
                )
            return finish(
                "deleted", "Sales Navigator deleted the list.", retry_safe=False
            )


def _error_message(payload: Any) -> str | None:
    """The reason in a Rest.li error answer, clipped; never the request."""
    message = payload.get("message") if isinstance(payload, dict) else None
    return (
        message.strip()[:200] if isinstance(message, str) and message.strip() else None
    )


def _not_an_account_list(account_list: Mapping[str, Any]) -> str | None:
    list_type = account_list.get("listType")
    if list_type != ACCOUNT_LIST_TYPE:
        return (
            "This id is not one of your Sales Navigator account lists "
            f"(list type {list_type or 'unknown'}), so nothing was changed."
        )
    return None
