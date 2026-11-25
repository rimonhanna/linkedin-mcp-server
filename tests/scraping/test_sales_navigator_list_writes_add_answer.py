"""How an add reads Sales Navigator's answer, and the read-back when it names no account.

``_measured`` mirrors the structure of the ``salesApiCompanies?action=save``
answer measured live on 2026-11-24 (keys, nesting and URN kinds); every id in
it is synthetic.
"""

from __future__ import annotations

from typing import Any

from scraping.test_sales_navigator_list_writes import (
    LIST,
    _account_list,
    _Page,
    _rows,
    _writer,
    ledger,  # noqa: F401
)

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.scraping import sales_navigator_list_writes as writes


def _measured(*entries: tuple[str, str], status: str = "SUCCESS") -> dict[str, Any]:
    return {
        "status": 200,
        "payload": {
            "value": {
                "listEntityActionResponses": [
                    {
                        "entityUnion": {"organization": f"urn:li:organization:{c}"},
                        "list": f"urn:li:fs_salesList:{LIST}",
                        "entity": f"urn:li:organization:{c}",
                        "status": s,
                    }
                    for c, s in entries
                ],
                "failed": [],
                "successful": [
                    {"companyUrn": f"urn:li:fs_salesCompany:{c}"} for c, _ in entries
                ],
                "status": status,
            }
        },
        "restliId": None,
    }


def _silent() -> dict[str, Any]:
    """A 2xx whose answer names no account."""
    return {
        "status": 200,
        "payload": {"value": {"successful": [], "failed": [], "status": "SUCCESS"}},
        "restliId": None,
    }


def _reads(writer: Any, *results: Any) -> list[tuple[str, Any]]:
    """Make the reader's ``get_account_list`` answer ``results``, then an empty list."""
    calls: list[tuple[str, Any]] = []
    queue = list(results)

    async def get_account_list(list_id: str, page: int = 1, *, deadline=None):
        calls.append((list_id, deadline))
        result = queue.pop(0) if queue else _listed()
        if isinstance(result, BaseException):
            raise result
        return result

    writer._reader.get_account_list = get_account_list
    return calls


def _listed(*ids: str) -> dict[str, Any]:
    return {"account_list": _account_list(), "accounts": [{"id": i} for i in ids]}


async def test_the_measured_answer_shape_reports_each_account(ledger, monkeypatch):  # noqa: F811
    page = _Page([_measured(("100000001", "SUCCESS"), ("100000002", "CONFLICT"))])
    writer, _ = _writer(page, monkeypatch, record=_account_list())
    reads = _reads(writer)
    result = await writer.add_accounts(
        LIST, _rows("100000001", "100000002"), confirm_send=True
    )
    assert [a["status"] for a in result["accounts"]] == [
        "added",
        "already_in_list",
    ], "an organization-URN entity in listEntityActionResponses must name the account"
    assert result["status"] == "added"
    assert reads == [], "an answer that names every account needs no read-back"


async def test_successful_and_top_status_alone_add_nothing():
    statuses = writes.add_statuses(
        {
            "value": {
                "listEntityActionResponses": [],
                "failed": [],
                "successful": [{"companyUrn": "urn:li:fs_salesCompany:100000001"}],
                "status": "SUCCESS",
            }
        },
        ["100000001"],
    )
    assert statuses == {"100000001": ("unconfirmed", None)}, (
        "successful[] and the top-level status are not per-list, so they add nothing"
    )


async def test_a_failed_company_record_is_refused():
    statuses = writes.add_statuses(
        {
            "value": {
                "listEntityActionResponses": [],
                "failed": [{"companyUrn": "urn:li:fs_salesCompany:100000001"}],
                "status": "FORBIDDEN",
            }
        },
        ["100000001"],
    )
    assert statuses == {"100000001": ("refused", "FORBIDDEN")}


async def test_a_silent_2xx_is_confirmed_by_reading_the_list(ledger, monkeypatch):  # noqa: F811
    page = _Page([_silent()])
    writer, _ = _writer(page, monkeypatch, record=_account_list())
    reads = _reads(writer, _listed("100000009", "100000001"))
    result = await writer.add_accounts(
        LIST, _rows("100000001", "100000002"), confirm_send=True
    )
    assert reads == [(LIST, 0.0)], "the read-back must read the list's first page once"
    assert [a["status"] for a in result["accounts"]] == [
        "added",
        "unconfirmed",
    ], "an account found on read-back is added, one not found stays unconfirmed"
    assert result["accounts"][0]["detail"] == writes._READ_BACK
    assert result["status"] == "add_unconfirmed"
    assert result["retry_safe"] is False


async def test_every_account_found_on_read_back_is_added(ledger, monkeypatch):  # noqa: F811
    page = _Page([_silent()])
    writer, _ = _writer(page, monkeypatch, record=_account_list())
    _reads(writer, _listed("100000002", "100000001"))
    result = await writer.add_accounts(
        LIST, _rows("100000001", "100000002"), confirm_send=True
    )
    assert result["status"] == "added"
    assert "read back" in result["message"]


async def test_a_failed_read_back_leaves_the_add_unconfirmed(ledger, monkeypatch):  # noqa: F811
    page = _Page([_silent()])
    writer, _ = _writer(page, monkeypatch, record=_account_list())
    _reads(writer, LinkedInScraperException("the list did not load"))
    result = await writer.add_accounts(LIST, _rows("100000001"), confirm_send=True)
    assert result["status"] == "add_unconfirmed"
    assert result["accounts"][0]["status"] == "unconfirmed"
    assert result["retry_safe"] is False
