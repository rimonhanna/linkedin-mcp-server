"""
LinkedIn Sales Navigator read tools.

Every tool opens Sales Navigator pages in the signed-in member's browser and
reads the responses the page itself receives. They need a Sales Navigator seat
and never open a lead page, so no profile view is registered.
"""

import logging
import time
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.pacing import request_arrived_at
from linkedin_mcp_server.scraping.contracts import FilterValidationError
from linkedin_mcp_server.scraping.identifiers import (
    normalize_sales_company_id,
    normalize_sales_list_id,
)
from linkedin_mcp_server.scraping.sales_navigator_lead_filters import (
    TenureBucket,
    function_ids,
)
from linkedin_mcp_server.scraping.sales_navigator_payload import SeniorityLevel
from linkedin_mcp_server.scraping.search_urls import (
    company_size_letters,
    industry_ids,
    network_tokens,
)
from linkedin_mcp_server.tools.enrichment import DEADLINE_FRACTION
from linkedin_mcp_server.tools.person import StrList

logger = logging.getLogger(__name__)


def _deadline(tool_timeout: float) -> float:
    """When a multi-page read stops starting pages, inside the tool timeout.

    From arrival at the middleware, as the bulk tools do: a call queued behind
    another spends its wait before its own timeout starts.
    """
    arrived = request_arrived_at.get()
    return (
        time.monotonic() if arrived is None else arrived
    ) + tool_timeout * DEADLINE_FRACTION


def register_sales_navigator_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register all Sales Navigator tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Sales Navigator Accounts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"sales_navigator", "scraping"},
        exclude_args=["extractor"],
    )
    async def search_sales_navigator_accounts(
        ctx: Context,
        keywords: str | None = None,
        industry: StrList | None = None,
        exclude_industry: StrList | None = None,
        hq_location: StrList | None = None,
        size: StrList | None = None,
        headcount_growth_min: int | None = None,
        headcount_growth_max: int | None = None,
        page: Annotated[int, Field(ge=1, le=40)] = 1,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Search Sales Navigator accounts (companies). Read-only, one page per
        call.

        Needs a Sales Navigator seat. Each result page spends one unit of the
        daily search cap (the same cap as classic search), and a location or
        company name costs extra navigations to resolve; pass numeric ids to
        avoid them. Sales Navigator returns at most 1,000 results per search
        (40 pages of 25): split a larger search into narrower ones, each under
        1,000, then page each. The request the page sent is checked against
        the filters asked: a filter the page drops or rewrites is an error,
        never silently unfiltered results. That check cannot show the server
        applied a filter or that an id is right. Display-text fields (industry, size
        range, seniority name) are in the member's UI language; ids carry
        identity.

        When no account matches every filter, Sales Navigator drops a filter
        and shows near matches instead. Those are not reported: the result
        has no accounts, paging.total 0 and section_errors.accounts
        (filters_relaxed) naming the dropped filter.

        At least one of keywords or a filter is required.

        Args:
            ctx: FastMCP context for progress reporting
            keywords: Free-text keywords.
            industry: One or a list of industries, numeric ids or names.
            exclude_industry: Industries whose accounts are left out, numeric
                ids or names.
            hq_location: One or a list of headquarters locations, numeric geo
                ids, urn:li:geo URNs or names.
            size: One or a list of company headcount buckets or facet letters
                (A to I).
            headcount_growth_min: Minimum headcount growth, percent.
            headcount_growth_max: Maximum headcount growth, percent.
            page: Result page, 1 to 40 (25 results each).

        Returns:
            Dict with url, sections (accounts -> one line per account),
            accounts (id, name, industry, employeeCountRange,
            employeeDisplayCount, description, saved, listCount,
            salesNavigatorUrl), paging (page, start, count = accounts
            returned, total, resultCap, capped, more) and, when a filter was
            dropped, section_errors.accounts.
        """
        try:
            industry_list = industry_ids(industry)
            exclude_industry_list = industry_ids(exclude_industry)
            size_list = company_size_letters(size)
            _require_criterion(
                keywords,
                industry_list,
                exclude_industry_list,
                hq_location,
                size_list,
                headcount_growth_min,
                headcount_growth_max,
            )
            if (
                headcount_growth_min is not None
                and headcount_growth_max is not None
                and headcount_growth_min > headcount_growth_max
            ):
                raise ToolError(
                    "headcount_growth_min must not be greater than "
                    "headcount_growth_max."
                )
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="search_sales_navigator_accounts"
            )
            logger.info("Searching Sales Navigator accounts, page %d", page)

            await ctx.report_progress(
                progress=0, total=100, message="Searching Sales Navigator accounts"
            )

            result = await extractor.search_sales_navigator_accounts(
                keywords=keywords,
                industry=industry_list or None,
                exclude_industry=exclude_industry_list or None,
                hq_location=hq_location or None,
                size=size_list or None,
                headcount_growth_min=headcount_growth_min,
                headcount_growth_max=headcount_growth_max,
                page=page,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "search_sales_navigator_accounts")
        except FilterValidationError as e:
            raise ToolError(str(e)) from e
        except Exception as e:
            raise_tool_error(e, "search_sales_navigator_accounts")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Sales Navigator Leads",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"sales_navigator", "scraping"},
        exclude_args=["extractor"],
    )
    async def search_sales_navigator_leads(
        ctx: Context,
        keywords: str | None = None,
        title: StrList | None = None,
        seniority: list[SeniorityLevel] | None = None,
        current_company: StrList | None = None,
        exclude_current_company: StrList | None = None,
        location: StrList | None = None,
        industry: StrList | None = None,
        exclude_industry: StrList | None = None,
        network: StrList | None = None,
        function: StrList | None = None,
        company_size: StrList | None = None,
        years_in_current_role: list[TenureBucket] | None = None,
        years_at_current_company: list[TenureBucket] | None = None,
        account_list: StrList | None = None,
        lead_list: StrList | None = None,
        exclude_lead_list: StrList | None = None,
        page: Annotated[int, Field(ge=1, le=40)] = 1,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Search Sales Navigator leads (people). Read-only, one page per call.
        No lead page is opened, so no profile view is registered.

        Needs a Sales Navigator seat. Each result page spends one unit of the
        daily search cap (the same cap as classic search), and a location or
        company name costs extra navigations to resolve; pass numeric ids to
        avoid them. Sales Navigator returns at most 1,000 results per search
        (40 pages of 25): split a larger search into narrower ones, each under
        1,000, then page each. The request the page sent is checked against
        the filters asked: a filter the page drops or rewrites is an error,
        never silently unfiltered results. That check cannot show the server
        applied a filter or that an id is right. Display-text fields (industry, size
        range, seniority name) are in the member's UI language; ids carry
        identity.

        Every account_list, lead_list and exclude_lead_list id is first looked
        up among the account's own lists (one list page load per 25 lists,
        until all are found), because Sales Navigator's search ignores an id
        it does not know; an id that is not one of them is refused before the
        search.

        When no lead matches every filter, Sales Navigator drops a filter and
        shows near matches instead. Those are not reported: the result has no
        leads, paging.total 0 and section_errors.leads (filters_relaxed)
        naming the dropped filter.

        Leads whose LinkedIn profile blocks third-party data sharing are left
        out and only counted in withheld. At least one of keywords or a filter
        is required.

        Args:
            ctx: FastMCP context for progress reporting
            keywords: Free-text keywords.
            title: One or a list of current job titles (free text).
            seniority: One or a list of seniority levels: in_training,
                entry_level, senior, strategic, entry_level_manager,
                experienced_manager, director, vice_president, cxo,
                owner_partner.
            current_company: One or a list of current companies, numeric
                company ids, urn:li:organization URNs or names.
            exclude_current_company: Companies whose current employees are
                left out, in the same forms as current_company.
            location: One or a list of locations, numeric geo ids,
                urn:li:geo URNs or names.
            industry: One or a list of industries, numeric ids or names.
            exclude_industry: Industries whose leads are left out, numeric ids
                or names.
            network: Connection degree, one or a list of "F" (1st-degree),
                "S" (2nd-degree) and "O" (3rd-degree and beyond), the same
                tokens as search_people. ["F"] finds the connections a company
                page can invite to follow it.
            function: One or a list of job functions, by name (engineering,
                sales, marketing, information_technology, ...) or id (1 to 26).
            company_size: One or a list of current-company headcount buckets
                (1-10, 11-50, ..., 10001+) or facet letters (A to I).
            years_in_current_role: One or a list of less_than_1, 1_to_2,
                3_to_5, 6_to_10, more_than_10.
            years_at_current_company: The same buckets, for the time at the
                current company.
            account_list: One or a list of account list ids, as
                list_sales_navigator_account_lists returned them: leads at
                the companies in those lists.
            lead_list: One or a list of lead list ids, as
                list_sales_navigator_lead_lists returned them: only leads in
                those lists.
            exclude_lead_list: Lead list ids whose leads are left out.
            page: Result page, 1 to 40 (25 results each).

        Returns:
            Dict with url, sections (leads -> one line per lead), leads (id,
            profileUrl, memberUrn, fullName, firstName, lastName, geoRegion,
            degree, premium, seniority, currentPositions, summary; no Open
            Profile flag, which the search does not carry),
            paging (page, start, count = leads returned, total,
            resultCap, capped, more), withheld
            ({count, reason}, present only when a lead was left out) and, when
            a filter was dropped, section_errors.leads.
            send_sales_navigator_message reads Open Profile from the message
            box itself.
        """
        try:
            industry_list = industry_ids(industry)
            exclude_industry_list = industry_ids(exclude_industry)
            network = network_tokens(network)
            function = function_ids(function or [])
            company_size = company_size_letters(company_size)
            account_list = [normalize_sales_list_id(v) for v in account_list or []]
            lead_list = [normalize_sales_list_id(v) for v in lead_list or []]
            exclude_lead_list = [
                normalize_sales_list_id(v) for v in exclude_lead_list or []
            ]
            _require_criterion(
                keywords,
                title,
                seniority,
                current_company,
                exclude_current_company,
                location,
                industry_list,
                exclude_industry_list,
                network,
                function,
                company_size,
                years_in_current_role,
                years_at_current_company,
                account_list,
                lead_list,
                exclude_lead_list,
            )
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="search_sales_navigator_leads"
            )
            logger.info("Searching Sales Navigator leads, page %d", page)

            await ctx.report_progress(
                progress=0, total=100, message="Searching Sales Navigator leads"
            )

            result = await extractor.search_sales_navigator_leads(
                keywords=keywords,
                title=title or None,
                seniority=seniority or None,
                current_company=current_company or None,
                exclude_current_company=exclude_current_company or None,
                location=location or None,
                industry=industry_list or None,
                exclude_industry=exclude_industry_list or None,
                network=network or None,
                function=function or None,
                company_size=company_size or None,
                years_in_current_role=years_in_current_role or None,
                years_at_current_company=years_at_current_company or None,
                account_list=account_list or None,
                lead_list=lead_list or None,
                exclude_lead_list=exclude_lead_list or None,
                page=page,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "search_sales_navigator_leads")
        except FilterValidationError as e:
            raise ToolError(str(e)) from e
        except Exception as e:
            raise_tool_error(e, "search_sales_navigator_leads")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Sales Navigator Account",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"sales_navigator", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_sales_navigator_account(
        company_id: str,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Get one company's Sales Navigator account page: description, industry,
        type, website, headquarters, revenue range, headcount and headcount
        growth. Read-only. Needs a Sales Navigator seat. Employees are never
        returned. Display-text fields are in the member's UI language.

        Args:
            company_id: Numeric company id, as the account search returned it.
                A urn:li:organization or urn:li:fs_salesCompany URN or a
                Sales Navigator company URL is accepted too.
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, sections (account -> one line) and account, the
            full record (id, name, description, industry, type, specialties,
            website, yearFounded, headquarters, revenueRange, employeeCount,
            employeeCountRange, employeeDisplayCount, employeeGrowth,
            flagshipCompanyUrl, salesNavigatorUrl, saved, listCount).
            section_errors.headcount is set when the headcount data did not
            load.
        """
        try:
            company_id = normalize_sales_company_id(company_id)
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_sales_navigator_account"
            )
            logger.info("Fetching Sales Navigator account %s", company_id)

            await ctx.report_progress(progress=0, total=100, message="Loading account")

            result = await extractor.get_sales_navigator_account(company_id)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_sales_navigator_account")
        except Exception as e:
            raise_tool_error(e, "get_sales_navigator_account")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="List Sales Navigator Account Lists",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"sales_navigator", "scraping"},
        exclude_args=["extractor"],
    )
    async def list_sales_navigator_account_lists(
        ctx: Context,
        page: Annotated[int, Field(ge=1)] = 1,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        List the Sales Navigator account lists of the signed-in member.
        Read-only. Needs a Sales Navigator seat. This reads up to 1,000
        entries per call; pass next_page to continue. It loads one page of 25
        lists at a time until all are read, the 1,000 are reached or the
        call's time limit is near, and every page counts against the pacing
        budget. section_errors.account_lists is set when the result is
        partial: a page did not load, or there are more lists than one call
        reads or has time for (then next_page is set).

        Args:
            ctx: FastMCP context for progress reporting
            page: 1-based page of 25 lists to start from (default 1); pass a
                previous result's next_page to continue.

        Returns:
            Dict with url, sections (account_lists -> one line per list),
            account_lists (id, name, description, listType, listSource,
            entityCount, role, lastModifiedAt), paging (page, start, count =
            lists returned, total as LinkedIn reports it, more, next_page; no
            resultCap/capped, unlike the searches) and, when more lists remain,
            next_page. more true with next_page null means the read stopped
            early; see section_errors.
        """
        try:
            deadline = _deadline(tool_timeout)
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="list_sales_navigator_account_lists"
            )
            logger.info("Listing Sales Navigator account lists")

            await ctx.report_progress(
                progress=0, total=100, message="Loading account lists"
            )

            result = await extractor.list_sales_navigator_account_lists(
                page, deadline=deadline
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "list_sales_navigator_account_lists")
        except Exception as e:
            raise_tool_error(e, "list_sales_navigator_account_lists")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Sales Navigator Account List",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"sales_navigator", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_sales_navigator_account_list(
        list_id: str,
        ctx: Context,
        page: Annotated[int, Field(ge=1)] = 1,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Get one Sales Navigator account list and its accounts. Read-only.
        Needs a Sales Navigator seat. This reads up to 1,000 entries per call;
        pass next_page to continue. It loads one page of 25 accounts at a time
        until the list is covered, the 1,000 are reached or the call's time
        limit is near, and every page counts against the pacing budget, so a
        long list takes several navigations and often several calls.
        section_errors.accounts is set when the result is partial: a page did
        not load, the list changed while it was read, or the list is longer
        than one call reads or has time for (then next_page is set).
        Display-text fields are in the member's UI language. The accounts can
        be passed, by name, as the companies of create_company_list_audience.

        Args:
            list_id: Numeric list id, as list_sales_navigator_account_lists
                returned it. A Sales Navigator list URL carrying listId is
                accepted too.
            ctx: FastMCP context for progress reporting
            page: 1-based page of 25 accounts to start from (default 1); pass
                a previous result's next_page to continue.

        Returns:
            Dict with url, sections (account_list and accounts, one line each),
            account_list (id, name, description, listType, listSource,
            entityCount, role, lastModifiedAt), accounts, each row read (id,
            name, industry, employeeCount, employeeDisplayCount,
            buyerIntentLevel, saved), paging (page, start, count = accounts
            returned, total as LinkedIn reports it, more, next_page; no
            resultCap/capped, unlike the searches) and, when more accounts
            remain, next_page. more true with next_page null means the read
            stopped early; see section_errors.
        """
        try:
            deadline = _deadline(tool_timeout)
            list_id = normalize_sales_list_id(list_id)
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_sales_navigator_account_list"
            )
            logger.info("Fetching Sales Navigator account list %s", list_id)

            await ctx.report_progress(
                progress=0, total=100, message="Loading account list"
            )

            result = await extractor.get_sales_navigator_account_list(
                list_id, page, deadline=deadline
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_sales_navigator_account_list")
        except Exception as e:
            raise_tool_error(e, "get_sales_navigator_account_list")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="List Sales Navigator Lead Lists",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"sales_navigator", "scraping"},
        exclude_args=["extractor"],
    )
    async def list_sales_navigator_lead_lists(
        ctx: Context,
        page: Annotated[int, Field(ge=1)] = 1,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        List the Sales Navigator lead lists of the signed-in member. Read-only.
        Needs a Sales Navigator seat. Reads like
        list_sales_navigator_account_lists: one page of 25 lists per page
        load, up to 1,000 per call or until the call's time limit is near;
        pass next_page to continue. section_errors.lead_lists is set when the
        result is partial.

        Args:
            ctx: FastMCP context for progress reporting
            page: 1-based page of 25 lists to start from (default 1); pass a
                previous result's next_page to continue.

        Returns:
            Dict with url, sections (lead_lists -> one line per list),
            lead_lists (id, name, description, listType, listSource,
            entityCount, role, lastModifiedAt), paging (page, start, count =
            lists returned, total as LinkedIn reports it, more, next_page) and,
            when more lists remain, next_page. The ids are what
            get_sales_navigator_lead_list and the lead_list filter of
            search_sales_navigator_leads take.
        """
        try:
            deadline = _deadline(tool_timeout)
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="list_sales_navigator_lead_lists"
            )
            logger.info("Listing Sales Navigator lead lists")

            await ctx.report_progress(
                progress=0, total=100, message="Loading lead lists"
            )

            result = await extractor.list_sales_navigator_lead_lists(
                page, deadline=deadline
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "list_sales_navigator_lead_lists")
        except Exception as e:
            raise_tool_error(e, "list_sales_navigator_lead_lists")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Sales Navigator Lead List",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"sales_navigator", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_sales_navigator_lead_list(
        list_id: str,
        ctx: Context,
        page: Annotated[int, Field(ge=1, le=40)] = 1,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Get the leads of one Sales Navigator lead list. Read-only. Needs a
        Sales Navigator seat. No lead page is opened, so no profile view is
        registered.

        The list is read as a Sales Navigator lead search filtered to it, so
        each page of 25 is one search page against the daily search cap, at
        most 1,000 leads of a list can be read (40 pages) and the rows are the
        same records search_sales_navigator_leads returns, with the same
        identifiers. One call reads pages from page until the list is
        covered, 20 pages (500 leads, a fifth of the daily search cap) are
        read, a pacing cap refuses the next page or the call's time limit is
        near; each of the last three sets next_page, and a cap refusal keeps
        the leads already read and names the cap and resume_at in
        section_errors.leads. Leads who blocked third-party data sharing are
        left out and only counted in withheld. section_errors.leads is set
        whenever the result is partial.

        The id is first looked up among the account's lead lists (one list
        page load per 25 lists, until found), because Sales Navigator's search
        ignores an id it does not know and would return unrelated leads; an id
        that is not one of them is refused before any search page loads.
        lead_list is that list's record. When its entityCount is more than
        the search's total, section_errors.lead_list says how many of the
        list's leads Sales Navigator's search does not serve; when it is less,
        section_errors.lead_list says the search may not have been limited to
        the list. If Sales Navigator answered a page by dropping a filter, the
        result has no leads, paging.total 0 and section_errors.leads
        (filters_relaxed).

        Args:
            list_id: Numeric lead list id, as list_sales_navigator_lead_lists
                returned it. A Sales Navigator lead list URL
                (/sales/lists/people/<id>) is accepted too.
            ctx: FastMCP context for progress reporting
            page: 1-based page of 25 leads to start from, 1 to 40 (default 1);
                pass a previous result's next_page to continue.

        Returns:
            Dict with url, sections (leads -> one line per lead), lead_list
            (id, name, entityCount, listType, ...), leads (id,
            profileUrl, memberUrn, fullName, firstName, lastName, geoRegion,
            degree, premium, seniority, currentPositions, summary; no Open
            Profile flag, which the search does not carry),
            paging (page,
            start, count = leads returned, total as LinkedIn reports it,
            resultCap, capped, more, next_page), next_page when more pages
            remain, and withheld ({count, reason}) when a lead was left out.
        """
        try:
            deadline = _deadline(tool_timeout)
            list_id = normalize_sales_list_id(list_id)
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_sales_navigator_lead_list"
            )
            logger.info("Fetching Sales Navigator lead list %s", list_id)

            await ctx.report_progress(
                progress=0, total=100, message="Loading lead list"
            )

            result = await extractor.get_sales_navigator_lead_list(
                list_id, page, deadline=deadline
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_sales_navigator_lead_list")
        except FilterValidationError as e:
            raise ToolError(str(e)) from e
        except Exception as e:
            raise_tool_error(e, "get_sales_navigator_lead_list")  # NoReturn


def _require_criterion(*criteria: object) -> None:
    if not any(value is not None and value != [] and value != "" for value in criteria):
        raise ToolError("Pass keywords or at least one filter.")
