"""connect_with_person and send_message refuse an opaque member id (synthetic ids).

Both confirm their result against the member's /in/<public identifier>/
address, which an ACo.../ACw... id never matches: a sent invitation would read
as unconfirmed and a retry could invite twice. The refusal comes before any
browser is acquired.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import FunctionTool

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.scraping.identifiers import refuse_opaque_member_id

SN_LEAD_ID = "ACwAAAExampleSalesLeadId0001"
MEMBER_ID = "ACoAAAExampleMemberUrnId00001"
OPAQUE = [
    SN_LEAD_ID,
    MEMBER_ID,
    f"https://www.linkedin.com/in/{SN_LEAD_ID}",
    f"https://www.linkedin.com/in/{MEMBER_ID}/",
]


async def _call(module: str, register: str, name: str, username: str) -> AsyncMock:
    """Call the tool with no extractor and return the browser acquirer."""
    mcp = FastMCP("test")
    getattr(__import__(module, fromlist=[register]), register)(mcp)
    tool = await mcp.get_tool(name)
    assert isinstance(tool, FunctionTool)
    acquire = AsyncMock(side_effect=AssertionError("a browser was acquired"))
    with patch(f"{module}.get_ready_extractor", acquire):
        ctx = MagicMock()
        ctx.report_progress = AsyncMock()
        with pytest.raises(ToolError, match="public identifier") as raised:
            if name == "connect_with_person":
                await tool.fn(username, ctx)
            else:
                await tool.fn(username, "Hello", True, ctx)
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


@pytest.mark.parametrize("username", OPAQUE)
async def test_send_message_refuses_an_opaque_id_before_the_browser(username):
    acquire = await _call(
        "linkedin_mcp_server.tools.messaging",
        "register_messaging_tools",
        "send_message",
        username,
    )

    acquire.assert_not_awaited()


@pytest.mark.parametrize(
    "username",
    [
        "williamhgates",
        "acorn-smith",
        # Opaque-looking prefix, but too short to be an id LinkedIn mints.
        "ACoolName",
        "ACwrite",
    ],
)
def test_a_public_identifier_passes(username):
    refuse_opaque_member_id(username, tool="connect_with_person")


def test_the_refusal_names_the_tool_and_the_fix():
    with pytest.raises(InvalidReferenceError) as raised:
        refuse_opaque_member_id(SN_LEAD_ID, tool="send_message")

    message = str(raised.value)
    assert message.startswith("send_message needs the member's public identifier")
    assert "Sales Navigator lead id" in message
