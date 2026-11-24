"""
LinkedIn Sales Navigator account-list write tools.

Each tool loads one Sales Navigator page in the signed-in member's browser to
verify its target, then lets the one planned request through and nothing else
(see ``sales_navigator_list_guard``). ``confirm_send=False`` is a dry run that
changes nothing.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import anyio
from fastmcp import Context, FastMCP

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.pacing import (
    SALES_LIST_WRITES,
    JobStore,
    load_account_budget,
    refuse_action,
    refuse_if_limited,
)
from linkedin_mcp_server.scraping.contracts import before_the_reply_deadline
from linkedin_mcp_server.scraping.identifiers import normalize_sales_list_id
from linkedin_mcp_server.scraping.sales_navigator_list_payload import (
    refuse_invalid_list_text,
)
from linkedin_mcp_server.scraping.sales_navigator_list_writes import (
    list_write_result,
    plan_accounts,
    tracking_list_writes,
)
from linkedin_mcp_server.tools.person import StrList

logger = logging.getLogger(__name__)

_ANNOTATIONS = {"destructiveHint": True, "openWorldHint": True}
_TAGS = {"sales_navigator", "actions"}


def _refuse_before_the_browser(confirm_send: bool, *, requests: int = 1) -> None:
    """Refuse a call whose ``requests`` do not all fit under the cap now."""
    # A dry run sends nothing and owes nothing.
    if not confirm_send:
        return
    now = datetime.now().astimezone()
    refuse_action(JobStore(), SALES_LIST_WRITES, now)
    if requests > 1:
        # A create with accounts sends two: refused here, a create that
        # leaves the accounts behind on the cap is not started.
        budget = load_account_budget(JobStore(), now)
        for _ in range(requests - 1):
            budget.ledger.record_kind(SALES_LIST_WRITES, now)
        refuse_if_limited(budget, SALES_LIST_WRITES, now)


async def _run(
    ctx: Context,
    tool: str,
    *,
    confirm_send: bool,
    requests: int,
    extractor: Any | None,
    message: str,
    call: Callable[[Any], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    _refuse_before_the_browser(confirm_send, requests=requests)
    extractor = extractor or await get_ready_extractor(ctx, tool_name=tool)
    logger.info("%s (confirm_send=%s)", tool, confirm_send)
    await ctx.report_progress(progress=0, total=100, message=message)
    # The one place an error after an admitted request becomes an outcome.
    # Each admitted request was counted by its window as it left, so a
    # cancellation (the tool timeout) needs nothing here and goes on up.
    with tracking_list_writes(tool) as ledger:
        try:
            result = await call(extractor)
        except Exception as exc:
            if not ledger.admitted:
                raise
            logger.warning("%s failed after a request was sent", tool, exc_info=True)
            result = ledger.unconfirmed(exc)
    try:
        # The last await before the deadline: landing there would discard a
        # result that may say a write landed, so it gives up first, and is
        # skipped once the deadline has passed (see send_message).
        if anyio.current_time() < anyio.current_effective_deadline():
            with before_the_reply_deadline():
                await ctx.report_progress(progress=100, total=100, message="Complete")
    except BaseException:
        if result.get("retry_safe") is False:
            logger.warning(
                "%s was interrupted after a request was sent; check Sales "
                "Navigator before calling it again",
                tool,
            )
        raise
    return result


def register_sales_navigator_list_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register the Sales Navigator account-list write tools."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Create Sales Navigator Account List",
        annotations=_ANNOTATIONS,
        tags=_TAGS,
        exclude_args=["extractor"],
    )
    async def create_sales_navigator_account_list(
        name: str,
        confirm_send: bool,
        ctx: Context,
        description: str | None = None,
        accounts: StrList | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Create a Sales Navigator account list owned by the signed-in member,
        optionally with accounts added to it. Needs a Sales Navigator seat.

        confirm_send False is a dry run: nothing is created or added, and the
        result reports the planned list and each account's status
        (would_add, duplicate). confirm_send True creates the list through
        Sales Navigator's own create-list form, reads the new list back from
        its page, then adds the accounts in one request, as
        add_accounts_to_sales_navigator_list does. Status created is only
        reported from Sales Navigator's answer and that read-back, and stays
        created whatever happened to the accounts: accounts_status then says
        how the add went (added, partly_added, add_unconfirmed,
        refused_by_linkedin, not_sent). When an outcome is unknown, retry_safe
        is false: check list_sales_navigator_account_lists before calling
        again, or the list can be created twice. Each request sent counts
        against the daily sales_list_writes cap, and a call with accounts
        needs room for both.

        Args:
            name: The list's name, as it will show in Sales Navigator, up to 75
                characters, one line.
            confirm_send: False for a dry run; True to create the list.
            ctx: FastMCP context for progress reporting
            description: Optional list description, up to 350 characters.
            accounts: Optional company ids to add, as
                search_sales_navigator_accounts or get_sales_navigator_account
                return them (a urn:li:fs_salesCompany or urn:li:organization
                URN or a Sales Navigator company URL is accepted too), up to
                25 per call.

        Returns:
            Dict with url, status, message, dry_run, retry_safe, list ({id,
            name, description}; id only once Sales Navigator answered with
            it), accounts (input, id, status, detail when there is one) and,
            when accounts were sent for, accounts_status.
        """
        tool = "create_sales_navigator_account_list"
        try:
            invalid = refuse_invalid_list_text(name, description)
            planned = plan_accounts(accounts or [])
            if invalid is None:
                invalid = planned.invalid
            if invalid is not None:
                return list_write_result(
                    tool,
                    "invalid_request",
                    invalid,
                    dry_run=not confirm_send,
                    accounts=planned.rows,
                )
            return await _run(
                ctx,
                tool,
                confirm_send=confirm_send,
                requests=1 + bool(planned.ids),
                extractor=extractor,
                message="Creating list",
                call=lambda extractor: extractor.create_sales_navigator_account_list(
                    name,
                    description=description,
                    accounts=planned.rows,
                    confirm_send=confirm_send,
                ),
            )
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, tool)
        except Exception as e:
            raise_tool_error(e, tool)  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Add Accounts to Sales Navigator List",
        annotations=_ANNOTATIONS,
        tags=_TAGS,
        exclude_args=["extractor"],
    )
    async def add_accounts_to_sales_navigator_list(
        list_id: str,
        accounts: StrList,
        confirm_send: bool,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Add accounts to one Sales Navigator account list of the signed-in
        member, in one request. Needs a Sales Navigator seat. Like Sales
        Navigator's own Save to list, this also saves each account.

        The list is opened first (one page load) and must be an account list;
        the list id is used exactly as given. confirm_send False is a dry run
        that adds nothing and reports each account's status (would_add,
        duplicate). With confirm_send True each account's status is taken
        from Sales Navigator's answer for it: added, already_in_list, refused
        (detail names Sales Navigator's reason, such as EXCEED_LIMIT) or
        unconfirmed when the answer does not name it. When an outcome is unknown, retry_safe is false: read the list
        with get_sales_navigator_account_list before calling again. The one
        request counts against the daily sales_list_writes cap.

        Args:
            list_id: Numeric account list id, as
                list_sales_navigator_account_lists or
                create_sales_navigator_account_list returned it. A Sales
                Navigator list URL carrying listId is accepted too.
            accounts: Company ids to add, as search_sales_navigator_accounts
                or get_sales_navigator_account return them (URNs and Sales
                Navigator company URLs are accepted too), up to 25 per call.
            confirm_send: False for a dry run; True to add them.
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, status, message, dry_run, retry_safe, list ({id,
            name, entityCount} as Sales Navigator showed it) and accounts
            (input, id, status, detail when there is one).
        """
        tool = "add_accounts_to_sales_navigator_list"
        try:
            list_id = normalize_sales_list_id(list_id)
            planned = plan_accounts(accounts)
            invalid = planned.invalid
            if invalid is None and not planned.ids:
                invalid = "Pass at least one account."
            if invalid is not None:
                return list_write_result(
                    tool,
                    "invalid_request",
                    invalid,
                    dry_run=not confirm_send,
                    list_id=list_id,
                    accounts=planned.rows,
                )
            return await _run(
                ctx,
                tool,
                confirm_send=confirm_send,
                requests=1,
                extractor=extractor,
                message="Adding accounts",
                call=lambda extractor: extractor.add_accounts_to_sales_navigator_list(
                    list_id, planned.rows, confirm_send=confirm_send
                ),
            )
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, tool)
        except Exception as e:
            raise_tool_error(e, tool)  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Delete Sales Navigator Account List",
        annotations=_ANNOTATIONS,
        tags=_TAGS,
        exclude_args=["extractor"],
    )
    async def delete_sales_navigator_account_list(
        list_id: str,
        expected_name: str,
        confirm_send: bool,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Delete one Sales Navigator account list of the signed-in member. The
        accounts stay saved, as with Sales Navigator's own default.
        Irreversible. Needs a Sales
        Navigator seat.

        The list is opened first (one page load), and it is deleted only when
        it is an account list whose name is exactly expected_name; otherwise
        nothing is sent. Only that list id is ever deleted. confirm_send
        False is a dry run that deletes nothing and reports the list that
        would be deleted. Success is only reported from Sales Navigator's own
        answer; when an outcome is unknown, retry_safe is false: check
        list_sales_navigator_account_lists before calling again. The request
        counts against the daily sales_list_writes cap.

        Args:
            list_id: Numeric account list id, as
                list_sales_navigator_account_lists returned it. A Sales
                Navigator list URL carrying listId is accepted too.
            expected_name: The list's exact current name, as
                list_sales_navigator_account_lists or
                get_sales_navigator_account_list showed it.
            confirm_send: False for a dry run; True to delete the list.
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, status, message, dry_run, retry_safe and list ({id,
            name, entityCount} as Sales Navigator showed it).
        """
        tool = "delete_sales_navigator_account_list"
        try:
            list_id = normalize_sales_list_id(list_id)
            if not expected_name.strip():
                return list_write_result(
                    tool,
                    "invalid_request",
                    "expected_name must be the list's current name.",
                    dry_run=not confirm_send,
                    list_id=list_id,
                )
            return await _run(
                ctx,
                tool,
                confirm_send=confirm_send,
                requests=1,
                extractor=extractor,
                message="Deleting list",
                call=lambda extractor: extractor.delete_sales_navigator_account_list(
                    list_id, expected_name=expected_name, confirm_send=confirm_send
                ),
            )
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, tool)
        except Exception as e:
            raise_tool_error(e, tool)  # NoReturn
