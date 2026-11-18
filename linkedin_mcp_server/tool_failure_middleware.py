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

from linkedin_mcp_server.error_diagnostics import (
    build_issue_diagnostics,
    format_tool_error_with_diagnostics,
)

logger = logging.getLogger(__name__)


def _masked(exc: ToolError, name: str) -> BaseException | None:
    """The exception the masking replaced, or None for a shaped error."""
    if exc.__cause__ is None or str(exc) != f"Error calling tool {name!r}":
        return None
    return exc.__cause__


def _explain(cause: BaseException, name: str) -> str:
    if isinstance(cause, McpError) and "timed out" in cause.error.message:
        return (
            f"{name} did not finish within its time limit and was stopped "
            f"({cause.error.message}), so nothing it read was returned. "
            "Ask for less in one call; a read that pages takes a page to "
            "start from."
        )
    return (
        f"{name} failed with an unexpected {type(cause).__name__}. The details "
        "are kept out of this reply; the local notes below record them."
    )


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
            message = _explain(cause, name)
            logger.warning("%s", message)
            try:
                diagnostics = build_issue_diagnostics(
                    cause if isinstance(cause, Exception) else exc, context=name
                )
            except Exception:
                logger.debug("Could not build issue diagnostics", exc_info=True)
            else:
                message = format_tool_error_with_diagnostics(message, diagnostics)
            raise ToolError(message) from exc
