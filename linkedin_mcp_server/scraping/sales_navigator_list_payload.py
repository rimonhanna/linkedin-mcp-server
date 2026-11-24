"""The account-list write requests and how Sales Navigator answers them.

Browser-free. The request shapes are the ones Sales Navigator's own app
(``lighthouse-web``) sends, read from its code and its aborted requests on
2026-11-24:

- create: ``POST salesApiLists`` with ``{name, listType, role}`` and the
  description when there is one, both trimmed as the app trims them;
- add: ``POST salesApiCompanies?action=save`` with ``{companyIds, lists}``, the
  app's Save to list. It saves each account as well, as the app does;
- delete: ``DELETE salesApiLists/<id>`` with ``unsaveEntity=false``, the app's
  default, so the accounts stay saved.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from linkedin_mcp_server.scraping.restli import as_dicts, as_mapping, as_str
from linkedin_mcp_server.scraping.sales_navigator_list_guard import PlannedListWrite
from linkedin_mcp_server.scraping.sales_navigator_payload import sales_company_id

LISTS = "salesApiLists"
COMPANIES = "salesApiCompanies"
ACCOUNT_LIST_TYPE = "ACCOUNT"
# Most accounts one call adds: one page of account search results.
MAX_ACCOUNTS_PER_CALL = 25
# The limits the app's own list form enforces (maxlength on its fields).
NAME_MAX_LENGTH = 75
DESCRIPTION_MAX_LENGTH = 350

AccountStatus = Literal["added", "already_in_list", "refused", "unconfirmed"]
# The app's LIST_ENTITY_ACTION_STATUS values other than SUCCESS and CONFLICT
# all mean the account is not in the list.
_ADDED = "SUCCESS"
_ALREADY = "CONFLICT"


def refuse_invalid_list_text(name: str, description: str | None) -> str | None:
    """Why a list name or description cannot be sent, or None."""
    if not name.strip():
        return "name must contain non-whitespace characters."
    if len(name.strip()) > NAME_MAX_LENGTH:
        return f"name must be at most {NAME_MAX_LENGTH} characters."
    if "\n" in name or "\r" in name:
        return "name must be one line."
    if description is not None and len(description.strip()) > DESCRIPTION_MAX_LENGTH:
        return f"description must be at most {DESCRIPTION_MAX_LENGTH} characters."
    for text in (name, description or ""):
        if any((ord(c) < 32 and c not in "\n\r") or ord(c) == 127 for c in text):
            return "name and description must not contain control characters."
    return None


def create_request(name: str, description: str | None) -> PlannedListWrite:
    body: dict[str, Any] = {
        "name": name.strip(),
        "listType": ACCOUNT_LIST_TYPE,
        "role": "OWNER",
    }
    if description and description.strip():
        body["description"] = description.strip()
    return PlannedListWrite(
        method="POST", resource=LISTS, entity_id=None, query=(), body=body
    )


def add_request(list_id: str, company_ids: Sequence[str]) -> PlannedListWrite:
    return PlannedListWrite(
        method="POST",
        resource=COMPANIES,
        entity_id=None,
        query=(("action", "save"),),
        body={"companyIds": [int(c) for c in company_ids], "lists": [int(list_id)]},
    )


def delete_request(list_id: str) -> PlannedListWrite:
    return PlannedListWrite(
        method="DELETE",
        resource=LISTS,
        entity_id=list_id,
        query=(("unsaveEntity", "false"), ("doUnsaveLeadsUnderAccount", "false")),
        body=None,
    )


def created_list_id(restli_id: Any, payload: Any) -> str | None:
    """The new list's id from a create's answer, or None when it names none."""
    record = as_mapping(payload) or {}
    for value in (
        restli_id,
        record.get("id"),
        (as_mapping(record.get("value")) or {}).get("id"),
    ):
        text = str(value).strip() if isinstance(value, (int, str)) else ""
        if text.isdigit():
            return text
    return None


def add_statuses(
    payload: Any, company_ids: Sequence[str]
) -> dict[str, tuple[AccountStatus, str | None]] | None:
    """Each account's outcome from a save's answer, or None when unreadable.

    The per-list answer decides: SUCCESS is added, CONFLICT was already in the
    list, any other status is refused. An account the answer does not name is
    unconfirmed, never added.
    """
    value = as_mapping((as_mapping(payload) or {}).get("value"))
    if value is None:
        return None
    statuses: dict[str, tuple[AccountStatus, str | None]] = {
        company_id: ("unconfirmed", None) for company_id in company_ids
    }
    for urn in value.get("failed") or []:
        company_id = sales_company_id(urn)
        if company_id in statuses:
            statuses[company_id] = ("refused", as_str(value.get("status")))
    for entry in as_dicts(value.get("listEntityActionResponses")):
        company_id = sales_company_id(entry.get("entity"))
        status = as_str(entry.get("status"))
        if company_id not in statuses or status is None:
            continue
        if status == _ADDED:
            statuses[company_id] = ("added", None)
        elif status == _ALREADY:
            statuses[company_id] = ("already_in_list", None)
        else:
            statuses[company_id] = ("refused", status)
    return statuses
