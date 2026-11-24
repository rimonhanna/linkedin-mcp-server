"""Results, refusals and compose routes of the Sales Navigator send tools.

A Sales Navigator send answers with the regular send result
(``contracts.message_action_result`` / ``reply_action_result``) plus who it
went to and what it cost: ``recipient`` and ``delivery``. The refusals here
are answered before a browser is acquired.
"""

from __future__ import annotations

import re
from typing import Any, Literal, TypedDict
from urllib.parse import quote, unquote, urlparse

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
import linkedin_mcp_server.scraping.contracts as contracts
from linkedin_mcp_server.scraping.identifiers import (
    SALES_INBOX_URL,
    normalize_sales_lead_id,
    normalize_sales_thread_id,
    sales_inbox_thread_url,
)

DeliveryKind = Literal["message", "inmail", "open_profile"]

# Statuses of their own; every other status means what it means for
# send_message.
INMAIL_CREDIT_REQUIRED = "inmail_credit_required"
NO_INMAIL_CREDITS = "no_inmail_credits"
INMAIL_CREDITS_UNKNOWN = "inmail_credits_unknown"
SUBJECT_REQUIRED = "subject_required"

_SALES_PROFILE_URN = "urn:li:fs_salesProfile:("
# The two Rest.li parts after the profile id: how the lead was found and its
# token. Each is a word or an opaque token, never a comma or a parenthesis.
_URN_PART = re.compile(r"^[A-Za-z0-9_-]*$")
_COMPOSE_PATH = urlparse(SALES_INBOX_URL).path + "/mobile-compose/"


class Recipient(TypedDict, total=False):
    leadId: str
    name: str
    degree: int


class Delivery(TypedDict):
    kind: DeliveryKind
    creditCost: int
    creditsRemaining: int | None


def sales_profile_urn(lead_id: str) -> str:
    """The ``fs_salesProfile`` URN the composer route takes for ``lead_id``.

    A full URN, or a ``/sales/lead/<id>,<authType>,<token>`` URL, keeps its
    own parts. A bare id, as the search and inbox tools return it, gets empty
    ones: Sales Navigator resolves the lead from the id alone.
    """
    value = lead_id.strip()
    parts: list[str] | None = None
    if value.startswith(_SALES_PROFILE_URN) and value.endswith(")"):
        parts = value[len(_SALES_PROFILE_URN) : -1].split(",")
    elif "/sales/lead/" in value:
        segment = urlparse(value).path.split("/sales/lead/", 1)[1].split("/")[0]
        parts = unquote(segment).split(",")
    profile_id = normalize_sales_lead_id(value)
    if parts is None:
        parts = [profile_id, "", ""]
    if (
        len(parts) != 3
        or parts[0] != profile_id
        or not all(_URN_PART.fullmatch(part) for part in parts[1:])
    ):
        raise InvalidReferenceError(
            "lead_id is not a Sales Navigator lead id. Pass the id exactly as "
            "search_sales_navigator_leads or get_sales_navigator_inbox returned it."
        )
    return f"{_SALES_PROFILE_URN}{','.join(parts)})"


def sales_compose_url(profile_urn: str) -> str:
    """The new-message composer for one lead, addressed by its URN."""
    return f"https://www.linkedin.com{_COMPOSE_PATH}{quote(profile_urn, safe='')}"


def compose_route_urn(url: str) -> str | None:
    """The lead URN a composer URL addresses, or None for any other page."""
    parsed = urlparse(url)
    if parsed.netloc != "www.linkedin.com" or not parsed.path.startswith(_COMPOSE_PATH):
        return None
    segment = parsed.path[len(_COMPOSE_PATH) :]
    if not segment or "/" in segment.rstrip("/"):
        return None
    return unquote(segment.rstrip("/"))


def sales_message_result(
    url: str,
    status: str,
    message: str,
    *,
    recipient: Recipient,
    delivery: Delivery | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """``message_action_result`` plus the lead and what the send costs.

    ``fields`` are ``message_action_result``'s keywords. ``delivery`` is None
    until the composer showed what kind of message this is;
    ``creditsRemaining`` is the balance Sales Navigator reported, or None when
    it sent none.
    """
    return {
        **contracts.message_action_result(url, status, message, **fields),
        "recipient": recipient,
        "delivery": delivery,
    }


def sales_reply_result(
    url: str,
    status: str,
    message: str,
    *,
    thread_id: str,
    participant: str | None,
    delivery: Delivery | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """``reply_action_result`` plus what the reply costs."""
    return {
        **contracts.reply_action_result(
            url, status, message, thread_id=thread_id, participant=participant, **fields
        ),
        "delivery": delivery,
    }


def _invalid_subject_reason(subject: str | None) -> str | None:
    if subject is None:
        return None
    if not subject.strip():
        return "Subject must contain non-whitespace characters, or be left out."
    if any(ord(character) < 32 or ord(character) == 127 for character in subject):
        return "Subject must be one line, with no line breaks or control characters."
    return None


def refuse_an_invalid_sales_message(
    lead_id: str, message: str, subject: str | None
) -> dict[str, Any] | None:
    """The browser-free refusal for an unsafe message or subject."""
    reason = contracts.invalid_message_reason(message) or _invalid_subject_reason(
        subject
    )
    if reason is None:
        return None
    profile_urn = sales_profile_urn(lead_id)
    return sales_message_result(
        sales_compose_url(profile_urn),
        "invalid_message",
        reason,
        recipient={"leadId": normalize_sales_lead_id(lead_id)},
    )


def refuse_an_invalid_sales_reply(
    thread_id: str, message: str
) -> dict[str, Any] | None:
    """The same refusal for a reply, addressed to the conversation."""
    reason = contracts.invalid_message_reason(message)
    if reason is None:
        return None
    thread_id = normalize_sales_thread_id(thread_id)
    return sales_reply_result(
        sales_inbox_thread_url(thread_id),
        "invalid_message",
        reason,
        thread_id=thread_id,
        participant=None,
    )
