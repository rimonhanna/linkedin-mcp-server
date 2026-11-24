"""Pure builders for the Sales Navigator inbox's API responses.

The inbox answers from ``/sales-api/salesApiMessagingThreads`` in normalized
Rest.li (``data`` + ``included``): a thread carries its participants as
``fs_salesProfile`` URNs resolved through ``included``, and its messages
inline. The signed-in member is a participant of every thread and is told
apart by id, never by name (``viewer_of``). Builders return ``None`` on a
wrong shape and never raise.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal, NotRequired, TypedDict

from linkedin_mcp_server.scraping.restli import (
    as_int,
    as_mapping,
    as_str,
    envelope_elements,
    envelope_record,
    included_by_urn,
    iso_from_ms,
)
from linkedin_mcp_server.scraping.sales_navigator_payload import sales_profile_id

THREADS = "salesApiMessagingThreads"
# Boot calls: the app's chrome names the signed-in member, and the access
# check says whether the contract has an inbox at all.
NAV_CHROME = "salesApiNavChrome"
ACCESS = "salesApiAccess"
# The thread list's finders: the inbox view (``filter``) and the search box.
LIST_FINDER = "filter"
SEARCH_FINDER = "keyword"
# Messages per thread read and per older batch, as the page asks for them.
MESSAGE_BATCH = 10
_PREVIEW_LENGTH = 200
_VIEWER_DEGREE = 0
_MEMBER_URN = "urn:li:member:"
# Where the chrome may name the member. ``viewerSalesUrn`` is in the decoration
# the page asks for; the value's form is checked, not assumed.
_CHROME_VIEWER_KEYS = ("viewerSalesUrn", "member")

InboxFilter = Literal[
    "all",
    "unread",
    "sent",
    "inmail_pending",
    "inmail_accepted",
    "inmail_declined",
    "archived",
]
# Each view's ``?filter=`` in the page URL and the ``filter`` the page then
# sends to the API. "all" has no URL parameter.
FILTERS: Mapping[InboxFilter, tuple[str | None, str]] = {
    "all": (None, "INBOX"),
    "unread": ("UNREAD", "UNREAD"),
    "sent": ("SENT", "SENT"),
    "inmail_pending": ("PENDING", "INMAIL_PENDING"),
    "inmail_accepted": ("ACCEPTED", "INMAIL_ACCEPTED"),
    "inmail_declined": ("DECLINED", "INMAIL_DECLINED"),
    "archived": ("ARCHIVED", "ARCHIVED"),
}
# The decoration the page was measured sending for older messages, for when the
# page made no older-messages request of its own to replay.
MESSAGE_DECORATION = (
    "(id,type,author,attachments,contentFlag,deliveredAt,subject,body,"
    "footerText,blockCopy,systemMessageContent)"
)


class Participant(TypedDict):
    leadId: str
    memberUrn: NotRequired[str]
    name: NotRequired[str]
    degree: NotRequired[int]


class InboxConversation(TypedDict):
    id: str
    participants: list[Participant]
    unread: bool
    unreadCount: int
    archived: bool
    inmail: bool
    messageCount: NotRequired[int]
    lastMessageAt: NotRequired[str]
    lastMessagePreview: NotRequired[str]
    subject: NotRequired[str]


class InboxMessage(TypedDict):
    id: str
    type: str
    fromViewer: bool
    inmail: bool
    body: str
    senderLeadId: NotRequired[str]
    senderName: NotRequired[str]
    sentAt: NotRequired[str]
    subject: NotRequired[str]
    deliveredAt: NotRequired[int]


class ThreadHistory(TypedDict):
    batches: int
    complete: bool


class SalesConversation(TypedDict):
    id: str
    participants: list[Participant]
    unread: bool
    unreadCount: int
    archived: bool
    inmail: bool
    messages: list[InboxMessage]
    history: ThreadHistory


def _ref(record: Mapping[str, Any], key: str) -> Any:
    """A field the normalized envelope may write inline or as ``*key``."""
    return record[key] if key in record else record.get(f"*{key}")


def _urns(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    urns = [item for item in value if isinstance(item, str)]
    return urns if len(urns) == len(value) else None


def _participant(
    urn: str, profiles: Mapping[str, dict[str, Any]]
) -> Participant | None:
    lead_id = sales_profile_id(urn)
    if lead_id is None:
        return None
    participant: Participant = {"leadId": lead_id}
    profile = profiles.get(urn, {})
    if (member := as_str(profile.get("objectUrn"))) is not None:
        participant["memberUrn"] = member
    name = as_str(profile.get("fullName")) or " ".join(
        part
        for part in (as_str(profile.get("firstName")), as_str(profile.get("lastName")))
        if part
    )
    if name:
        participant["name"] = name
    if (degree := as_int(profile.get("degree"))) is not None:
        participant["degree"] = degree
    return participant


def _is_viewer(urn: str, viewer: str | None) -> bool:
    return viewer is not None and sales_profile_id(urn) == viewer


def chrome_viewer(payload: Any) -> str | None:
    """The member the app's chrome names: a sales profile or a member URN."""
    record = envelope_record(payload)
    if record is None:
        return None
    for key in _CHROME_VIEWER_KEYS:
        value = as_str(_ref(record, key))
        if value is not None and (
            sales_profile_id(value) is not None or value.startswith(_MEMBER_URN)
        ):
            return value
    return None


def no_inbox_access(payload: Any) -> bool:
    """The access check says this contract has no Sales Navigator inbox."""
    record = envelope_record(payload)
    return record is not None and record.get("hasContractInboxAccess") is False


def _named_by(urn: str, profiles: Mapping[str, dict[str, Any]], chrome: str) -> bool:
    if (chrome_id := sales_profile_id(chrome)) is not None:
        return sales_profile_id(urn) == chrome_id
    return as_str(profiles.get(urn, {}).get("objectUrn")) == chrome


# Which viewer check failed. Only the first is not a conflict between signals:
# the chrome named someone, but no participant of the response is that member.
ViewerCheck = Literal[
    "chrome_names_no_participant",
    "chrome_names_several",
    "degree_zero_not_one",
    "degree_disagrees",
    "missing_from_a_thread",
]


def viewer_of(
    payload: Any, chrome: str | None
) -> tuple[str | None, ViewerCheck | None]:
    """The signed-in member's profile id in one thread or list response.

    The second value names the check that failed, or is None when the signals
    agree: the chrome's member (when the page loaded it), the ``degree`` 0
    participant, and the one participant present in every thread must all be
    the same person. A response with no thread names no viewer and fails no
    check.
    """
    elements = envelope_elements(payload)
    if elements is None:
        record = envelope_record(payload)
        elements = [record] if record is not None else []
    threads = [_urns(_ref(record, "participants")) or [] for record in elements]
    if not threads:
        return None, None
    profiles = included_by_urn(payload)
    everyone = {urn for thread in threads for urn in thread}
    by_degree = {
        sales_profile_id(urn)
        for urn in everyone
        if as_int(profiles.get(urn, {}).get("degree")) == _VIEWER_DEGREE
    }
    if chrome is not None:
        named = {
            sales_profile_id(urn)
            for urn in everyone
            if _named_by(urn, profiles, chrome)
        } - {None}
        if not named:
            return None, "chrome_names_no_participant"
        if len(named) > 1:
            return None, "chrome_names_several"
    else:
        named = by_degree - {None}
        if len(named) != 1:
            return None, "degree_zero_not_one"
    (viewer,) = named
    if not by_degree <= {viewer}:
        return None, "degree_disagrees"
    if not all(any(_is_viewer(urn, viewer) for urn in thread) for thread in threads):
        return None, "missing_from_a_thread"
    return viewer, None


def build_participants(
    value: Any, profiles: Mapping[str, dict[str, Any]], viewer: str | None
) -> list[Participant] | None:
    """The thread's other participants; the signed-in member is left out."""
    urns = _urns(value)
    if urns is None:
        return None
    participants: list[Participant] = []
    for urn in urns:
        if _is_viewer(urn, viewer):
            continue
        participant = _participant(urn, profiles)
        if participant is None:
            return None
        participants.append(participant)
    return participants


def build_message(
    element: Any, profiles: Mapping[str, dict[str, Any]], viewer: str | None
) -> InboxMessage | None:
    record = as_mapping(element)
    if record is None:
        return None
    message_id = as_str(record.get("id"))
    kind = as_str(record.get("type"))
    delivered = as_int(record.get("deliveredAt"))
    body = record.get("body", "")
    if message_id is None or kind is None or delivered is None:
        return None
    if not isinstance(body, str):
        return None
    message: InboxMessage = {
        "id": message_id,
        "type": kind,
        "fromViewer": False,
        "inmail": kind.startswith("INMAIL"),
        "body": body,
        "deliveredAt": delivered,
    }
    if (sent := iso_from_ms(delivered)) is not None:
        message["sentAt"] = sent
    if (subject := as_str(record.get("subject"))) is not None:
        message["subject"] = subject
    author = as_str(_ref(record, "author"))
    if author is not None:
        message["fromViewer"] = _is_viewer(author, viewer)
        if (sender := _participant(author, profiles)) is not None:
            message["senderLeadId"] = sender["leadId"]
            if "name" in sender:
                message["senderName"] = sender["name"]
    return message


def build_messages(
    value: Any, profiles: Mapping[str, dict[str, Any]], viewer: str | None
) -> list[InboxMessage] | None:
    """Messages oldest first; ``None`` when any one is malformed."""
    if not isinstance(value, list):
        return None
    messages: list[InboxMessage] = []
    for element in value:
        message = build_message(element, profiles, viewer)
        if message is None:
            return None
        messages.append(message)
    return sorted(messages, key=lambda message: message.get("deliveredAt", 0))


def _thread_facts(
    record: Mapping[str, Any],
    profiles: Mapping[str, dict[str, Any]],
    viewer: str | None,
) -> tuple[str, list[Participant], int, bool, list[InboxMessage]] | None:
    thread_id = as_str(record.get("id"))
    unread = as_int(record.get("unreadMessageCount"))
    archived = record.get("archived", False)
    participants = build_participants(_ref(record, "participants"), profiles, viewer)
    messages = build_messages(_ref(record, "messages"), profiles, viewer)
    if (
        thread_id is None
        or unread is None
        or not isinstance(archived, bool)
        or participants is None
        or messages is None
    ):
        return None
    return thread_id, participants, unread, archived, messages


def build_conversation_row(
    element: Any, profiles: Mapping[str, dict[str, Any]], viewer: str | None
) -> InboxConversation | None:
    record = as_mapping(element)
    if record is None:
        return None
    facts = _thread_facts(record, profiles, viewer)
    if facts is None:
        return None
    thread_id, participants, unread, archived, messages = facts
    row: InboxConversation = {
        "id": thread_id,
        "participants": participants,
        "unread": unread > 0,
        "unreadCount": unread,
        "archived": archived,
        "inmail": any(message["inmail"] for message in messages),
    }
    if (total := as_int(record.get("totalMessageCount"))) is not None:
        row["messageCount"] = total
    if messages:
        latest = messages[-1]
        if "sentAt" in latest:
            row["lastMessageAt"] = latest["sentAt"]
        if preview := " ".join(latest["body"].split())[:_PREVIEW_LENGTH]:
            row["lastMessagePreview"] = preview
        if "subject" in latest:
            row["subject"] = latest["subject"]
    return row


def build_conversation_rows(
    payload: Any, viewer: str | None
) -> list[InboxConversation] | None:
    """Every row of one thread-list response; ``None`` if any is malformed."""
    elements = envelope_elements(payload)
    if elements is None:
        return None
    profiles = included_by_urn(payload)
    rows: list[InboxConversation] = []
    for element in elements:
        row = build_conversation_row(element, profiles, viewer)
        if row is None:
            return None
        rows.append(row)
    return rows


def build_thread(
    payload: Any, viewer: str | None
) -> tuple[SalesConversation, Mapping[str, dict[str, Any]]] | None:
    """One thread (the single-thread response) and the profiles it names."""
    record = envelope_record(payload)
    if record is None:
        return None
    profiles = included_by_urn(payload)
    facts = _thread_facts(record, profiles, viewer)
    if facts is None:
        return None
    thread_id, participants, unread, archived, messages = facts
    conversation: SalesConversation = {
        "id": thread_id,
        "participants": participants,
        "unread": unread > 0,
        "unreadCount": unread,
        "archived": archived,
        "inmail": any(message["inmail"] for message in messages),
        "messages": messages,
        "history": {"batches": 1, "complete": len(messages) < MESSAGE_BATCH},
    }
    return conversation, profiles


def build_older_messages(
    payload: Any, profiles: Mapping[str, dict[str, Any]], viewer: str | None
) -> list[InboxMessage] | None:
    """One older batch (the ``/messages`` response), oldest first."""
    elements = envelope_elements(payload)
    if elements is None:
        return None
    return build_messages(elements, {**profiles, **included_by_urn(payload)}, viewer)


def _names(participants: Iterable[Participant]) -> str:
    names = [item.get("name") or f"lead {item['leadId']}" for item in participants]
    return ", ".join(names) or "(no other participant)"


def render_conversations(rows: Iterable[InboxConversation]) -> str:
    lines = []
    for row in rows:
        details = [
            row.get("lastMessageAt"),
            f"unread {row['unreadCount']}" if row["unread"] else None,
            "archived" if row["archived"] else None,
            f'InMail "{row["subject"]}"'
            if "subject" in row
            else ("InMail" if row["inmail"] else None),
            row.get("lastMessagePreview"),
        ]
        detail = ", ".join(part for part in details if part)
        head = f"{_names(row['participants'])} (thread {row['id']})"
        lines.append(f"{head}: {detail}" if detail else head)
    return "\n".join(lines)


def render_conversation(conversation: SalesConversation) -> str:
    lines = [f"Conversation with {_names(conversation['participants'])}"]
    for message in conversation["messages"]:
        sender = (
            "You"
            if message["fromViewer"]
            else message.get("senderName") or message.get("senderLeadId") or "Unknown"
        )
        when = message.get("sentAt", "")
        lines.append(f"[{when}] {sender}:")
        if "subject" in message:
            lines.append(f"Subject: {message['subject']}")
        lines.append(message["body"])
    return "\n".join(lines)


def strip_delivered_at(messages: list[InboxMessage]) -> list[InboxMessage]:
    """The messages as returned: ``deliveredAt`` is the read's own cursor."""
    for message in messages:
        message.pop("deliveredAt", None)
    return messages
