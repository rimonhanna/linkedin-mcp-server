"""list_recent_connections: the date it asks for and what it hands the extractor."""

from datetime import date, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import FunctionTool

from linkedin_mcp_server.sequential_tool_middleware import (
    SequentialToolExecutionMiddleware,
)
from linkedin_mcp_server.tools.connections import register_connection_tools


async def _tool() -> FunctionTool:
    mcp = FastMCP("test")
    register_connection_tools(mcp)
    tool = await mcp.get_tool("list_recent_connections")
    assert tool is not None
    return cast(FunctionTool, tool)


def _extractor() -> MagicMock:
    mock = MagicMock()
    mock.list_recent_connections = AsyncMock(return_value={"connections": []})
    return mock


def _today() -> date:
    return datetime.now().astimezone().date()


async def test_the_tool_is_a_paced_read():
    tool = await _tool()
    assert tool.annotations and tool.annotations.readOnlyHint
    assert (
        "list_recent_connections" not in SequentialToolExecutionMiddleware._WRITE_TOOLS
    )


@pytest.mark.parametrize(
    ("arguments", "expected_days_ago"),
    [({}, 7), ({"days": 1}, 1), ({"days": 0}, 0)],
)
async def test_days_count_back_from_today(mock_context, arguments, expected_days_ago):
    extractor = _extractor()
    await (await _tool()).fn(mock_context, extractor=extractor, **arguments)

    (since,), kwargs = extractor.list_recent_connections.call_args
    assert since == _today() - timedelta(days=expected_days_ago)
    assert kwargs["limit"] == 100
    assert kwargs["deadline"] is not None


async def test_since_is_passed_as_the_date_it_names(mock_context):
    extractor = _extractor()
    day = (_today() - timedelta(days=1)).isoformat()
    await (await _tool()).fn(mock_context, since=day, limit=40, extractor=extractor)

    (since,), kwargs = extractor.list_recent_connections.call_args
    assert since.isoformat() == day
    assert kwargs["limit"] == 40


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"since": "2026-11-26", "days": 1}, "not both"),
        ({"since": "26/11/2026"}, "YYYY-MM-DD"),
        ({"since": "9999-01-01"}, "future"),
        ({"since": "2000-01-01"}, "at most 365 days"),
    ],
)
async def test_a_bad_date_is_refused_before_the_browser(
    mock_context, arguments, message
):
    extractor = _extractor()
    with pytest.raises(ToolError, match=message):
        await (await _tool()).fn(mock_context, extractor=extractor, **arguments)
    extractor.list_recent_connections.assert_not_called()
