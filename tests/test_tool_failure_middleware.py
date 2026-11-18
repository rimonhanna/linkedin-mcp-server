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

    @mcp.tool(timeout=0.05, annotations={"readOnlyHint": True})
    async def slow_read() -> str:
        await asyncio.sleep(5)
        return "never"

    @mcp.tool(annotations={"readOnlyHint": True})
    async def broken_read() -> str:
        raise KeyError("a value the masking keeps out of the reply")

    @mcp.tool(annotations={"readOnlyHint": True})
    async def shaped_read() -> str:
        raise ToolError("A message the tool shaped itself.")

    @mcp.tool(annotations={"readOnlyHint": True})
    async def counted_read(count: int) -> str:
        return str(count)

    @mcp.tool(timeout=0.05)
    async def slow_send() -> str:
        await asyncio.sleep(5)
        return "never"

    @mcp.tool
    async def broken_send() -> str:
        raise KeyError("a value the masking keeps out of the reply")

    @mcp.tool(annotations={"readOnlyHint": True})
    async def lookalike_read() -> str:
        from mcp.shared.exceptions import McpError
        from mcp.types import ErrorData

        raise McpError(ErrorData(code=-32000, message="Upstream timed out"))

    return mcp


async def _error_text(mcp: FastMCP, tool: str, args: dict | None = None) -> str:
    async with Client(mcp) as client:
        result = await client.call_tool(tool, args or {}, raise_on_error=False)
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
    assert "Nothing it read was returned" in text
    assert "Ask for less in one call" in text
    assert "check LinkedIn" not in text
    assert "notes below record them" not in text
    assert "McpError" in _notes(text).read_text()


async def test_an_unexpected_exception_is_named_without_its_text():
    text = await _error_text(_server(), "broken_read")

    assert text.startswith("broken_read failed with an unexpected KeyError.")
    assert "a value the masking keeps out" not in text
    assert "check LinkedIn" not in text
    assert "the local notes below record them" in text
    assert "KeyError" in _notes(text).read_text()


async def test_a_write_that_timed_out_says_check_linkedin_before_retrying():
    """A write may have reached LinkedIn before it was stopped; a retry resends it."""
    text = await _error_text(_server(), "slow_send")

    assert text.startswith("slow_send did not finish within its time limit")
    assert "may or may not have taken effect" in text
    assert "check LinkedIn before calling it again" in text
    assert "Ask for less" not in text
    assert "Nothing it read" not in text
    _notes(text)


async def test_a_write_that_failed_says_check_linkedin_before_retrying():
    text = await _error_text(_server(), "broken_send")

    assert text.startswith("broken_send failed with an unexpected KeyError.")
    assert "check LinkedIn before calling it again" in text
    assert "a value the masking keeps out" not in text


async def test_only_fastmcps_own_wording_counts_as_a_timeout():
    text = await _error_text(_server(), "lookalike_read")

    assert text.startswith("lookalike_read failed with an unexpected McpError.")
    assert "time limit" not in text
    assert "Upstream timed out" not in text


async def test_notes_sentence_only_when_notes_were_written(monkeypatch):
    from linkedin_mcp_server import tool_failure_middleware

    def no_notes(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(tool_failure_middleware, "build_issue_diagnostics", no_notes)
    text = await _error_text(_server(), "broken_read")

    assert text == "broken_read failed with an unexpected KeyError."


async def test_an_argument_validation_error_passes_unchanged():
    with_it = await _error_text(_server(), "counted_read", {"count": "many"})
    without = await _error_text(
        _server(with_middleware=False), "counted_read", {"count": "many"}
    )

    assert with_it == without
    assert "count" in with_it
    assert "Diagnostics:" not in with_it


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
