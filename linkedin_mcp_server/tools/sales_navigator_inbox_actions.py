"""
LinkedIn Sales Navigator send tools.

A new message to a lead and a reply in a Sales Navigator conversation. Both
act as the user, need ``confirm_send``, and a send that spends an InMail
credit, or may spend one, also needs ``spend_inmail_credit``. They have their own daily cap in
the pacing ledger, beside the regular messages cap.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import anyio
from fastmcp import Context, FastMCP

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.debug_trace import retain_trace_for_failed_action
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.pacing import (
    SALES_NAVIGATOR_MESSAGES,
    JobStore,
    record_action,
    refuse_action,
    request_arrived_at,
)
from linkedin_mcp_server.scraping.contracts import (
    SEND_INTERRUPTED_WARNING,
    before_the_reply_deadline,
)
from linkedin_mcp_server.scraping.identifiers import (
    normalize_sales_lead_id,
    normalize_sales_thread_id,
)
from linkedin_mcp_server.scraping.sales_navigator_send_contract import (
    INMAIL_CREDIT_REQUIRED,
    INMAIL_REPLY_COST_UNKNOWN,
    refuse_an_invalid_sales_message,
    refuse_an_invalid_sales_reply,
)

logger = logging.getLogger(__name__)

_ANNOTATIONS = {"destructiveHint": True, "openWorldHint": True}
_TAGS = {"sales_navigator", "actions"}
# Outcomes that need no explaining: a confirmed send, and a dry run or credit
# gate that verified its target. Every other status keeps the run's trace.
_SEND_SUCCEEDED = frozenset(
    {
        "sent",
        "sent_flattened",
        "confirmation_required",
        INMAIL_CREDIT_REQUIRED,
        INMAIL_REPLY_COST_UNKNOWN,
    }
)
# The not-retry-safe statuses the sender only answers before its one click on
# Send: typing that lost focus or was interrupted, or a page that moved during
# typing. Typing cannot send by itself: text is inserted, never typed as a
# plain Enter, and each line break is Shift+Enter pressed on the message field
# itself, which the composer reads as a new line. Every other status that is
# not retry safe may follow the click and is counted.
_BEFORE_THE_CLICK = frozenset(
    {"compose_interact_failed", "recipient_resolution_failed"}
)


async def _send(
    ctx: Context,
    tool: str,
    *,
    confirm_send: bool,
    tool_timeout: float,
    progress: str,
    call: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Run one send the way send_message does, with the Sales Navigator cap."""
    await ctx.report_progress(progress=0, total=100, message=progress)
    # Budgeted from arrival, as send_message is.
    arrived = request_arrived_at.get()
    with anyio.fail_after(
        None if arrived is None else arrived + tool_timeout - time.monotonic()
    ):
        result = await call()
        # `retry_safe` is false from the moment a submission is attempted, so
        # an unconfirmed send is counted: a repeat can deliver it twice.
        if result.get("sent") or (
            result.get("retry_safe") is False
            and result.get("status") not in _BEFORE_THE_CLICK
        ):
            record_action(
                JobStore(), SALES_NAVIGATOR_MESSAGES, datetime.now().astimezone()
            )
        retain_trace_for_failed_action(
            tool, result.get("status"), succeeded=_SEND_SUCCEEDED
        )
        try:
            # As send_message: no await once the deadline has passed.
            if anyio.current_time() < anyio.current_effective_deadline():
                with before_the_reply_deadline():
                    await ctx.report_progress(
                        progress=100, total=100, message="Complete"
                    )
        except BaseException:
            if result.get("retry_safe") is False:
                logger.warning(SEND_INTERRUPTED_WARNING)
            raise
    return result


def register_sales_navigator_inbox_action_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register the Sales Navigator send tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Send Sales Navigator Message",
        annotations=_ANNOTATIONS,
        tags=_TAGS,
        exclude_args=["extractor"],
    )
    async def send_sales_navigator_message(
        lead_id: str,
        message: str,
        confirm_send: bool,
        ctx: Context,
        subject: str | None = None,
        spend_inmail_credit: bool = False,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Send a new Sales Navigator message to one lead, a connection or not.

        Opens Sales Navigator's message box for the lead (a Sales Navigator
        page, not a lead page view, so no profile view is registered). The
        lead is verified from the page's own profile data and the page is
        pinned to that lead through every step. To a connection this is a
        free message. To anyone else it is an InMail: it needs a subject and,
        unless the lead has an Open Profile, uses one InMail credit (a
        TeamLink colleague's cost is not yet confirmed, so it is treated as
        unknown), which needs spend_inmail_credit=true on top of
        confirm_send=true. A message into an existing conversation with
        someone who is not a connection has a cost the page does not show
        (creditCost null): it needs spend_inmail_credit=true and a known
        credit balance, and without the flag returns
        inmail_reply_cost_unknown. Use a dry
        run first: it types nothing and reports the message type, its cost
        and the credits left. A draft already in the box is left untouched
        (composer_occupied). This is a write operation when confirm_send is
        True.

        Args:
            lead_id: The lead's id as search_sales_navigator_leads or
                get_sales_navigator_inbox returned it, or its fs_salesProfile
                URN or /sales/lead/ URL
            message: Message text to send. Line breaks (LF or CRLF) are typed
                as Shift+Enter; three or more in a row collapse to one blank
                line. Other C0 control characters and DEL are rejected.
            confirm_send: Must be True to send. False is a dry run.
            ctx: FastMCP context for progress reporting
            subject: The InMail subject, one line. Required for an InMail;
                refused for a message to a connection, which has none.
            spend_inmail_credit: Must be True for a send that uses an InMail
                credit, or may use one. Without it such a send returns
                inmail_credit_required (or inmail_reply_cost_unknown) and
                nothing is typed.

        Returns:
            Dict with url, status, message, recipient_selected, sent,
            retry_safe, recipient ({leadId, name, degree}) and delivery
            ({kind: message, inmail, open_profile or teamlink, creditCost,
            creditsRemaining}, null until the message box showed the kind;
            creditCost is null when the cost is unknown). ``thread_id`` is
            present only on ``sent`` and ``sent_flattened``: the existing
            conversation with the lead, or null when there was none. ``sent``
            is true only after Sales Navigator's send request answered
            success. While ``retry_safe`` is false, calling again can deliver
            the message twice. Statuses of their own:
            ``inmail_credit_required``, ``no_inmail_credits``,
            ``inmail_credits_unknown``, ``inmail_reply_cost_unknown`` and
            ``subject_required``, all before anything is typed, and
            ``send_blocked_no_credits``: an Open Profile message
            whose Send Sales Navigator kept disabled while no InMail credits
            were left, so nothing was sent.
        """
        try:
            # Answered before a session is acquired, inside the `try` as in
            # send_message: an unusable lead id has to name the correction.
            refusal = refuse_an_invalid_sales_message(lead_id, message, subject)
            if refusal is not None:
                return refusal
            # A dry run submits nothing and owes nothing.
            if confirm_send:
                refuse_action(
                    JobStore(), SALES_NAVIGATOR_MESSAGES, datetime.now().astimezone()
                )
            normalize_sales_lead_id(lead_id)
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="send_sales_navigator_message"
            )
            logger.info(
                "Sending a Sales Navigator message (confirm_send=%s)", confirm_send
            )
            bound = extractor
            return await _send(
                ctx,
                "send_sales_navigator_message",
                confirm_send=confirm_send,
                tool_timeout=tool_timeout,
                progress="Sending Sales Navigator message",
                call=lambda: bound.send_sales_navigator_message(
                    lead_id,
                    message,
                    confirm_send=confirm_send,
                    subject=subject,
                    spend_inmail_credit=spend_inmail_credit,
                ),
            )
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "send_sales_navigator_message")
        except Exception as e:
            raise_tool_error(e, "send_sales_navigator_message")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Reply to Sales Navigator Conversation",
        annotations=_ANNOTATIONS,
        tags=_TAGS,
        exclude_args=["extractor"],
    )
    async def reply_to_sales_navigator_conversation(
        thread_id: str,
        message: str,
        confirm_send: bool,
        ctx: Context,
        spend_inmail_credit: bool = False,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Reply inside an existing Sales Navigator conversation.

        Opens /sales/inbox/<thread_id> and types into that conversation's own
        reply box, so the message lands in the same conversation. The page
        must load exactly that conversation and stay on it. Until the reply is
        confirmed the conversation's read state is left alone, so a dry run
        leaves an unread conversation unread. A reply box that asks for a
        subject would start a new InMail and is refused (composer_unavailable).
        A draft already in the box is left untouched (composer_occupied). A
        reply is free in a conversation with no InMail in it, or in an InMail
        the other side accepted or answered. In any other InMail its cost is
        unknown (creditCost null): it needs spend_inmail_credit=true and a
        known credit balance, and without the flag returns
        inmail_reply_cost_unknown. This is a write operation when
        confirm_send is True.

        Args:
            thread_id: Sales Navigator conversation id, or its /sales/inbox/<id>
                URL, as get_sales_navigator_inbox returned it
            message: Message text to send, read as send_sales_navigator_message
                reads it.
            confirm_send: Must be True to send. False is a dry run that
                verifies the conversation and reply box, types nothing, and
                says who the reply would go to.
            ctx: FastMCP context for progress reporting
            spend_inmail_credit: Must be True for a reply whose cost is
                unknown. Without it such a reply returns
                inmail_reply_cost_unknown and nothing is typed.

        Returns:
            Dict with url, status, message, recipient_selected, sent,
            retry_safe, thread_id, participant (the other participants' names,
            reported only) and delivery ({kind, creditCost, creditsRemaining}).
            ``sent`` and ``retry_safe`` mean what they mean for
            send_sales_navigator_message.
        """
        try:
            refusal = refuse_an_invalid_sales_reply(thread_id, message)
            if refusal is not None:
                return refusal
            if confirm_send:
                refuse_action(
                    JobStore(), SALES_NAVIGATOR_MESSAGES, datetime.now().astimezone()
                )
            thread_id = normalize_sales_thread_id(thread_id)
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="reply_to_sales_navigator_conversation"
            )
            logger.info(
                "Replying in a Sales Navigator conversation (confirm_send=%s)",
                confirm_send,
            )
            bound = extractor
            return await _send(
                ctx,
                "reply_to_sales_navigator_conversation",
                confirm_send=confirm_send,
                tool_timeout=tool_timeout,
                progress="Sending Sales Navigator reply",
                call=lambda: bound.reply_to_sales_navigator_conversation(
                    thread_id,
                    message,
                    confirm_send=confirm_send,
                    spend_inmail_credit=spend_inmail_credit,
                ),
            )
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "reply_to_sales_navigator_conversation")
        except Exception as e:
            raise_tool_error(e, "reply_to_sales_navigator_conversation")  # NoReturn
