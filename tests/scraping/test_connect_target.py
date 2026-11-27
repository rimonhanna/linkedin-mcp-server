"""connect_with_person reads every form a member is named by, before any page.

The ids are synthetic but of LinkedIn's shape: base64url of a 4-byte header
(``00 2c 00 00`` for a Sales Navigator ``ACwAA`` id, ``00 2a 00 00`` for an
``ACoAA`` profile id), the member number, then bytes of the id's own. Measured
against 91 Sales Navigator leads on 2026-11-27: every ``ACwAA`` id decoded to
the number in the lead's own ``memberUrn``, and the one lead opened at
``/in/<id>/`` carried an ``ACoAA`` URN that decoded to the same number.
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import FunctionTool

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.scraping.connect_target import ConnectTarget, connect_target


def encoded_id(header: bytes, member: int, tail: bytes = b"\x01" * 21) -> str:
    raw = header + member.to_bytes(4, "big") + tail
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


MEMBER = 185140733
SN_LEAD_ID = encoded_id(b"\x00\x2c\x00\x00", MEMBER)
PROFILE_ID = encoded_id(b"\x00\x2a\x00\x00", MEMBER, b"\x02" * 21)
OTHER_PROFILE_ID = encoded_id(b"\x00\x2a\x00\x00", MEMBER + 1, b"\x02" * 21)


def test_the_synthetic_ids_have_linkedins_shape():
    assert SN_LEAD_ID.startswith("ACwAA") and len(SN_LEAD_ID) == 39
    assert PROFILE_ID.startswith("ACoAA") and len(PROFILE_ID) == 39


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("williamhgates", "williamhgates"),
        ("https://www.linkedin.com/in/williamhgates/", "williamhgates"),
        ("de.linkedin.com/in/williamhgates?trk=x", "williamhgates"),
        # Shaped like an id's start, but no id LinkedIn mints: a vanity name.
        ("ACoAAron-smith", "ACoAAron-smith"),
        ("ACMEcorp-smith-1234", "ACMEcorp-smith-1234"),
        ("ACoAA" + "x" * 29, "ACoAA" + "x" * 29),
        (SN_LEAD_ID.lower(), SN_LEAD_ID.lower()),
    ],
)
def test_a_public_identifier_stays_one(given, expected):
    assert connect_target(given) == expected


@pytest.mark.parametrize(
    "given",
    [
        SN_LEAD_ID,
        f"https://www.linkedin.com/in/{SN_LEAD_ID}",
        f"https://www.linkedin.com/in/{SN_LEAD_ID}/",
        f"/in/{SN_LEAD_ID}/",
        f"urn:li:fs_salesProfile:({SN_LEAD_ID},NAME_SEARCH,abcd)",
        f"https://www.linkedin.com/sales/lead/{SN_LEAD_ID},NAME_SEARCH,abcd",
    ],
)
def test_a_sales_navigator_lead_is_resolved_by_its_member_number(given):
    target = connect_target(given)

    assert isinstance(target, ConnectTarget)
    assert (target.profile_id, target.member_id, target.exact) == (
        SN_LEAD_ID,
        str(MEMBER),
        None,
    )


@pytest.mark.parametrize(
    "given",
    [
        PROFILE_ID,
        f"https://www.linkedin.com/in/{PROFILE_ID}/",
        f"urn:li:fsd_profile:{PROFILE_ID}",
    ],
)
def test_a_profile_id_is_resolved_and_matched_exactly(given):
    target = connect_target(given)

    assert target == ConnectTarget(given, PROFILE_ID, str(MEMBER), PROFILE_ID)


def test_a_profile_id_that_does_not_decode_is_still_matched_exactly():
    # The header's fourth byte is not zero, so no member number is read out.
    undecodable = encoded_id(b"\x00\x2a\x00\x03", MEMBER)
    assert undecodable.startswith("ACoAA")

    target = connect_target(undecodable)

    assert isinstance(target, ConnectTarget)
    assert target.member_id is None
    assert target.confirms(undecodable)
    assert not target.confirms(PROFILE_ID)


def test_a_lead_id_that_cannot_be_matched_is_refused():
    with pytest.raises(InvalidReferenceError, match="Nothing was sent"):
        connect_target(encoded_id(b"\x00\x2c\x00\x03", MEMBER))


def test_a_member_urn_is_refused_with_the_way_out():
    with pytest.raises(InvalidReferenceError) as raised:
        connect_target(f"urn:li:member:{MEMBER}")

    assert "lead's id" in str(raised.value)
    assert "Nothing was sent" in str(raised.value)


@pytest.mark.parametrize(
    "given",
    ["me", "https://www.linkedin.com/company/microsoft/", "urn:li:organization:1"],
)
def test_what_names_no_member_is_refused(given):
    with pytest.raises(InvalidReferenceError):
        connect_target(given)


def test_the_page_confirms_only_the_member_asked_for():
    target = connect_target(SN_LEAD_ID)
    assert isinstance(target, ConnectTarget)

    assert target.confirms(PROFILE_ID)
    assert not target.confirms(OTHER_PROFILE_ID)
    assert not target.confirms(None)
    assert not target.confirms("")


async def test_a_member_urn_is_refused_before_the_browser():
    mcp = FastMCP("test")
    from linkedin_mcp_server.tools.person import register_person_tools

    register_person_tools(mcp)
    tool = await mcp.get_tool("connect_with_person")
    assert isinstance(tool, FunctionTool)
    acquire = AsyncMock(side_effect=AssertionError("a browser was acquired"))
    ctx = MagicMock()
    ctx.report_progress = AsyncMock()
    with (
        patch("linkedin_mcp_server.tools.person.get_ready_extractor", acquire),
        patch("linkedin_mcp_server.tools.person.refuse_action"),
        pytest.raises(ToolError, match="Nothing was sent"),
    ):
        await tool.fn(f"urn:li:member:{MEMBER}", ctx)

    acquire.assert_not_awaited()


async def test_the_tool_hands_the_target_to_the_extractor():
    mcp = FastMCP("test")
    from linkedin_mcp_server.tools.person import register_person_tools

    register_person_tools(mcp)
    tool = await mcp.get_tool("connect_with_person")
    assert isinstance(tool, FunctionTool)
    extractor = MagicMock()
    extractor.connect_with_person = AsyncMock(
        return_value={"status": "identity_unconfirmed", "note_sent": False}
    )
    ctx = MagicMock()
    ctx.report_progress = AsyncMock()
    with (
        patch("linkedin_mcp_server.tools.person.refuse_action"),
        patch("linkedin_mcp_server.tools.person.record_action") as record,
    ):
        result = await tool.fn(
            f"https://www.linkedin.com/in/{SN_LEAD_ID}", ctx, extractor=extractor
        )

    assert result["status"] == "identity_unconfirmed"
    call = extractor.connect_with_person.await_args
    assert call is not None
    assert call.args[0] == ConnectTarget(
        f"https://www.linkedin.com/in/{SN_LEAD_ID}", SN_LEAD_ID, str(MEMBER), None
    )
    record.assert_not_called()
