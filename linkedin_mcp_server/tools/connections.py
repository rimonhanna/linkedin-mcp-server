"""
Connection tools: the member's own 1st-degree connections, newest first.
"""

import logging
import time
from datetime import date, datetime, timedelta
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.pacing import request_arrived_at
from linkedin_mcp_server.scraping.recent_connections import MAX_CONNECTIONS
from linkedin_mcp_server.tools.enrichment import DEADLINE_FRACTION

logger = logging.getLogger(__name__)

_DEFAULT_DAYS = 7
_MAX_DAYS = 365


def _since(since: str | None, days: int | None, today: date) -> date:
    """The earliest connection date asked for, from exactly one of the two."""
    if since is not None and days is not None:
        raise ToolError("Pass since or days, not both.")
    if since is None:
        return today - timedelta(days=_DEFAULT_DAYS if days is None else days)
    try:
        value = date.fromisoformat(since.strip())
    except ValueError:
        raise ToolError("since must be a date as YYYY-MM-DD.") from None
    if value > today:
        raise ToolError("since must not be in the future.")
    if value < today - timedelta(days=_MAX_DAYS):
        raise ToolError(f"since may be at most {_MAX_DAYS} days ago.")
    return value


def _deadline(tool_timeout: float) -> float:
    arrived = request_arrived_at.get()
    return (
        time.monotonic() if arrived is None else arrived
    ) + tool_timeout * DEADLINE_FRACTION


def register_connection_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register the connection tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="List Recent Connections",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "scraping"},
        exclude_args=["extractor"],
    )
    async def list_recent_connections(
        ctx: Context,
        since: str | None = None,
        days: Annotated[int | None, Field(ge=0, le=_MAX_DAYS)] = None,
        limit: Annotated[int, Field(ge=1, le=MAX_CONNECTIONS)] = 100,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        List the signed-in member's 1st-degree connections made on or after a
        date, newest first, from LinkedIn's own connections page ("Recently
        added"). Read-only: nothing is clicked or sent.

        LinkedIn shows each connection's date only as "Connected on <date>",
        so connectedOn is a day (connectedOnPrecision "day"), in the time zone
        LinkedIn rendered it in, with no time of day. The words are read in
        en-US only: on another interface language connectedOn is null,
        connectedText keeps what LinkedIn showed and paging stops after the
        first page (reason date_unreadable).

        One page load, then one more charged page load per further 10
        connections as the list scrolls. Paging stops at the first connection
        older than since.

        Args:
            ctx: FastMCP context for progress reporting
            since: Earliest connection date, YYYY-MM-DD in the member's local
                calendar (at most 365 days ago). Pass this or days.
            days: Connections from this many days ago until now (0 is today
                only, 1 includes yesterday). Default 7 when neither is given.
            limit: Most connections to return, 1 to 500 (default 100).

        Returns:
            Dict with url, since, sections (connections -> one line each),
            connections (name, headline, profileId, publicIdentifier,
            profileUrl, connectedText, connectedOn, connectedOnPrecision) and
            paging (count, pages, more, reason: since, limit, complete,
            deadline, date_unreadable, page_not_loaded, rate_limited, an
            action_limit or working-hours refusal, or account_cooldown until
            <time>). profileId is the id invite_to_follow_page takes as a
            person; publicIdentifier is the /in/ name. section_errors appears
            when a date could not be read (connected_on) or paging stopped on
            a rate limit (connections).
        """
        try:
            first = _since(since, days, datetime.now().astimezone().date())
            deadline = _deadline(tool_timeout)
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="list_recent_connections"
            )
            logger.info("Listing connections made since %s", first.isoformat())
            await ctx.report_progress(
                progress=0, total=100, message="Opening the connections page"
            )
            result = await extractor.list_recent_connections(
                first, limit=limit, deadline=deadline
            )
            await ctx.report_progress(progress=100, total=100, message="Complete")
            return result
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "list_recent_connections")
        except Exception as e:
            raise_tool_error(e, "list_recent_connections")  # NoReturn
