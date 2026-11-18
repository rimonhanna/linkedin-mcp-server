"""Say what failed when the masking would reply only ``Error calling tool``.

``mask_error_details=True`` turns every exception a tool does not shape into
``ToolError("Error calling tool '<name>'")`` (fastmcp ``server.py``), and that
includes FastMCP's own tool timeout, an ``McpError`` raised around the tool
body where no ``raise_tool_error`` can see it. The client is told nothing it
can act on and no failure notes are written. This names the failure, keeps
the exception's text out of the reply (the masking's reason), and writes the
notes the shaped errors get.
"""

from __future__ import annotations

import logging

import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from mcp.shared.exceptions import McpError

from linkedin_mcp_server.daemon_auth import a_repeat_could_change_something
from linkedin_mcp_server.error_diagnostics import (
    build_issue_diagnostics,
    format_tool_error_with_diagnostics,
)

logger = logging.getLogger(__name__)

# A write that failed after LinkedIn received it would be sent twice by a retry.
_CHECK_BEFORE_RETRY = (
    "It may or may not have taken effect on LinkedIn: check LinkedIn before "
    "calling it again."
)


def _masked(exc: ToolError, name: str) -> BaseException | None:
    """The exception the masking replaced, or None for a shaped error."""
    if exc.__cause__ is None or str(exc) != f"Error calling tool {name!r}":
        return None
    return exc.__cause__


def _timed_out(cause: BaseException, name: str) -> bool:
    """FastMCP's own tool timeout (``fastmcp/tools/function_tool.py``), by its wording."""
    return isinstance(cause, McpError) and cause.error.message.startswith(
        f"Tool {name!r} execution timed out"
    )


def _explain(cause: BaseException, name: str, *, writes: bool) -> str:
    if _timed_out(cause, name):
        assert isinstance(cause, McpError)
        what = (
            f"{name} did not finish within its time limit and was stopped "
            f"({cause.error.message})."
        )
        if writes:
            return f"{what} {_CHECK_BEFORE_RETRY}"
        return (
            f"{what} Nothing it read was returned. Ask for less in one call; a "
            "read that pages takes a page to start from."
        )
    what = f"{name} failed with an unexpected {type(cause).__name__}."
    return f"{what} {_CHECK_BEFORE_RETRY}" if writes else what


class ToolFailureMiddleware(Middleware):
    """Replace the masked generic tool error with what failed, and its notes."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        try:
            return await call_next(context)
        except ToolError as exc:
            name = context.message.name
            cause = _masked(exc, name)
            if cause is None:
                raise
            message = _explain(
                cause, name, writes=await a_repeat_could_change_something(context)
            )
            logger.warning("%s", message)
            try:
                diagnostics = build_issue_diagnostics(
                    cause if isinstance(cause, Exception) else exc, context=name
                )
            except Exception:
                logger.debug("Could not build issue diagnostics", exc_info=True)
            else:
                # Only here: the notes exist once the builder has returned.
                if not _timed_out(cause, name):
                    message += (
                        " The details are kept out of this reply; the local "
                        "notes below record them."
                    )
                message = format_tool_error_with_diagnostics(message, diagnostics)
            raise ToolError(message) from exc
