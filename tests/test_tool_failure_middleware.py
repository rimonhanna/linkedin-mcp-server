"""A failure the masking would hide reaches the client named, with its notes."""

import asyncio
import re
from pathlib import Path

from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from linkedin_mcp_server.tool_failure_middleware import ToolFailureMiddleware


def _server(*, with_middleware: bool = True) -> FastMCP:
    mcp = FastMCP("test", mask_error_details=True)
    if with_middleware:
        mcp.add_middleware(ToolFailureMiddleware())

    @mcp.tool(timeout=0.05)
    async def slow_read() -> str:
        await asyncio.sleep(5)
        return "never"

    @mcp.tool
    async def broken_read() -> str:
        raise KeyError("a value the masking keeps out of the reply")

    @mcp.tool
    async def shaped_read() -> str:
        raise ToolError("A message the tool shaped itself.")

    return mcp


async def _error_text(mcp: FastMCP, tool: str) -> str:
    async with Client(mcp) as client:
        result = await client.call_tool(tool, {}, raise_on_error=False)
    assert result.is_error
    return result.content[0].text  # type: ignore[union-attr]


def _notes(text: str) -> Path:
    match = re.search(r"Local diagnostic notes: (.+)", text)
    assert match, f"no failure notes named in: {text!r}"
    path = Path(match.group(1).strip())
    assert path.is_file()
    return path


async def test_without_it_a_timeout_is_only_the_generic_mask():
    """The behaviour this replaces, so the assertions below cannot pass by default."""
    text = await _error_text(_server(with_middleware=False), "slow_read")
    assert text == "Error calling tool 'slow_read'"


async def test_a_timeout_says_the_call_ran_out_of_time_and_writes_notes():
    text = await _error_text(_server(), "slow_read")

    assert text.startswith("slow_read did not finish within its time limit")
    assert "timed out after 0.05s" in text
    assert "nothing it read was returned" in text
    assert "McpError" in _notes(text).read_text()


async def test_an_unexpected_exception_is_named_without_its_text():
    text = await _error_text(_server(), "broken_read")

    assert text.startswith("broken_read failed with an unexpected KeyError.")
    assert "a value the masking keeps out" not in text
    assert "KeyError" in _notes(text).read_text()


async def test_a_shaped_tool_error_passes_unchanged():
    text = await _error_text(_server(), "shaped_read")
    assert text == "A message the tool shaped itself."


def test_installed_only_where_the_tools_run():
    from test_server import _server_for

    from linkedin_mcp_server.server_role import ServerRole

    for role in ServerRole:
        mcp = _server_for(role)
        installed = any(isinstance(m, ToolFailureMiddleware) for m in mcp.middleware)
        assert installed == role.drives_browser, role
