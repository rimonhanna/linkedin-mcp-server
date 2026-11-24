"""What the Sales Navigator send tools read from the page's own responses.

The lead's profile, the conversation, the InMail balance, the contract's
inbox access and the conversation the composer found with the lead; and the
app's send request, whose answer is the one confirmation of a send.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import unquote

from linkedin_mcp_server.scraping.restli import (
    as_int,
    as_mapping,
    as_str,
    envelope_elements,
    envelope_record,
    included_by_urn,
)
from linkedin_mcp_server.scraping.sales_navigator_api import (
    SalesNavigatorCapture,
    SnResponse,
)
from linkedin_mcp_server.scraping.sales_navigator_inbox_payload import (
    build_messages,
    viewer_of,
)
from linkedin_mcp_server.scraping.sales_navigator_payload import (
    SnCall,
    parse_sn_call,
    sales_profile_id,
)

_PROFILES = "salesApiProfiles"
THREADS = "salesApiMessagingThreads"
CREDITS = "salesApiCredits"
_ACCESS = "salesApiAccess"
_SEND = "salesApiMessageActions"
_SEND_ACTION = "createMessage"
_INMAIL_GRANT = "LSS_INMAIL"
# The message types that show the recipient took up an InMail.
_INMAIL_ANSWERS = frozenset({"INMAIL_ACCEPT", "INMAIL_REPLY"})
# The degree Sales Navigator gives the signed-in member in a thread.
_VIEWER_DEGREE = 0
CONNECTION_DEGREE = 1


def is_send(call: SnCall) -> bool:
    return (
        call.method == "POST"
        and call.resource == _SEND
        and call.params.get("action") == _SEND_ACTION
    )


class SendCapture(SalesNavigatorCapture):
    """The page's responses, plus the body and request of its send call."""

    def __init__(self, page: Any):
        super().__init__(
            page, resources=frozenset({_PROFILES, THREADS, CREDITS, _ACCESS})
        )
        self.send_requests: list[Any] = []

    def _extra_listeners(self) -> tuple[tuple[str, Callable[..., None]], ...]:
        return (("request", self._on_request),)

    def _on_request(self, request: Any) -> None:
        call = parse_sn_call(request.url, request.method)
        if call is not None and is_send(call):
            self.send_requests.append(request)

    def _wants_body(self, call: SnCall) -> bool:
        return is_send(call) or super()._wants_body(call)


def sent_body(request: Any) -> str | None:
    """The message text the send request carried, when it names exactly one."""
    try:
        payload = request.post_data_json
    except Exception:
        return None
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "body" and isinstance(item, str):
                    found.append(item)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return found[0] if len(found) == 1 else None


def is_profile(response: SnResponse) -> bool:
    """A profile the page loaded, of whoever it is."""
    call = response.call
    return response.ok and call.method == "GET" and call.resource == _PROFILES


def profile_of(response: SnResponse, lead_id: str) -> dict[str, Any] | None:
    """The lead's profile record, when ``response`` is the lead's own."""
    if not is_profile(response):
        return None
    record = envelope_record(response.payload)
    if record is None or sales_profile_id(record.get("entityUrn")) != lead_id:
        return None
    return record


def thread_of(response: SnResponse, thread_id: str) -> dict[str, Any] | None:
    """The conversation record, when ``response`` is that thread's own GET."""
    call = response.call
    if not (
        response.ok
        and call.method == "GET"
        and call.resource == THREADS
        and call.entity_id is not None
        and unquote(call.entity_id) == thread_id
        and call.sub_path is None
    ):
        return None
    record = envelope_record(response.payload)
    if record is None or as_str(record.get("id")) != thread_id:
        return None
    return record


def inmail_credits(responses: list[SnResponse]) -> int | None:
    """The InMail balance the page was given, or None when it was not."""
    for response in responses:
        call = response.call
        if not (response.ok and call.resource == CREDITS and call.method == "GET"):
            continue
        for element in envelope_elements(response.payload) or []:
            if element.get("type") == _INMAIL_GRANT:
                value = as_int(element.get("value"))
                if value is not None:
                    return value
    return None


def no_inbox_access(responses: list[SnResponse]) -> bool:
    """True when the page was told this contract has no Sales Navigator inbox."""
    for response in responses:
        if response.ok and response.call.resource == _ACCESS:
            record = as_mapping(response.payload)
            if record is not None and record.get("hasContractInboxAccess") is False:
                return True
    return False


def existing_thread(responses: list[SnResponse]) -> str | None:
    """The conversation the page found with the lead, from its recipients lookup."""
    for response in responses:
        call = response.call
        if response.ok and call.resource == THREADS and call.finder == "recipients":
            elements = envelope_elements(response.payload) or []
            if len(elements) == 1:
                return as_str(elements[0].get("id"))
    return None


def reply_cost(payload: Any, record: dict[str, Any]) -> tuple[bool, int | None]:
    """Whether the conversation is an InMail, and what a reply in it costs.

    Read from the conversation's own messages (their ``type``): a reply is
    free in a conversation with no InMail in it, or in an InMail the other
    side accepted or answered (an ``INMAIL_ACCEPT`` or ``INMAIL_REPLY`` from a
    participant who is not the signed-in member). Otherwise the cost is None,
    unknown and never assumed free: an InMail nobody answered, messages that
    cannot be read, or a signed-in member who cannot be told apart.
    """
    viewer, agrees = viewer_of(payload, None)
    messages = build_messages(
        record.get("messages", record.get("*messages")),
        included_by_urn(payload),
        viewer,
    )
    if not messages:
        return False, None
    if not any(message["inmail"] for message in messages):
        return False, 0
    answered = (
        viewer is not None
        and agrees
        and any(
            message["type"] in _INMAIL_ANSWERS
            and not message["fromViewer"]
            and "senderLeadId" in message
            for message in messages
        )
    )
    return True, 0 if answered else None


def merged_profile(responses: list[SnResponse], lead_id: str) -> dict[str, Any]:
    """The lead's profile across every profile response the page loaded.

    Responses differ in decoration, so a field one leaves out another may
    carry: ``memberBadges.openLink`` and ``colleague`` are true when any
    response says so, and ``degree`` is kept only when every response that
    gives one gives the same.
    """
    merged: dict[str, Any] = {}
    degrees: set[int] = set()
    open_link = False
    colleague = False
    for response in responses:
        record = profile_of(response, lead_id)
        if record is None:
            continue
        for key, value in record.items():
            merged.setdefault(key, value)
        if (degree := as_int(record.get("degree"))) is not None:
            degrees.add(degree)
        badges = as_mapping(record.get("memberBadges")) or {}
        open_link = open_link or badges.get("openLink") is True
        colleague = colleague or record.get("colleague") is True
    merged.pop("degree", None)
    if len(degrees) == 1:
        merged["degree"] = next(iter(degrees))
    merged["memberBadges"] = {
        **(as_mapping(merged.get("memberBadges")) or {}),
        "openLink": open_link,
    }
    merged["colleague"] = colleague
    return merged


def profile_name(profile: dict[str, Any]) -> str | None:
    return as_str(profile.get("fullName")) or (
        " ".join(
            part
            for part in (
                as_str(profile.get("firstName")),
                as_str(profile.get("lastName")),
            )
            if part
        )
        or None
    )


def thread_participant(payload: Any, record: dict[str, Any]) -> str | None:
    """The names of the conversation's other participants, for the answer only."""
    profiles = included_by_urn(payload)
    names: list[str] = []
    for urn in record.get("participants") or []:
        profile = profiles.get(urn) if isinstance(urn, str) else None
        if profile is None or as_int(profile.get("degree")) == _VIEWER_DEGREE:
            continue
        name = profile_name(profile)
        if name:
            names.append(name)
    return ", ".join(names) or None
