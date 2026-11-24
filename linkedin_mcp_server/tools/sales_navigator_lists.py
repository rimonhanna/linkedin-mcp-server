"""
LinkedIn Sales Navigator account-list write tools.

Each tool loads one Sales Navigator page in the signed-in member's browser to
verify its target, then sends the one planned request and nothing else (see
``sales_navigator_list_guard``). ``confirm_send=False`` is a dry run that
changes nothing.
"""

import logging
from datetime import datetime
from typing import Any

from fastmcp import Context, FastMCP

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.pacing import SALES_LIST_WRITES, JobStore, refuse_action
from linkedin_mcp_server.scraping.identifiers import normalize_sales_list_id
from linkedin_mcp_server.scraping.sales_navigator_list_payload import (
    refuse_invalid_list_text,
)
from linkedin_mcp_server.scraping.sales_navigator_list_writes import (
    list_write_result,
    plan_accounts,
)
from linkedin_mcp_server.tools.person import StrList

logger = logging.getLogger(__name__)

_ANNOTATIONS = {"destructiveHint": True, "openWorldHint": True}
_TAGS = {"sales_navigator", "actions"}


def _refuse_before_the_browser(confirm_send: bool) -> None:
    # A dry run sends nothing and owes nothing.
    if confirm_send:
        refuse_action(JobStore(), SALES_LIST_WRITES, datetime.now().astimezone())


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
        (would_add, duplicate). confirm_send True creates the list, then adds
        the accounts in one request, as add_accounts_to_sales_navigator_list
        does. Success is only reported from Sales Navigator's own answer. When an outcome is unknown, retry_safe is
        false: check list_sales_navigator_account_lists before calling again,
        or the list can be created twice. Each request sent counts against
        the daily sales_list_writes cap.

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
            it) and accounts (input, id, status, detail when there is one).
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
            _refuse_before_the_browser(confirm_send)
            extractor = extractor or await get_ready_extractor(ctx, tool_name=tool)
            logger.info("%s (confirm_send=%s)", tool, confirm_send)
            await ctx.report_progress(progress=0, total=100, message="Creating list")
            result = await extractor.create_sales_navigator_account_list(
                name,
                description=description,
                accounts=planned.ids,
                confirm_send=confirm_send,
            )
            await ctx.report_progress(progress=100, total=100, message="Complete")
            return result
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
            _refuse_before_the_browser(confirm_send)
            extractor = extractor or await get_ready_extractor(ctx, tool_name=tool)
            logger.info("%s %s (confirm_send=%s)", tool, list_id, confirm_send)
            await ctx.report_progress(progress=0, total=100, message="Adding accounts")
            result = await extractor.add_accounts_to_sales_navigator_list(
                list_id, planned.ids, confirm_send=confirm_send
            )
            await ctx.report_progress(progress=100, total=100, message="Complete")
            return result
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
            _refuse_before_the_browser(confirm_send)
            extractor = extractor or await get_ready_extractor(ctx, tool_name=tool)
            logger.info("%s %s (confirm_send=%s)", tool, list_id, confirm_send)
            await ctx.report_progress(progress=0, total=100, message="Deleting list")
            result = await extractor.delete_sales_navigator_account_list(
                list_id, expected_name=expected_name, confirm_send=confirm_send
            )
            await ctx.report_progress(progress=100, total=100, message="Complete")
            return result
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, tool)
        except Exception as e:
            raise_tool_error(e, tool)  # NoReturn
