"""
LinkedIn Sales Navigator inbox read tools.

Every tool loads the Sales Navigator inbox in the signed-in member's browser
and reads the responses the page itself receives. They need a Sales Navigator
seat. The inbox opens its top conversation on its own; every POST under the
app's thread resource (mark read included) is aborted during every read, so
unread conversations stay unread.
"""

import logging
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import Field, StringConstraints

from linkedin_mcp_server.config.schema import (
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    PER_TOOL_TIMEOUT_SECONDS,
)
from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    InvalidReferenceError,
)
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping.identifiers import (
    normalize_sales_lead_id,
    normalize_sales_thread_id,
)
from linkedin_mcp_server.scraping.sales_navigator_inbox_payload import InboxFilter
from linkedin_mcp_server.tools.sales_navigator import _deadline

logger = logging.getLogger(__name__)


def register_sales_navigator_inbox_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register the Sales Navigator inbox read tools with the MCP server."""

    # Scrolling to a large limit does not fit the default; a larger configured
    # value wins.
    inbox_timeout = max(
        tool_timeout, PER_TOOL_TIMEOUT_SECONDS["get_sales_navigator_inbox"]
    )

    @mcp.tool(
        timeout=inbox_timeout,
        title="Get Sales Navigator Inbox",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"sales_navigator", "messaging", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_sales_navigator_inbox(
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=1000)] = 20,
        filter: InboxFilter = "all",
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        List recent conversations from the Sales Navigator inbox. Read-only.

        Needs a Sales Navigator seat. One page load, then the list is scrolled
        (20 conversations per scroll) until limit is reached, the view ends or
        the call's time limit is near.

        Sales Navigator opens the top conversation of any inbox view on its
        own, which also loads that participant's profile; every POST under the
        thread resource (mark read included) is blocked, so unread
        conversations stay unread (marked_read is true only if LinkedIn
        confirmed a mark-read anyway).

        Args:
            ctx: FastMCP context for progress reporting
            limit: Maximum number of conversations (1-1000, default 20)
            filter: The inbox view: all (default), unread, sent,
                inmail_pending, inmail_accepted, inmail_declined or archived

        Returns:
            Dict with url, sections (inbox -> one line per conversation),
            conversations (id, participants [leadId, memberUrn, name, degree],
            unread, unreadCount, archived, inmail, messageCount, lastMessageAt,
            lastMessagePreview, subject), paging (count, more, reason:
            complete, limit, time_budget, no_new_rows, not_scrollable or
            max_scrolls), marked_read and, when the read stopped early,
            section_errors.inbox. A conversation's id is its thread_id for
            get_sales_navigator_conversation; leadId is the id
            search_sales_navigator_leads returns.
        """
        try:
            deadline = _deadline(inbox_timeout)
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_sales_navigator_inbox"
            )
            logger.info("Fetching Sales Navigator inbox (limit=%d)", limit)

            await ctx.report_progress(
                progress=0, total=100, message="Loading Sales Navigator inbox"
            )

            result = await extractor.get_sales_navigator_inbox(
                limit, filter, deadline=deadline
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_sales_navigator_inbox")
        except Exception as e:
            raise_tool_error(e, "get_sales_navigator_inbox")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Sales Navigator Conversation",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"sales_navigator", "messaging", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_sales_navigator_conversation(
        ctx: Context,
        thread_id: str | None = None,
        lead_id: str | None = None,
        index: Annotated[int, Field(ge=0)] = 0,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Read one Sales Navigator conversation. Read-only.

        Provide thread_id (from get_sales_navigator_inbox or
        search_sales_navigator_conversations) or lead_id. A lead_id is matched
        only against the participants' lead ids in the inbox's own data, never
        by name, among the 200 most recent conversations; a lead with no
        conversation there (archived ones are not searched), or an index past
        the ones found, is refused rather than resolved to another thread. A
        miss is no proof there is no conversation; the error says when the
        scan stopped early. Needs a Sales Navigator seat. The newest 10
        messages come with the page; up to 30 older ones are read by repeating
        the page's own older-messages request.

        Sales Navigator opens the conversation on its
        own, which also loads that participant's profile; every POST under the
        thread resource (mark read included) is blocked, so unread
        conversations stay unread (marked_read is true only if LinkedIn
        confirmed a mark-read anyway, for this conversation or, with lead_id,
        for any conversation the inbox scan opened).

        Args:
            ctx: FastMCP context for progress reporting
            thread_id: Sales Navigator thread id, or its /sales/inbox/ URL
            lead_id: Lead id as search_sales_navigator_leads returned it (an
                fs_salesProfile URN or a /sales/lead/ URL is accepted too)
            index: 0-based choice when the lead has several conversations,
                most recent first. Ignored when thread_id is given.

        Returns:
            Dict with url, sections (conversation -> text), conversation (id,
            participants, unread, unreadCount, archived, inmail, messages
            oldest first [id, type, fromViewer, inmail, body, senderLeadId,
            senderName, sentAt, subject], history [batches, complete]) and
            marked_read.
        """
        if not thread_id and not lead_id:
            raise_tool_error(
                InvalidReferenceError("Provide at least one of thread_id or lead_id"),
                "get_sales_navigator_conversation",
            )
        try:
            deadline = _deadline(tool_timeout)
            if thread_id:
                thread_id = normalize_sales_thread_id(thread_id)
            else:
                lead_id = normalize_sales_lead_id(lead_id or "")
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_sales_navigator_conversation"
            )
            logger.info("Fetching Sales Navigator conversation (index=%d)", index)

            await ctx.report_progress(
                progress=0, total=100, message="Loading conversation"
            )

            result = await extractor.get_sales_navigator_conversation(
                thread_id, lead_id, index, deadline=deadline
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_sales_navigator_conversation")
        except Exception as e:
            raise_tool_error(e, "get_sales_navigator_conversation")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Sales Navigator Conversations",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"sales_navigator", "messaging", "search"},
        exclude_args=["extractor"],
    )
    async def search_sales_navigator_conversations(
        keywords: Annotated[
            str, StringConstraints(strip_whitespace=True, min_length=1)
        ],
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Search Sales Navigator messages by keyword. Read-only.

        Types the keywords into the inbox's own search box (Sales Navigator has
        no search URL) and clicks no result. Needs a Sales Navigator seat.

        Sales Navigator opens the top conversation of any inbox view on its
        own, which also loads that participant's profile; every POST under the
        thread resource (mark read included) is blocked, so unread
        conversations stay unread (marked_read is true only if LinkedIn
        confirmed a mark-read anyway).

        Args:
            keywords: Search keywords
            ctx: FastMCP context for progress reporting
            limit: Maximum number of conversations (1-50, default 20)

        Returns:
            Dict with url, sections (search_results -> one line per
            conversation), conversations (the same rows as
            get_sales_navigator_inbox), paging, marked_read and, when the read
            stopped early, section_errors.search_results.
        """
        if not any(char.isalnum() for char in keywords):
            raise_tool_error(
                InvalidReferenceError(
                    "keywords must contain at least one letter or digit"
                ),
                "search_sales_navigator_conversations",
            )
        try:
            deadline = _deadline(tool_timeout)
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="search_sales_navigator_conversations"
            )
            logger.info("Searching Sales Navigator conversations (limit=%d)", limit)

            await ctx.report_progress(
                progress=0, total=100, message="Searching Sales Navigator messages"
            )

            result = await extractor.search_sales_navigator_conversations(
                keywords, limit, deadline=deadline
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "search_sales_navigator_conversations")
        except Exception as e:
            raise_tool_error(e, "search_sales_navigator_conversations")  # NoReturn
