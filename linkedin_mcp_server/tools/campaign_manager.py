"""
LinkedIn Campaign Manager read tools.

Campaign Manager's API names differ from its UI labels: an API campaign group
is the UI "Campaign", an API campaign is the UI "Ad set" and an API creative is
the UI "Ad". Tools are named by API entity so a UI rename cannot change them.
"""

import logging
from datetime import date
from typing import Any, Literal

from fastmcp import Context, FastMCP

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    InvalidReferenceError,
)
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping.identifiers import (
    normalize_ad_account_id,
    normalize_campaign_id,
)

logger = logging.getLogger(__name__)


def register_campaign_manager_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register all Campaign Manager tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="List Ad Accounts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"campaign_manager", "scraping"},
        exclude_args=["extractor"],
    )
    async def list_ad_accounts(
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Terminology: API campaign group = "Campaign" in Campaign Manager's UI,
        API campaign = "Ad set", API creative = "Ad".

        List the LinkedIn ad accounts the signed-in member can open in
        Campaign Manager, including the personal boost account that boosted
        posts run in. Read-only.

        Args:
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, sections (ad_accounts -> one line per ad account:
            name, id, status) and ad_accounts, the full records (kind, uiLabel,
            id, name, status, type, currencyCode, servingStatuses,
            currentUserRole, testAccount, organizationUrn; boost: true on the
            boost account). section_errors is present when the list is
            partial or the boost account could not be read.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="list_ad_accounts"
            )
            logger.info("Listing ad accounts")

            await ctx.report_progress(
                progress=0, total=100, message="Loading ad accounts"
            )

            result = await extractor.list_ad_accounts()

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "list_ad_accounts")
        except Exception as e:
            raise_tool_error(e, "list_ad_accounts")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="List Campaigns",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"campaign_manager", "scraping"},
        exclude_args=["extractor"],
    )
    async def list_campaigns(
        ad_account_id: str,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Terminology: API campaign group = "Campaign" in Campaign Manager's UI,
        API campaign = "Ad set", API creative = "Ad".

        List an ad account's campaign groups (UI "Campaigns") and campaigns
        (UI "Ad sets"). Read-only.

        Args:
            ad_account_id: Numeric ad account id, as list_ad_accounts returned
                it. An urn:li:sponsoredAccount URN or a Campaign Manager
                account URL is accepted too and is reduced to the id.
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, sections (one line per campaign group and campaign:
            name, id, status, budget), campaign_groups and campaigns (full
            records with kind, uiLabel, status, budgets as decimal strings,
            run schedule), and ad_accounts with the account's own record
            (currency included) when the page loaded it. An account with no
            active campaign is reported with section_errors.campaigns
            (error_type account_setup_redirect) when Campaign Manager sends it
            to its setup flow and the campaigns cannot be read.
        """
        try:
            ad_account_id = normalize_ad_account_id(ad_account_id)
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="list_campaigns"
            )
            logger.info("Listing campaigns for ad account %s", ad_account_id)

            await ctx.report_progress(
                progress=0, total=100, message="Loading campaigns"
            )

            result = await extractor.list_campaigns(ad_account_id)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "list_campaigns")
        except Exception as e:
            raise_tool_error(e, "list_campaigns")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Campaign",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"campaign_manager", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_campaign(
        ad_account_id: str,
        campaign_id: str,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Terminology: API campaign group = "Campaign" in Campaign Manager's UI,
        API campaign = "Ad set", API creative = "Ad".

        Get one API campaign, an ad set in Campaign Manager's UI: its settings,
        targeting, schedule, parent campaign group and creatives (UI "Ads").
        Read-only.

        Args:
            ad_account_id: Numeric ad account id. An urn:li:sponsoredAccount
                URN or a Campaign Manager account URL is accepted too.
            campaign_id: Numeric campaign id, as list_campaigns returned it. An
                urn:li:sponsoredCampaign URN or a Campaign Manager campaign
                URL is accepted too.
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, sections (one line each for the campaign, its
            campaign group and its creatives) and campaign, the full record
            (kind, uiLabel, status, budgets as decimal strings, runSchedule,
            targeting), plus campaign_groups (its one group) and creatives
            when read.
            Creatives are parsed defensively; section_errors.creatives is set
            when their payload has no recognisable shape.
        """
        try:
            ad_account_id = normalize_ad_account_id(ad_account_id)
            campaign_id = normalize_campaign_id(campaign_id)
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_campaign"
            )
            logger.info(
                "Fetching campaign %s in ad account %s", campaign_id, ad_account_id
            )

            await ctx.report_progress(progress=0, total=100, message="Loading campaign")

            result = await extractor.get_campaign(ad_account_id, campaign_id)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_campaign")
        except Exception as e:
            raise_tool_error(e, "get_campaign")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Campaign Analytics",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"campaign_manager", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_campaign_analytics(
        ad_account_id: str,
        ctx: Context,
        level: Literal["campaign_group", "campaign", "creative"] = "campaign",
        start_date: date | None = None,
        end_date: date | None = None,
        demographics: (
            list[
                Literal[
                    "MEMBER_JOB_FUNCTION",
                    "MEMBER_SENIORITY",
                    "MEMBER_INDUSTRY",
                    "MEMBER_COMPANY_SIZE",
                    "MEMBER_COUNTRY_V2",
                ]
            ]
            | None
        ) = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Terminology: API campaign group = "Campaign" in Campaign Manager's UI,
        API campaign = "Ad set", API creative = "Ad".

        Read performance analytics for an ad account, one row per entity at
        the chosen level. Read-only.

        Args:
            ad_account_id: Numeric ad account id. An urn:li:sponsoredAccount
                URN or a Campaign Manager account URL is accepted too.
            ctx: FastMCP context for progress reporting
            level: Entity level to report on: campaign_group (UI "Campaigns"),
                campaign (UI "Ad sets", the default) or creative (UI "Ads").
            start_date: First day of the period (YYYY-MM-DD). Give it together
                with end_date. Omit both to get whatever date range Campaign
                Manager shows by default.
            end_date: Last day of the period (YYYY-MM-DD), not before
                start_date.
            demographics: Optional audience breakdowns by member attribute.
                MEMBER_JOB_FUNCTION is verified against live responses.
                MEMBER_SENIORITY, MEMBER_INDUSTRY, MEMBER_COMPANY_SIZE and
                MEMBER_COUNTRY_V2 are unverified; a breakdown LinkedIn
                rejects is reported in section_errors and does not fail the
                call.

        Returns:
            Dict with url, sections (one line per entity: name, id,
            impressions, clicks, spend) and analytics, whose rows carry
            entity {kind, id, urn, name} and the metrics LinkedIn returned
            (impressions, clicks, costInLocalCurrency, and the cost-per
            figures only when LinkedIn supplies them), plus the date window
            used and whether it came from the page or from the request.
            An empty period is valid and returns no rows.
        """
        try:
            ad_account_id = normalize_ad_account_id(ad_account_id)
            if (start_date is None) != (end_date is None):
                raise InvalidReferenceError(
                    "Give start_date and end_date together, or neither."
                )
            if start_date and end_date and start_date > end_date:
                raise InvalidReferenceError("start_date must not be after end_date.")
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_campaign_analytics"
            )
            logger.info("Fetching %s analytics for ad account %s", level, ad_account_id)

            await ctx.report_progress(
                progress=0, total=100, message="Loading analytics"
            )

            result = await extractor.get_campaign_analytics(
                ad_account_id,
                level=level,
                start_date=start_date,
                end_date=end_date,
                demographics=demographics,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_campaign_analytics")
        except Exception as e:
            raise_tool_error(e, "get_campaign_analytics")  # NoReturn
