"""
LinkedIn Campaign Manager write tools.

Each tool drives Campaign Manager's own UI and lets through only the app's
request that matches the planned change. ``confirm_send=False`` is a dry run.
API names as in the read tools: an API campaign group is the UI "Campaign", an
API campaign the UI "Ad set" and an API creative the UI "Ad".
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import Any, Literal

from fastmcp import Context, FastMCP

from linkedin_mcp_server.config.schema import (
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    PER_TOOL_TIMEOUT_SECONDS,
)
from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    InvalidReferenceError,
    LinkedInScraperException,
)
from linkedin_mcp_server.debug_trace import retain_trace_for_failed_action
from linkedin_mcp_server.error_diagnostics import build_issue_diagnostics
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.pacing import (
    CAMPAIGN_WRITES,
    JobStore,
    record_action,
    refuse_action,
    request_arrived_at,
)
from linkedin_mcp_server.scraping.campaign_manager_changes import (
    refuse_invalid_audience_name,
    refuse_invalid_campaign_edit,
    refuse_invalid_draft,
    refuse_invalid_targeting_change,
)
from linkedin_mcp_server.scraping.campaign_manager_entities_payload import (
    ACCOUNT_TEMPLATE_HEADERS,
    CompanyRow,
    company_list_csv,
)
from linkedin_mcp_server.scraping.campaign_manager_results import (
    SUCCEEDED,
    campaign_action_result,
)
from linkedin_mcp_server.scraping.campaign_manager_targeting import TargetingAdd
from linkedin_mcp_server.scraping.campaign_manager_write import tracking_writes
from linkedin_mcp_server.scraping.identifiers import (
    campaign_manager_url,
    normalize_ad_account_id,
    normalize_campaign_group_id,
    normalize_campaign_id,
    normalize_post_urn,
    normalize_targeting_urn,
)
from linkedin_mcp_server.tools.enrichment import DEADLINE_FRACTION

logger = logging.getLogger(__name__)

_ANNOTATIONS = {"destructiveHint": True, "openWorldHint": True}
_TAGS = {"campaign_manager", "actions"}


def _invalid(tool: str, url: str, message: str) -> dict[str, Any]:
    return campaign_action_result(tool, url, "invalid_change", message, retry_safe=True)


async def _apply(
    ctx: Context,
    tool: str,
    *,
    confirm_send: bool,
    extractor: Any | None,
    message: str,
    call: Callable[[Any], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    # Refused before the browser, and only for a real write: a dry run sends
    # nothing and owes nothing.
    if confirm_send:
        refuse_action(JobStore(), CAMPAIGN_WRITES, datetime.now().astimezone())
    extractor = extractor or await get_ready_extractor(ctx, tool_name=tool)
    logger.info("%s (confirm_send=%s)", tool, confirm_send)

    await ctx.report_progress(progress=0, total=100, message=message)

    # The one place an error after an admitted write is turned into an
    # outcome, whichever flow raised it. Exactly one of the three exits
    # records: a returned result with retry_safe False (an escaped error
    # included), or a cancellation (the tool timeout) after an admitted
    # write, which is recorded and then re-raised. record_action follows the
    # await with no await between, so a cancellation cannot fall between them.
    with tracking_writes() as ledger:
        try:
            result = await call(extractor)
        except asyncio.CancelledError:
            if confirm_send and ledger.admitted:
                record_action(JobStore(), CAMPAIGN_WRITES, datetime.now().astimezone())
            raise
        except Exception as exc:
            if not ledger.admitted:
                raise
            logger.warning("%s failed after a write was sent", tool, exc_info=True)
            result = ledger.unverified(tool, exc)
    # retry_safe is False whenever one of the app's writes may have landed,
    # so such a write is counted.
    if confirm_send and result.get("retry_safe") is False:
        record_action(JobStore(), CAMPAIGN_WRITES, datetime.now().astimezone())
    retain_trace_for_failed_action(
        tool, result.get("status"), succeeded=SUCCEEDED[tool]
    )
    # A returned control_unavailable gets the same notes an error does, with
    # the API responses and page structure the capture recorded.
    if result.get("status") == "control_unavailable":
        notes = build_issue_diagnostics(
            LinkedInScraperException(str(result.get("message"))), context=tool
        )
        logger.warning(
            "%s: control unavailable; notes at %s",
            tool,
            notes.get("issue_template_path"),
        )

    await ctx.report_progress(progress=100, total=100, message="Complete")

    return result


def register_campaign_manager_action_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register the Campaign Manager write tools with the MCP server."""
    targeting_timeout = max(
        tool_timeout, PER_TOOL_TIMEOUT_SECONDS["update_campaign_targeting"]
    )
    upload_timeout = max(
        tool_timeout, PER_TOOL_TIMEOUT_SECONDS["create_company_list_audience"]
    )

    @mcp.tool(
        timeout=tool_timeout,
        title="Update Campaign",
        annotations=_ANNOTATIONS,
        tags=_TAGS,
        exclude_args=["extractor"],
    )
    async def update_campaign(
        ad_account_id: str,
        campaign_id: str,
        confirm_send: bool,
        ctx: Context,
        daily_budget: str | None = None,
        lifetime_budget: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        political_ad: bool | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Terminology: API campaign group = "Campaign" in Campaign Manager's UI,
        API campaign = "Ad set", API creative = "Ad".

        Change an API campaign's (UI "Ad set") budget amount or schedule
        dates. The budget type (daily or lifetime) and the schedule type are
        never switched. Never launches the ad set and never touches billing.

        confirm_send False is a dry run that verifies the target and returns
        the full planned change, with planned.politicalAdQuestion "will" when
        the save will ask whether the ads are political, else "may".

        On EU targeting the save first asks whether the ad set's ads are
        political. That answer is a legal declaration the user makes for the
        advertiser: pass political_ad only as the user declared it; the tool
        never defaults it. Saving targeting with no EU location makes Campaign
        Manager clear an existing declaration: that save stops with
        declaration_would_clear, or declaration_cleared_by_linkedin when
        political_ad was given, and sends nothing. In the newer editor the
        save may fill in "not political" itself: it passes only with
        political_ad false (declaration_required without it,
        declaration_not_offered with true).

        Args:
            ad_account_id: Numeric ad account id, its URN or account URL.
            campaign_id: Numeric campaign id, its URN or campaign URL.
            confirm_send: Must be True to save the change.
            ctx: FastMCP context for progress reporting
            daily_budget: New daily budget as a plain number with a dot, e.g.
                "12.5", in the account's currency. Only for a daily budget.
            lifetime_budget: New lifetime budget, same format. Only for a
                lifetime budget.
            start_date: New start date (YYYY-MM-DD), not in the past.
            end_date: New end date (YYYY-MM-DD), only when the ad set has one.
            political_ad: Only when the user has declared it: true if this
                ad set's ads are political, false if they are not. Campaign
                Manager asks this on save for an ad set that targets the EU
                (EU political advertising rules). It is a legal declaration
                the user makes for the advertiser, so never guess or default
                it; leave it out until the user answers. Without it, a save
                that asks stops with declaration_required and sends nothing.

        Returns:
            Dict with url, status, message, retry_safe and, when known,
            target, planned, applied and refused. retry_safe is True only when
            LinkedIn did not apply anything; when False, re-read the ad set before
            calling again.
        """
        try:
            ad_account_id = normalize_ad_account_id(ad_account_id)
            campaign_id = normalize_campaign_id(campaign_id)
            invalid = refuse_invalid_campaign_edit(
                daily_budget=daily_budget,
                lifetime_budget=lifetime_budget,
                start_date=start_date,
                end_date=end_date,
                today=datetime.now(UTC).date(),
            )
            if invalid is not None:
                url = campaign_manager_url(
                    ad_account_id, f"/campaigns/{campaign_id}/details"
                )
                return _invalid("update_campaign", url, invalid)
            return await _apply(
                ctx,
                "update_campaign",
                confirm_send=confirm_send,
                extractor=extractor,
                message="Updating ad set",
                call=lambda ex: ex.update_campaign(
                    ad_account_id,
                    campaign_id,
                    daily_budget=daily_budget,
                    lifetime_budget=lifetime_budget,
                    start_date=start_date,
                    end_date=end_date,
                    confirm_send=confirm_send,
                    political_ad=political_ad,
                ),
            )
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "update_campaign")
        except Exception as e:
            raise_tool_error(e, "update_campaign")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Set Campaign Status",
        annotations=_ANNOTATIONS,
        tags=_TAGS,
        exclude_args=["extractor"],
    )
    async def set_campaign_status(
        ad_account_id: str,
        campaign_id: str,
        status: Literal["PAUSED", "ACTIVE"],
        confirm_send: bool,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Terminology: API campaign group = "Campaign" in Campaign Manager's UI,
        API campaign = "Ad set", API creative = "Ad".

        Pause an active API campaign (UI "Ad set") or resume a paused one.
        A draft is never launched and billing is never touched. A resume
        that Campaign Manager answers with its reactivation dialog is
        dismissed and reported as reactivation_needs_changes.

        confirm_send False is a dry run that verifies the target and returns
        the full planned change.

        Args:
            ad_account_id: Numeric ad account id, its URN or account URL.
            campaign_id: Numeric campaign id, its URN or campaign URL.
            status: PAUSED or ACTIVE.
            confirm_send: Must be True to change the status.
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, status, message, retry_safe and, when known,
            target, planned, applied and refused. retry_safe is True only when
            LinkedIn did not apply anything.
        """
        try:
            ad_account_id = normalize_ad_account_id(ad_account_id)
            campaign_id = normalize_campaign_id(campaign_id)
            return await _apply(
                ctx,
                "set_campaign_status",
                confirm_send=confirm_send,
                extractor=extractor,
                message="Changing ad set status",
                call=lambda ex: ex.set_campaign_status(
                    ad_account_id,
                    campaign_id,
                    status=status,
                    confirm_send=confirm_send,
                ),
            )
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "set_campaign_status")
        except Exception as e:
            raise_tool_error(e, "set_campaign_status")  # NoReturn

    @mcp.tool(
        timeout=targeting_timeout,
        title="Update Campaign Targeting",
        annotations=_ANNOTATIONS,
        tags=_TAGS,
        exclude_args=["extractor"],
    )
    async def update_campaign_targeting(
        ad_account_id: str,
        campaign_id: str,
        confirm_send: bool,
        ctx: Context,
        add: list[TargetingAdd] | None = None,
        remove: list[str] | None = None,
        political_ad: bool | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Terminology: API campaign group = "Campaign" in Campaign Manager's UI,
        API campaign = "Ad set", API creative = "Ad".

        Add or remove targeting values of an API campaign (UI "Ad set"), 1 to
        10 in total. Never launches the ad set and never touches billing.

        confirm_send False is a dry run that verifies the target and returns
        the full planned change, with planned.politicalAdQuestion "will" when
        the save will ask whether the ads are political (the targeting after
        the change includes an EU location), else "may".

        On EU targeting the save first asks whether the ad set's ads are
        political. That answer is a legal declaration the user makes for the
        advertiser: pass political_ad only as the user declared it; the tool
        never defaults it. Saving targeting with no EU location makes Campaign
        Manager clear an existing declaration: that save stops with
        declaration_would_clear, or declaration_cleared_by_linkedin when
        political_ad was given, and sends nothing. In the newer editor the
        save may fill in "not political" itself: it passes only with
        political_ad false (declaration_required without it,
        declaration_not_offered with true).

        Args:
            ad_account_id: Numeric ad account id, its URN or account URL.
            campaign_id: Numeric campaign id, its URN or campaign URL.
            confirm_send: Must be True to save the change.
            ctx: FastMCP context for progress reporting
            add: Values to add, each {urn, query, exclude?}: the urn and the
                query exactly as search_targeting_entities took and returned
                them; exclude true adds it to the exclusions.
            remove: URNs of values the ad set targets now, to remove.
            political_ad: Only when the user has declared it: true if this
                ad set's ads are political, false if they are not. Campaign
                Manager asks this on save for an ad set that targets the EU
                (EU political advertising rules). It is a legal declaration
                the user makes for the advertiser, so never guess or default
                it; leave it out until the user answers. Without it, a save
                that asks stops with declaration_required and sends nothing.

        Returns:
            Dict with url, status, message, retry_safe and, when known,
            target, planned, applied and refused. retry_safe is True only when
            LinkedIn did not apply anything. partially_applied means only part of
            the change was saved; re-read the ad set before calling again.
        """
        try:
            arrived = request_arrived_at.get()
            deadline = (
                time.monotonic() if arrived is None else arrived
            ) + targeting_timeout * DEADLINE_FRACTION
            ad_account_id = normalize_ad_account_id(ad_account_id)
            campaign_id = normalize_campaign_id(campaign_id)
            invalid = refuse_invalid_targeting_change(add, remove)
            if invalid is not None:
                url = campaign_manager_url(
                    ad_account_id, f"/campaigns/{campaign_id}/details"
                )
                return _invalid("update_campaign_targeting", url, invalid)
            adds: list[TargetingAdd] = [
                {**item, "urn": normalize_targeting_urn(item["urn"])}
                for item in add or []
            ]
            removes = [normalize_targeting_urn(urn) for urn in remove or []]
            return await _apply(
                ctx,
                "update_campaign_targeting",
                confirm_send=confirm_send,
                extractor=extractor,
                message="Updating ad set targeting",
                call=lambda ex: ex.update_campaign_targeting(
                    ad_account_id,
                    campaign_id,
                    add=adds or None,
                    remove=removes or None,
                    confirm_send=confirm_send,
                    deadline=deadline,
                    political_ad=political_ad,
                ),
            )
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "update_campaign_targeting")
        except Exception as e:
            raise_tool_error(e, "update_campaign_targeting")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Create Campaign Draft",
        annotations=_ANNOTATIONS,
        tags=_TAGS,
        exclude_args=["extractor"],
    )
    async def create_campaign_draft(
        ad_account_id: str,
        campaign_group_id: str,
        objective: str,
        name: str,
        confirm_send: bool,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Terminology: API campaign group = "Campaign" in Campaign Manager's UI,
        API campaign = "Ad set", API creative = "Ad".

        Create a draft API campaign (UI "Ad set") with an objective and a name
        in an existing campaign group. Never creates a group, never launches
        the draft and never touches billing.

        confirm_send False is a dry run that verifies the target and returns
        the full planned change.

        Args:
            ad_account_id: Numeric ad account id, its URN or account URL.
            campaign_group_id: Numeric id of an existing campaign group, as
                list_campaigns returned it, or its URN.
            objective: Campaign Manager objective in capitals, e.g.
                WEBSITE_VISIT. objective_unavailable lists the ones offered.
            name: Name of the new ad set, 1 to 255 characters.
            confirm_send: Must be True to create the draft.
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, status, message, retry_safe and, when known,
            target, planned, applied, created_id and refused. retry_safe is
            True only when LinkedIn did not apply anything.
        """
        try:
            ad_account_id = normalize_ad_account_id(ad_account_id)
            campaign_group_id = normalize_campaign_group_id(campaign_group_id)
            invalid = refuse_invalid_draft(objective, name)
            if invalid is not None:
                url = campaign_manager_url(ad_account_id, "/campaigns/new/setup")
                return _invalid("create_campaign_draft", url, invalid)
            return await _apply(
                ctx,
                "create_campaign_draft",
                confirm_send=confirm_send,
                extractor=extractor,
                message="Creating draft ad set",
                call=lambda ex: ex.create_campaign_draft(
                    ad_account_id,
                    campaign_group_id,
                    objective=objective,
                    name=name,
                    confirm_send=confirm_send,
                ),
            )
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "create_campaign_draft")
        except Exception as e:
            raise_tool_error(e, "create_campaign_draft")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Add Post to Campaign",
        annotations=_ANNOTATIONS,
        tags=_TAGS,
        exclude_args=["extractor"],
    )
    async def add_post_to_campaign(
        ad_account_id: str,
        campaign_id: str,
        post_urn: str,
        confirm_send: bool,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Terminology: API campaign group = "Campaign" in Campaign Manager's UI,
        API campaign = "Ad set", API creative = "Ad".

        Add a company post as an API creative (UI "Ad") to a draft or paused
        API campaign (UI "Ad set"). An active ad set is refused, because a new
        ad on it would start spending at once. Never launches the ad set and
        never touches billing.

        confirm_send False is a dry run that verifies the target and returns
        the full planned change.

        Args:
            ad_account_id: Numeric ad account id, its URN or account URL.
            campaign_id: Numeric campaign id, its URN or campaign URL.
            post_urn: urn:li:share:<n>, urn:li:ugcPost:<n> or
                urn:li:activity:<n> (as get_company_posts returns it), or the
                post's /feed/update/ link. An activity URN is used only when
                Campaign Manager's content library names the post's activity;
                otherwise it is refused (post_not_found) and the share or
                ugcPost URN is needed. Only the 100 most recent company posts
                can be found.
            confirm_send: Must be True to add the post.
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, status, message, retry_safe and, when known,
            target, planned, applied, created_id and refused. retry_safe is
            True only when LinkedIn did not apply anything.
        """
        try:
            ad_account_id = normalize_ad_account_id(ad_account_id)
            campaign_id = normalize_campaign_id(campaign_id)
            post_urn = normalize_post_urn(post_urn)
            return await _apply(
                ctx,
                "add_post_to_campaign",
                confirm_send=confirm_send,
                extractor=extractor,
                message="Adding post to ad set",
                call=lambda ex: ex.add_post_to_campaign(
                    ad_account_id,
                    campaign_id,
                    post_urn,
                    confirm_send=confirm_send,
                ),
            )
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "add_post_to_campaign")
        except Exception as e:
            raise_tool_error(e, "add_post_to_campaign")  # NoReturn

    @mcp.tool(
        timeout=upload_timeout,
        title="Create Company List Audience",
        annotations=_ANNOTATIONS,
        tags=_TAGS,
        exclude_args=["extractor"],
    )
    async def create_company_list_audience(
        ad_account_id: str,
        name: str,
        companies: list[CompanyRow],
        confirm_send: bool,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Terminology: API campaign group = "Campaign" in Campaign Manager's UI,
        API campaign = "Ad set", API creative = "Ad".

        Upload a list of companies as a matched audience of the ad account.
        When confirm_send is True this accepts the LinkedIn Ads Agreement
        (planned.termsUrl) on the user's behalf for this ad account, as
        Campaign Manager's own upload button does. Never touches billing.

        confirm_send False is a dry run that verifies the target and returns
        the full planned change.

        Args:
            ad_account_id: Numeric ad account id, its URN or account URL.
            name: Audience name, 1 to 200 characters.
            companies: 1 to 10000 companies, each {name, linkedin_url?,
                website?}. Only the name is required; repeats are dropped.
            confirm_send: Must be True to upload the list.
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, status, message, retry_safe and, when known,
            target, planned, created_id and refused. retry_safe is True only
            when LinkedIn did not apply anything. created_id is the listed
            audience's id (its segmentId, as list_audiences reports it);
            applied.sourceId is the uploaded list behind it. A new audience is
            BUILDING, with no member count, until LinkedIn has matched it;
            list_audiences shows its status.
        """
        try:
            ad_account_id = normalize_ad_account_id(ad_account_id)
            url = campaign_manager_url(ad_account_id, "/audiences/matched")
            invalid = refuse_invalid_audience_name(name, saved=False)
            if invalid is None:
                try:
                    company_list_csv(companies, ACCOUNT_TEMPLATE_HEADERS)
                except InvalidReferenceError as exc:
                    invalid = str(exc)
            if invalid is not None:
                return _invalid("create_company_list_audience", url, invalid)
            return await _apply(
                ctx,
                "create_company_list_audience",
                confirm_send=confirm_send,
                extractor=extractor,
                message="Uploading company list",
                call=lambda ex: ex.create_company_list_audience(
                    ad_account_id,
                    name,
                    companies,
                    confirm_send=confirm_send,
                ),
            )
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "create_company_list_audience")
        except Exception as e:
            raise_tool_error(e, "create_company_list_audience")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Create Saved Audience",
        annotations=_ANNOTATIONS,
        tags=_TAGS,
        exclude_args=["extractor"],
    )
    async def create_saved_audience(
        ad_account_id: str,
        campaign_id: str,
        name: str,
        confirm_send: bool,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Terminology: API campaign group = "Campaign" in Campaign Manager's UI,
        API campaign = "Ad set", API creative = "Ad".

        Save an API campaign's (UI "Ad set") targeting as a saved audience of
        the ad account. The ad set itself is not changed. Never touches
        billing.

        confirm_send False is a dry run that verifies the target and returns
        the full planned change.

        Args:
            ad_account_id: Numeric ad account id, its URN or account URL.
            campaign_id: Numeric campaign id, its URN or campaign URL.
            name: Audience name, 1 to 25 characters.
            confirm_send: Must be True to save the audience.
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, status, message, retry_safe and, when known,
            target, planned, created_id and refused. retry_safe is True only
            when LinkedIn did not apply anything.
        """
        try:
            ad_account_id = normalize_ad_account_id(ad_account_id)
            campaign_id = normalize_campaign_id(campaign_id)
            invalid = refuse_invalid_audience_name(name, saved=True)
            if invalid is not None:
                url = campaign_manager_url(
                    ad_account_id, f"/campaigns/{campaign_id}/details"
                )
                return _invalid("create_saved_audience", url, invalid)
            return await _apply(
                ctx,
                "create_saved_audience",
                confirm_send=confirm_send,
                extractor=extractor,
                message="Saving audience",
                call=lambda ex: ex.create_saved_audience(
                    ad_account_id,
                    campaign_id,
                    name,
                    confirm_send=confirm_send,
                ),
            )
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "create_saved_audience")
        except Exception as e:
            raise_tool_error(e, "create_saved_audience")  # NoReturn
