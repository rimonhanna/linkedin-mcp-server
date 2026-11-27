"""
LinkedIn Sales Navigator saved-search write tool.

Saves a lead or account search under a name through Sales Navigator's own
Save search control on the search results page. ``confirm_send=False`` is a
dry run that changes nothing.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import anyio
from fastmcp import Context, FastMCP

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping.contracts import (
    FilterValidationError,
    before_the_reply_deadline,
)
from linkedin_mcp_server.scraping.identifiers import normalize_sales_list_id
from linkedin_mcp_server.scraping.sales_navigator_lead_filters import (
    TenureBucket,
    function_ids,
)
from linkedin_mcp_server.scraping.sales_navigator_payload import SeniorityLevel
from linkedin_mcp_server.scraping.sales_navigator_saved_search import (
    tracking_saved_search,
)
from linkedin_mcp_server.scraping.sales_navigator_saved_search_payload import (
    SAVE_TOOL,
    refuse_invalid_search_name,
    saved_search_result,
    search_criteria,
)
from linkedin_mcp_server.scraping.search_urls import (
    company_size_letters,
    industry_ids,
    network_tokens,
)
from linkedin_mcp_server.tools.person import StrList
from linkedin_mcp_server.tools.sales_navigator_lists import refuse_before_the_browser

logger = logging.getLogger(__name__)


async def _run(
    ctx: Context,
    *,
    confirm_send: bool,
    extractor: Any | None,
    call: Callable[[Any], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Like the list writes' runner: an error after the save left is an outcome."""
    # The save and the rename: refused here, a save that would leave the
    # search unnamed on the cap is not started.
    refuse_before_the_browser(confirm_send, requests=2)
    extractor = extractor or await get_ready_extractor(ctx, tool_name=SAVE_TOOL)
    logger.info("%s (confirm_send=%s)", SAVE_TOOL, confirm_send)
    await ctx.report_progress(progress=0, total=100, message="Saving search")
    with tracking_saved_search() as ledger:
        try:
            result = await call(extractor)
        except Exception as exc:
            if not ledger.admitted:
                raise
            logger.warning(
                "%s failed after the save was sent", SAVE_TOOL, exc_info=True
            )
            result = ledger.unconfirmed(exc)
    try:
        if anyio.current_time() < anyio.current_effective_deadline():
            with before_the_reply_deadline():
                await ctx.report_progress(progress=100, total=100, message="Complete")
    except BaseException:
        if result.get("retry_safe") is False:
            logger.warning(
                "%s was interrupted after the save was sent; check Saved "
                "searches before calling it again",
                SAVE_TOOL,
            )
        raise
    return result


def register_sales_navigator_saved_search_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register the Sales Navigator saved-search write tool."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Save Sales Navigator Search",
        annotations={"destructiveHint": False, "openWorldHint": True},
        tags={"sales_navigator", "actions"},
        exclude_args=["extractor"],
    )
    async def save_sales_navigator_search(
        kind: Literal["leads", "accounts"],
        name: str,
        confirm_send: bool,
        ctx: Context,
        keywords: str | None = None,
        industry: StrList | None = None,
        exclude_industry: StrList | None = None,
        title: StrList | None = None,
        seniority: list[SeniorityLevel] | None = None,
        current_company: StrList | None = None,
        exclude_current_company: StrList | None = None,
        location: StrList | None = None,
        company_hq_location: StrList | None = None,
        network: StrList | None = None,
        function: StrList | None = None,
        company_size: StrList | None = None,
        years_in_current_role: list[TenureBucket] | None = None,
        years_at_current_company: list[TenureBucket] | None = None,
        account_list: StrList | None = None,
        lead_list: StrList | None = None,
        exclude_lead_list: StrList | None = None,
        hq_location: StrList | None = None,
        size: StrList | None = None,
        headcount_growth_min: int | None = None,
        headcount_growth_max: int | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Save a Sales Navigator lead or account search under a name, as Sales
        Navigator's own Save search button does; it then shows under Saved
        searches and Sales Navigator alerts on new results. Needs a Sales
        Navigator seat. Never contacts anyone.

        The filters are the ones search_sales_navigator_leads (kind "leads")
        or search_sales_navigator_accounts (kind "accounts") takes, resolved
        and sent the same way, so the saved search is the search those tools
        run. A filter of the other kind is refused before the browser starts.

        confirm_send False is a dry run: nothing is saved, and the result
        reports the planned search URL and name (resolving a location or a
        company name may still load pages). confirm_send True loads the
        search page, checks that the page searched exactly the filters asked,
        presses Save search, types the name and saves it through Sales
        Navigator's own form; only that one request may leave the browser.
        Status saved is reported only when Sales Navigator answered it and
        the saved search then reads back under that name with that query.
        When an outcome is unknown, retry_safe is false: check Saved searches
        in Sales Navigator before calling again, or the search can be saved
        twice. The save counts against the daily sales_list_writes cap and is
        refused outside working hours.

        Args:
            kind: "leads" or "accounts": which search to save.
            name: The saved search's name, one line.
            confirm_send: False for a dry run; True to save the search.
            ctx: FastMCP context for progress reporting
            keywords: Free-text keywords (both kinds).
            industry: Industries, numeric ids or names (both kinds).
            exclude_industry: Industries left out (both kinds).
            title: Lead current job titles (leads).
            seniority: Lead seniority levels (leads).
            current_company: Lead current companies (leads).
            exclude_current_company: Companies left out (leads).
            location: Lead locations (leads).
            company_hq_location: Headquarters locations of the lead's current
                company (leads).
            network: Connection degrees F, S, O (leads).
            function: Job functions (leads).
            company_size: Current-company headcount buckets (leads).
            years_in_current_role: Tenure buckets (leads).
            years_at_current_company: Tenure buckets (leads).
            account_list: Account list ids (leads).
            lead_list: Lead list ids (leads).
            exclude_lead_list: Lead list ids left out (leads).
            hq_location: Account headquarters locations (accounts).
            size: Account headcount buckets (accounts).
            headcount_growth_min: Minimum headcount growth, percent (accounts).
            headcount_growth_max: Maximum headcount growth, percent (accounts).

        Returns:
            Dict with url (the search page), tool, status, message, dry_run,
            retry_safe and search ({kind, name, url, id once Sales Navigator
            answered with it}).
        """
        tool = SAVE_TOOL
        try:
            given = {
                "keywords": keywords,
                "industry": industry_ids(industry),
                "exclude_industry": industry_ids(exclude_industry),
                "title": title,
                "seniority": seniority,
                "current_company": current_company,
                "exclude_current_company": exclude_current_company,
                "location": location,
                "company_hq_location": company_hq_location,
                "network": network_tokens(network),
                "function": function_ids(function or []),
                "company_size": company_size_letters(company_size),
                "years_in_current_role": years_in_current_role,
                "years_at_current_company": years_at_current_company,
                "account_list": [
                    normalize_sales_list_id(v) for v in account_list or []
                ],
                "lead_list": [normalize_sales_list_id(v) for v in lead_list or []],
                "exclude_lead_list": [
                    normalize_sales_list_id(v) for v in exclude_lead_list or []
                ],
                "hq_location": hq_location,
                "size": company_size_letters(size),
                "headcount_growth_min": headcount_growth_min,
                "headcount_growth_max": headcount_growth_max,
            }
            invalid = refuse_invalid_search_name(name)
            criteria: dict[str, Any] = {}
            if invalid is None:
                criteria, invalid = search_criteria(kind, given)
            if invalid is not None:
                return saved_search_result(
                    "invalid_request",
                    invalid,
                    dry_run=not confirm_send,
                    kind=kind,
                    name=name,
                )
            return await _run(
                ctx,
                confirm_send=confirm_send,
                extractor=extractor,
                call=lambda extractor: extractor.save_sales_navigator_search(
                    kind, name.strip(), criteria=criteria, confirm_send=confirm_send
                ),
            )
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, tool)
        except FilterValidationError as e:
            return saved_search_result(
                "invalid_request",
                str(e),
                dry_run=not confirm_send,
                kind=kind,
                name=name,
            )
        except Exception as e:
            raise_tool_error(e, tool)  # NoReturn
