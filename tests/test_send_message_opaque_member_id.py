"""send_message takes an opaque member id as the recipient.

send_message takes the recipient from the landed profile page and its Message
action, so an ACo.../ACw... id is passed on. (connect_with_person resolves one
too, in ``tests/scraping/test_connect_target.py``.) Synthetic ids, at
LinkedIn's length of 39 characters.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import FastMCP
from fastmcp.tools import FunctionTool


SN_LEAD_ID = "ACwAAAExampleSalesNavigatorLeadId000001"
MEMBER_ID = "ACoAAAExampleMemberUrnIdentifier0000001"
SN_LEAD_URL = f"https://www.linkedin.com/in/{SN_LEAD_ID}"


def _register(module: str, register: str):
    mcp = FastMCP("test")
    getattr(__import__(module, fromlist=[register]), register)(mcp)
    return mcp


@pytest.mark.parametrize(
    ("username", "expected"),
    [(SN_LEAD_ID, SN_LEAD_ID), (MEMBER_ID, MEMBER_ID), (SN_LEAD_URL, SN_LEAD_ID)],
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
