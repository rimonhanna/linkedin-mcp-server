"""Sales Navigator inbox read tools: argument handling and delegation."""

from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import FunctionTool

from linkedin_mcp_server.config.schema import PER_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.scraping.sales_navigator_inbox_payload import FILTERS
from linkedin_mcp_server.tools.sales_navigator_inbox import (
    register_sales_navigator_inbox_tools,
)

TOOLS = (
    "get_sales_navigator_inbox",
    "get_sales_navigator_conversation",
    "search_sales_navigator_conversations",
)
RESULT = {"url": "https://www.linkedin.com/sales/inbox", "sections": {}}


def _extractor() -> MagicMock:
    mock = MagicMock()
    for name in TOOLS:
        setattr(mock, name, AsyncMock(return_value=RESULT))
    return mock


async def _tool(name: str):
    mcp = FastMCP("test")
    register_sales_navigator_inbox_tools(mcp)
    tool = await mcp.get_tool(name)
    assert tool is not None
    return tool


async def _fn(name: str):
    return cast(FunctionTool, await _tool(name)).fn


@pytest.fixture
def ready():
    mock = AsyncMock(return_value=_extractor())
    with patch(
        "linkedin_mcp_server.tools.sales_navigator_inbox.get_ready_extractor", mock
    ):
        yield mock


@pytest.mark.parametrize("name", TOOLS)
async def test_tools_are_read_only_and_say_unread_stays_unread(name):
    tool = await _tool(name)
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is True
    assert "sales_navigator" in tool.tags
    description = " ".join((tool.description or "").split())
    assert "unread conversations stay unread" in description


async def test_inbox_offers_exactly_the_url_filters_and_its_own_timeout():
    tool = await _tool("get_sales_navigator_inbox")
    assert tool.parameters["properties"]["filter"]["enum"] == list(FILTERS)
    assert tool.timeout == PER_TOOL_TIMEOUT_SECONDS["get_sales_navigator_inbox"]


async def test_inbox_delegates_with_a_deadline(ready):
    fn = await _fn("get_sales_navigator_inbox")
    result = await fn(MagicMock(report_progress=AsyncMock()), 30, "unread")
    extractor = ready.return_value
    assert result == RESULT
    call = extractor.get_sales_navigator_inbox.await_args
    assert call.args == (30, "unread")
    assert isinstance(call.kwargs["deadline"], float)


async def test_conversation_needs_a_thread_or_a_lead(ready):
    fn = await _fn("get_sales_navigator_conversation")
    with pytest.raises(ToolError, match="thread_id or lead_id"):
        await fn(MagicMock(report_progress=AsyncMock()))
    ready.assert_not_awaited()


async def test_conversation_normalizes_ids_before_the_browser(ready):
    fn = await _fn("get_sales_navigator_conversation")
    ctx = MagicMock(report_progress=AsyncMock())
    await fn(ctx, thread_id="https://www.linkedin.com/sales/inbox/2-abc")
    await fn(ctx, lead_id="urn:li:fs_salesProfile:(ACwAAA0001,NAME_SEARCH,x)", index=1)
    calls = ready.return_value.get_sales_navigator_conversation.await_args_list
    assert [call.args for call in calls] == [
        ("2-abc", None, 0),
        (None, "ACwAAA0001", 1),
    ]


async def test_a_bad_lead_id_is_refused_before_the_browser(ready):
    fn = await _fn("get_sales_navigator_conversation")
    with pytest.raises(ToolError, match="lead_id"):
        await fn(MagicMock(report_progress=AsyncMock()), lead_id="urn:li:member:1")
    ready.assert_not_awaited()


async def test_search_refuses_keywords_without_a_letter_or_digit(ready):
    fn = await _fn("search_sales_navigator_conversations")
    with pytest.raises(ToolError, match="letter or digit"):
        await fn("?!", MagicMock(report_progress=AsyncMock()))
    ready.assert_not_awaited()


async def test_search_delegates(ready):
    fn = await _fn("search_sales_navigator_conversations")
    await fn("example", MagicMock(report_progress=AsyncMock()), 5)
    call = ready.return_value.search_sales_navigator_conversations.await_args
    assert call.args == ("example", 5)


async def test_an_expired_session_goes_through_the_auth_handler(ready):
    ready.return_value.get_sales_navigator_inbox.side_effect = AuthenticationError(
        "expired"
    )
    fn = await _fn("get_sales_navigator_inbox")
    handler = AsyncMock(side_effect=ToolError("Sign in again"))
    with (
        patch(
            "linkedin_mcp_server.tools.sales_navigator_inbox.handle_auth_error",
            handler,
        ),
        pytest.raises(ToolError, match="Sign in again"),
    ):
        await fn(MagicMock(report_progress=AsyncMock()))
    handler.assert_awaited_once()
