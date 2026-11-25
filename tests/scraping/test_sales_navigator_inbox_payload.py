"""Builders for the Sales Navigator inbox's API responses (synthetic data)."""

from __future__ import annotations

from typing import Any

from linkedin_mcp_server.scraping.sales_navigator_inbox_payload import (
    build_conversation_rows,
    build_older_messages,
    build_thread,
    render_conversation,
    render_conversations,
)

VIEWER = "urn:li:fs_salesProfile:(ACwAAA0000,NAME_SEARCH,v0)"
LEAD = "urn:li:fs_salesProfile:(ACwAAA0001,NAME_SEARCH,t1)"
UNKNOWN = "urn:li:fs_salesProfile:(ACwAAA0009,NAME_SEARCH,t9)"
VIEWER_ID = "ACwAAA0000"


def _profiles() -> list[dict[str, Any]]:
    return [
        {
            "entityUrn": VIEWER,
            "objectUrn": "urn:li:member:100000000",
            "fullName": "Example Viewer",
            "degree": 0,
        },
        {
            "entityUrn": LEAD,
            "objectUrn": "urn:li:member:100000001",
            "firstName": "Example",
            "lastName": "Lead",
            "degree": 2,
        },
    ]


def _message(
    message_id: str, delivered: int, author: str, **extra: Any
) -> dict[str, Any]:
    return {
        "id": message_id,
        "type": "MESSAGE",
        "author": author,
        "body": f"Body {message_id}\nsecond line",
        "deliveredAt": delivered,
        "attachments": [],
        **extra,
    }


def _thread(**extra: Any) -> dict[str, Any]:
    return {
        "id": "2-AAAA",
        "archived": False,
        "unreadMessageCount": 2,
        "totalMessageCount": 4,
        "nextPageStartsAt": 1_790_000_000_000,
        "restrictions": [],
        "participants": [VIEWER, LEAD],
        "messages": [
            _message(
                "m1",
                1_790_000_000_000,
                LEAD,
                type="INMAIL",
                subject="Example subject",
            )
        ],
        **extra,
    }


def _list(*threads: dict[str, Any]) -> dict[str, Any]:
    return {
        "data": {"elements": list(threads), "paging": {"count": 20, "start": 0}},
        "included": _profiles(),
    }


def test_rows_leave_out_the_viewer_by_degree_and_carry_the_latest_message():
    rows = build_conversation_rows(_list(_thread()), VIEWER_ID)

    assert rows == [
        {
            "id": "2-AAAA",
            "participants": [
                {
                    "leadId": "ACwAAA0001",
                    "memberUrn": "urn:li:member:100000001",
                    "name": "Example Lead",
                    "degree": 2,
                }
            ],
            "unread": True,
            "unreadCount": 2,
            "archived": False,
            "inmail": True,
            "messageCount": 4,
            "lastMessageAt": "2026-09-21T14:13:20Z",
            "lastMessagePreview": "Body m1 second line",
            "subject": "Example subject",
        }
    ]


def test_a_participant_missing_from_included_keeps_only_the_lead_id():
    rows = build_conversation_rows(
        _list(_thread(participants=[VIEWER, UNKNOWN])), VIEWER_ID
    )

    assert rows is not None
    assert rows[0]["participants"] == [{"leadId": "ACwAAA0009"}]


def test_participants_given_as_star_references_are_read():
    thread = _thread()
    thread["*participants"] = thread.pop("participants")

    rows = build_conversation_rows(_list(thread), VIEWER_ID)

    assert rows is not None
    assert [p["leadId"] for p in rows[0]["participants"]] == ["ACwAAA0001"]


def test_one_malformed_row_refuses_the_page():
    broken = _thread(id=None)

    assert build_conversation_rows(_list(_thread(), broken), VIEWER_ID) is None
    assert (
        build_conversation_rows(_list(_thread(unreadMessageCount="2")), VIEWER_ID)
        is None
    )
    assert (
        build_conversation_rows(_list(_thread(participants=["not-a-urn"])), VIEWER_ID)
        is None
    )
    assert build_conversation_rows({"elements": "x"}, VIEWER_ID) is None


def test_thread_messages_are_oldest_first_and_the_viewer_is_flagged():
    record = _thread(
        messages=[
            _message("m3", 3_000, VIEWER),
            _message("m2", 2_000, LEAD),
        ]
    )
    built = build_thread({"data": record, "included": _profiles()}, VIEWER_ID)

    assert built is not None
    conversation, _profiles_by_urn = built
    assert [m["id"] for m in conversation["messages"]] == ["m2", "m3"]
    assert [m["fromViewer"] for m in conversation["messages"]] == [False, True]
    assert conversation["messages"][0]["senderLeadId"] == "ACwAAA0001"
    assert conversation["messages"][0]["senderName"] == "Example Lead"
    assert conversation["history"] == {"batches": 1, "complete": True}


def test_a_full_first_batch_is_not_complete():
    messages = [_message(f"m{i}", 1_000 + i, LEAD) for i in range(10)]
    built = build_thread(
        {"data": _thread(messages=messages), "included": _profiles()}, VIEWER_ID
    )

    assert built is not None
    assert built[0]["history"] == {"batches": 1, "complete": False}


def test_older_messages_resolve_authors_through_the_thread_profiles():
    built = build_thread({"data": _thread(), "included": _profiles()}, VIEWER_ID)
    assert built is not None
    payload = {"data": {"elements": [_message("m0", 500, LEAD)]}, "included": []}

    older = build_older_messages(payload, built[1], VIEWER_ID)

    assert older is not None
    assert older[0]["senderName"] == "Example Lead"
    assert (
        build_older_messages({"data": {"elements": [{"id": "x"}]}}, {}, VIEWER_ID)
        is None
    )


def test_renders_one_line_per_row_and_the_thread_with_its_line_breaks():
    rows = build_conversation_rows(_list(_thread()), VIEWER_ID)
    assert rows is not None
    assert render_conversations(rows) == (
        "Example Lead (thread 2-AAAA): 2026-09-21T14:13:20Z, unread 2, "
        'InMail "Example subject", Body m1 second line'
    )
    built = build_thread({"data": _thread(), "included": _profiles()}, VIEWER_ID)
    assert built is not None
    text = render_conversation(built[0])
    assert "Subject: Example subject" in text
    assert "Body m1\nsecond line" in text
