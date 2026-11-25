"""connect_with_person refuses an opaque member id; send_message takes one.

connect_with_person confirms its invite against the member's
/in/<public identifier>/ address, which an ACo.../ACw... id never matches: a
sent invitation would read as unconfirmed and a retry could invite twice. The
refusal comes before any browser is acquired. send_message takes the recipient
from the landed profile page and its Message action, so an id is passed on.
Synthetic ids, at LinkedIn's length of 39 characters.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import FunctionTool

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.scraping.identifiers import refuse_opaque_member_id

SN_LEAD_ID = "ACwAAAExampleSalesNavigatorLeadId000001"
MEMBER_ID = "ACoAAAExampleMemberUrnIdentifier0000001"
OPAQUE = [
    SN_LEAD_ID,
    MEMBER_ID,
    f"https://www.linkedin.com/in/{SN_LEAD_ID}",
    f"https://www.linkedin.com/in/{MEMBER_ID}/",
]


def _register(module: str, register: str):
    mcp = FastMCP("test")
    getattr(__import__(module, fromlist=[register]), register)(mcp)
    return mcp


async def _call(module: str, register: str, name: str, username: str) -> AsyncMock:
    """Call the tool with no extractor and return the browser acquirer."""
    tool = await _register(module, register).get_tool(name)
    assert isinstance(tool, FunctionTool)
    acquire = AsyncMock(side_effect=AssertionError("a browser was acquired"))
    with patch(f"{module}.get_ready_extractor", acquire):
        ctx = MagicMock()
        ctx.report_progress = AsyncMock()
        with pytest.raises(ToolError, match="public identifier") as raised:
            await tool.fn(username, ctx)
    assert "Nothing was sent" in str(raised.value)
    return acquire


@pytest.mark.parametrize("username", OPAQUE)
async def test_connect_with_person_refuses_an_opaque_id_before_the_browser(
    username,
):
    acquire = await _call(
        "linkedin_mcp_server.tools.person",
        "register_person_tools",
        "connect_with_person",
        username,
    )

    acquire.assert_not_awaited()


@pytest.mark.parametrize(
    ("username", "expected"),
    [(SN_LEAD_ID, SN_LEAD_ID), (MEMBER_ID, MEMBER_ID), (OPAQUE[2], SN_LEAD_ID)],
)
async def test_send_message_passes_a_profile_id_to_the_sender(username, expected):
    module = "linkedin_mcp_server.tools.messaging"
    tool = await _register(module, "register_messaging_tools").get_tool("send_message")
    assert isinstance(tool, FunctionTool)
    extractor = MagicMock()
    extractor.send_message = AsyncMock(
        return_value={"status": "confirmation_required", "retry_safe": True}
    )
    ctx = MagicMock()
    ctx.report_progress = AsyncMock()

    result = await tool.fn(username, "Hello", False, ctx, extractor=extractor)

    assert result["status"] == "confirmation_required"
    call = extractor.send_message.await_args
    assert call is not None
    assert call.args[0] == expected


@pytest.mark.parametrize(
    "username",
    [
        "williamhgates",
        "acorn-smith",
        # Opaque-looking prefix, but no id LinkedIn mints.
        "ACoolName",
        "ACwrite",
        "ACoAAron-smith",
        # The prefix ids carry, but not their length.
        "ACoAA" + "x" * 29,
        # Their length, but not the "AA" after ACo/ACw.
        "ACoBBExampleMemberUrnIdentifier00000001",
        # Case-sensitive: a lowercase prefix is a vanity name.
        SN_LEAD_ID.lower(),
    ],
)
def test_a_public_identifier_passes(username):
    refuse_opaque_member_id(username, tool="connect_with_person")


@pytest.mark.parametrize("username", [SN_LEAD_ID, MEMBER_ID, "ACwAA" + "x" * 30])
def test_an_id_of_linkedins_shape_is_refused(username):
    with pytest.raises(InvalidReferenceError):
        refuse_opaque_member_id(username, tool="connect_with_person")


def test_the_refusal_names_the_tool_and_the_fix():
    with pytest.raises(InvalidReferenceError) as raised:
        refuse_opaque_member_id(SN_LEAD_ID, tool="connect_with_person")

    message = str(raised.value)
    assert message.startswith(
        "connect_with_person needs the member's public identifier"
    )
    assert "Sales Navigator lead id" in message
