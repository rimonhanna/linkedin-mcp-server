"""The Sales Navigator list tools hand the reader a deadline inside their timeout."""

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import FastMCP
from fastmcp.tools import FunctionTool

from linkedin_mcp_server.pacing import request_arrived_at
from linkedin_mcp_server.tools.enrichment import DEADLINE_FRACTION
from linkedin_mcp_server.tools.sales_navigator import (
    register_sales_navigator_tools,
)

TIMEOUT = 200.0
ARRIVED = 1000.0
RESULT = {"url": "https://www.linkedin.com/sales/lists/company", "sections": {}}


async def _fn(name: str):
    mcp = FastMCP("test")
    register_sales_navigator_tools(mcp, tool_timeout=TIMEOUT)
    tool = await mcp.get_tool(name)
    assert tool is not None
    return cast(FunctionTool, tool).fn


@pytest.fixture
def arrived():
    token = request_arrived_at.set(ARRIVED)
    yield
    request_arrived_at.reset(token)


@pytest.mark.usefixtures("arrived")
async def test_account_list_deadline_counts_from_arrival(mock_context):
    extractor = MagicMock()
    extractor.get_sales_navigator_account_list = AsyncMock(return_value=RESULT)
    fn = await _fn("get_sales_navigator_account_list")

    await fn("100000001", mock_context, page=3, extractor=extractor)

    extractor.get_sales_navigator_account_list.assert_awaited_once_with(
        "100000001", 3, deadline=ARRIVED + TIMEOUT * DEADLINE_FRACTION
    )


@pytest.mark.usefixtures("arrived")
async def test_account_lists_deadline_counts_from_arrival(mock_context):
    extractor = MagicMock()
    extractor.list_sales_navigator_account_lists = AsyncMock(return_value=RESULT)
    fn = await _fn("list_sales_navigator_account_lists")

    await fn(mock_context, extractor=extractor)

    extractor.list_sales_navigator_account_lists.assert_awaited_once_with(
        1, deadline=ARRIVED + TIMEOUT * DEADLINE_FRACTION
    )


@pytest.mark.parametrize(
    "name", ["get_sales_navigator_account_list", "list_sales_navigator_account_lists"]
)
async def test_list_tools_say_a_call_stops_near_its_time_limit(name):
    mcp = FastMCP("test")
    register_sales_navigator_tools(mcp)
    tool = await mcp.get_tool(name)
    assert tool is not None
    description = " ".join((tool.description or "").split())
    assert "the call's time limit is near" in description
