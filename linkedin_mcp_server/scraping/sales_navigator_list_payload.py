"""The account-list write requests and how Sales Navigator answers them.

Browser-free: the planned request for each write, the checks on the text a
list carries, and the reading of Sales Navigator's answer.
"""

from __future__ import annotations

from typing import Any

from linkedin_mcp_server.scraping.restli import as_mapping
from linkedin_mcp_server.scraping.sales_navigator_list_guard import PlannedListWrite

LISTS = "salesApiLists"
ACCOUNT_LIST_TYPE = "ACCOUNT"
# Most accounts one call adds: one page of account search results.
MAX_ACCOUNTS_PER_CALL = 25
# The limits the app's own list form enforces.
NAME_MAX_LENGTH = 80
DESCRIPTION_MAX_LENGTH = 500


def company_urn(company_id: str) -> str:
    return f"urn:li:fs_salesCompany:{company_id}"


def refuse_invalid_list_text(name: str, description: str | None) -> str | None:
    """Why a list name or description cannot be sent, or None."""
    if not name.strip():
        return "name must contain non-whitespace characters."
    if len(name) > NAME_MAX_LENGTH:
        return f"name must be at most {NAME_MAX_LENGTH} characters."
    if description is not None and len(description) > DESCRIPTION_MAX_LENGTH:
        return f"description must be at most {DESCRIPTION_MAX_LENGTH} characters."
    for text in (name, description or ""):
        if any(ord(c) < 32 or ord(c) == 127 for c in text if c != "\n"):
            return "name and description must not contain control characters."
    if "\n" in name:
        return "name must be one line."
    return None


def create_request(name: str, description: str | None) -> PlannedListWrite:
    body: dict[str, Any] = {"name": name, "listType": ACCOUNT_LIST_TYPE}
    if description:
        body["description"] = description
    return PlannedListWrite(
        method="POST", resource=LISTS, entity_id=None, action=None, body=body
    )


def add_request(list_id: str, company_ids: list[str]) -> PlannedListWrite:
    return PlannedListWrite(
        method="POST",
        resource=LISTS,
        entity_id=list_id,
        action="addEntities",
        body={"entityUrns": [company_urn(c) for c in company_ids]},
    )


def delete_request(list_id: str) -> PlannedListWrite:
    return PlannedListWrite(
        method="DELETE", resource=LISTS, entity_id=list_id, action=None, body=None
    )


def created_list_id(restli_id: Any, payload: Any) -> str | None:
    """The new list's id from a create's answer, or None when it names none."""
    for value in (restli_id, (as_mapping(payload) or {}).get("id")):
        text = str(value).strip() if isinstance(value, (int, str)) else ""
        if text.isdigit():
            return text
    return None
