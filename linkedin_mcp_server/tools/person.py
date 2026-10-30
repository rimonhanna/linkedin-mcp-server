"""
LinkedIn person profile scraping tools.

Uses innerText extraction for resilient profile data capture
with configurable section selection.
"""

import json
import logging
import time
from datetime import datetime
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BeforeValidator, Field

from linkedin_mcp_server.callbacks import MCPContextProgressCallback
from linkedin_mcp_server.config.schema import (
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    PER_TOOL_TIMEOUT_SECONDS,
)
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.pacing import (
    INVITES,
    JobStore,
    record_action,
    refuse_action,
    request_arrived_at,
)
from linkedin_mcp_server.scraping import parse_person_sections
from linkedin_mcp_server.scraping.contracts import FilterValidationError
from linkedin_mcp_server.scraping.identifiers import normalize_person_identifier
from linkedin_mcp_server.scraping.invitation_withdrawal import MAX_WITHDRAWALS_LIMIT
from linkedin_mcp_server.tools.enrichment import DEADLINE_FRACTION

logger = logging.getLogger(__name__)

# Outcomes after which an invitation exists, or may: the unverified one is
# counted because a repeat can invite twice. ``not_sent`` and
# ``dialog_not_found`` are LinkedIn declining, and a decline is not an invite.
# (``outcome_unknown`` is synthesised by the frontend proxy when the daemon
# went away mid-call, so this tool body never sees it.)
_INVITE_SUBMITTED = frozenset({"connected", "send_unverified"})


def _coerce_str_list(value: Any) -> Any:
    """Accept a string where a list of strings is declared.

    ``network`` is published as ``anyOf: [array, null]``, which is a correct
    JSON Schema. Some MCP clients collapse an ``anyOf``-with-null union to an
    untyped ``{}`` and then transmit the value as a string, so the array the
    caller wrote never arrives as one and pydantic rejects it (#739).

    Coercing at the tool boundary keeps the transport quirk here and leaves
    ``LinkedInExtractor.search_people`` strictly ``list[str]``. Only the
    container shape is repaired; token values are still validated downstream,
    so an invalid token fails with the same message it always did.
    """
    if not isinstance(value, str):
        return value

    text = value.strip()
    if text.startswith("["):
        try:
            decoded = json.loads(text)
        except ValueError:
            pass
        else:
            if isinstance(decoded, list):
                return decoded

    return [part.strip() for part in text.split(",") if part.strip()]


StrList = Annotated[list[str], BeforeValidator(_coerce_str_list)]


def register_person_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register all person-related tools with the MCP server."""
    # Loading the sent list and the paced withdrawals of a full run (up to 50,
    # seconds apart) do not fit the default; a larger configured value wins.
    withdraw_timeout = max(
        tool_timeout, PER_TOOL_TIMEOUT_SECONDS["withdraw_invitations"]
    )

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Person Profile",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_person_profile(
        linkedin_username: str,
        ctx: Context,
        sections: str | None = None,
        max_scrolls: Annotated[int, Field(ge=1, le=50)] | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Get a specific person's LinkedIn profile.

        Args:
            linkedin_username: LinkedIn username (e.g., "stickerdaniel", "williamhgates"). A full profile URL is accepted too and is reduced to the username.
            ctx: FastMCP context for progress reporting
            sections: Comma-separated list of extra sections to scrape.
                The main profile page is always included.
                Available sections: experience, education, interests, honors, languages, certifications, skills, projects, contact_info, posts
                Examples: "experience,education", "contact_info", "skills,projects", "honors,languages", "posts"
                Default (None) scrapes only the main profile page.
            max_scrolls: Maximum pagination attempts per section to load more content.
                On detail sections (experience, certifications, skills, etc.) this
                is the max number of "Show more" button clicks. On activity/posts
                it is the max scroll-to-bottom iterations. Applies to all sections
                in this call. Default (None) uses 5 for detail sections and 10 for
                posts. Increase when a profile has many items in a section
                (e.g., 30+ certifications, max_scrolls=20). To avoid slowing down
                other sections, request heavy sections in a separate call.

        Returns:
            Dict with url, sections (name -> raw text), and optional references and section_errors.
            Sections may be absent if extraction yielded no content for that page.
            contact_info is read only from an accepted contact-overlay root. If no such root
            is found, the section is omitted and section_errors explains the failure;
            underlying profile text and links are never substituted. Existing suspected
            rate-limit retry and stop behavior is retained.
            Includes public_profile_url when the loaded page resolves to a public profile;
            opaque member IDs require a matching top-card member identity first.
            Includes unknown_sections when unrecognised names are passed.
            The LLM should parse the raw text in each section.
        """
        try:
            linkedin_username = normalize_person_identifier(linkedin_username)
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_person_profile"
            )
            requested, unknown = parse_person_sections(sections)

            logger.info(
                "Scraping profile: %s (sections=%s)",
                linkedin_username,
                sections,
            )

            cb = MCPContextProgressCallback(ctx)
            result = await extractor.scrape_person(
                linkedin_username,
                requested,
                callbacks=cb,
                max_scrolls=max_scrolls,
            )

            if unknown:
                result["unknown_sections"] = unknown

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_person_profile")
        except Exception as e:
            raise_tool_error(e, "get_person_profile")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Search People",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "search"},
        exclude_args=["extractor"],
    )
    async def search_people(
        ctx: Context,
        keywords: str | None = None,
        location: str | None = None,
        network: StrList | None = None,
        current_company: StrList | None = None,
        max_pages: Annotated[int, Field(ge=1, le=10)] = 1,
        title: str | None = None,
        past_company: StrList | None = None,
        industry: StrList | None = None,
        school: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        profile_language: StrList | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Search for people on LinkedIn, with Clay-style facets.

        At least one of keywords or a facet is required. Every result page is
        one navigation against the daily budget, and each company name that
        has to be resolved costs one or two more; pass numeric ids (or
        companies already in the cache) to keep it at one per page.

        Recommended funnel for account-based prospecting: search_companies
        (industry/size/hq_location facets) -> enrich_companies(about=True)
        -> query_company_cache to pick the accounts -> search_people(
        current_company=[...ids or names...], keywords='"<title>"') for
        the people.

        Args:
            ctx: FastMCP context for progress reporting
            keywords: Free-text query (e.g., "software engineer", "recruiter
                at Google"). Boolean search works on free LinkedIn: AND, OR,
                NOT, quoted phrases and parentheses, e.g.
                '("head of sales" OR "VP sales") AND NOT recruiter'.
            location: Optional location filter: a country or city name
                (e.g., "Egypt", "United Arab Emirates", "Amsterdam"). It is
                resolved to LinkedIn's numeric geo id through the site's own
                location dropdown, so free-text names actually filter. A name
                LinkedIn's dropdown does not recognize raises an error rather
                than silently returning worldwide results.
            network: Optional connection-degree filter. Each element is one of
                "F" (1st-degree), "S" (2nd-degree), "O" (3rd-degree and beyond).
                Example: ["F"] to only return 1st-degree connections. A single
                token ("F") or a comma-separated string ("F,S") is also
                accepted, for clients that cannot transmit an array.
            current_company: Optional current-employer filter, one or a list.
                Each is a company name (e.g. "SAP"), a /company/<slug> URL, or
                the numeric company URN id (e.g. "1115" for SAP). LinkedIn's
                currentCompany facet filters on the id only, so a name or URL
                is resolved to it first (company search, then the company's
                About page; cached on disk so a company already looked up
                costs no navigation). A name that does not resolve raises an
                error rather than silently returning the unfiltered result
                set. Pass the id directly, as exposed by get_company_profile
                under references["about"], to skip the resolution. For
                company-wide employee demographics (location/education/
                function breakdown) plus a slug-based lookup, use
                get_company_employees instead.
            max_pages: Number of result pages to load, 1-10 (default 1).
                LinkedIn returns 10 people per page, so max_pages=10 yields up
                to 100. Pagination stops early once a page adds no new people.
                Raise this when you need more than a top-10 sample -- e.g.
                enumerating 1st-degree connections in a region with
                network=["F"].
            title: Optional current job title, free text (e.g. "Head of
                Sales"). Measured live (2026-09-12) as silently ignored by
                LinkedIn's current results page: the results did not match
                the title. Prefer putting the title in keywords as a quoted
                phrase, e.g. '"VP Engineering"', which does filter; this
                parameter is kept for a results-page variant that may still
                read it and is never merged into keywords for you. On its
                own it is refused with an error rather than returning an
                unfiltered worldwide list: combine it with another facet
                (location, current_company, ...) or use keywords.
            past_company: Optional past-employer filter; same shapes and
                resolution as current_company. Each unresolved name may cost
                up to two navigations. The facet's URL parameter name is
                unverified against live LinkedIn; a wrong name is ignored,
                so cross-check results.
            industry: Optional industry filter, one or a list. Each is
                LinkedIn's numeric industry id (e.g. "4") or a name this
                server knows (e.g. "Software Development", "Financial
                Services"; same table as search_companies). An unknown name
                raises an error listing the known names. The facet's URL
                parameter name and values are unverified against live
                LinkedIn; a wrong name is ignored, so cross-check results.
            school: Optional school filter, the numeric school id only (a
                name raises an error: LinkedIn's schools search exposes no
                id to resolve it from). To find the id: LinkedIn people
                search -> All filters -> School -> pick one; the URL then
                shows schoolFilter=["<id>"]. The facet's URL parameter name
                is unverified against live LinkedIn; a wrong name is
                ignored, so cross-check results.
            first_name: Optional first-name filter (verified live).
            last_name: Optional last-name filter. The facet's URL parameter
                name is unverified against live LinkedIn; a wrong name is
                ignored, so cross-check results.
            profile_language: Optional profile-language filter, one or a list
                of two-letter ISO 639-1 codes (e.g. "en", "de", "fr"). The
                facet's URL parameter name and values are unverified against
                live LinkedIn; a wrong name is ignored, so cross-check
                results.

        Returns:
            Dict with url, sections (name -> raw text), people, result_count,
            and optional references. Pages are joined by a "---" line in the
            raw text; references are deduplicated by URL across pages.
            people is a list of rows {name, degree, headline, location,
            snippet, url[, followers]} parsed from the raw text, deduplicated
            by url across pages; url is null when a card could not be paired
            with a profile link. result_count is the "About N results" header
            of the first page, or null; the measured people-search page
            renders no such header, so expect null here (company search
            has one). Fall back to the raw text for anything the rows do
            not carry.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="search_people"
            )
            logger.info(
                "Searching people: keywords='%s', location='%s', network=%s, "
                "current_company=%s, past_company=%s, title='%s', industry=%s, "
                "school='%s', max_pages=%d",
                keywords,
                location,
                network,
                current_company,
                past_company,
                title,
                industry,
                school,
                max_pages,
            )

            await ctx.report_progress(
                progress=0, total=100, message="Starting people search"
            )

            try:
                result = await extractor.search_people(
                    keywords,
                    location,
                    network=network,
                    current_company=current_company,
                    max_pages=max_pages,
                    title=title,
                    past_company=past_company,
                    industry=industry,
                    school=school,
                    first_name=first_name,
                    last_name=last_name,
                    profile_language=profile_language,
                )
            except FilterValidationError as e:
                # Validation messages carry actionable detail; surface
                # them as ToolError so mask_error_details doesn't reduce
                # them to "Error calling tool 'search_people'".
                raise ToolError(str(e)) from e

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "search_people")
        except Exception as e:
            raise_tool_error(e, "search_people")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Connect With Person",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"person", "actions"},
        exclude_args=["extractor"],
    )
    async def connect_with_person(
        linkedin_username: str,
        ctx: Context,
        note: str | None = None,
        include_profile: bool = False,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Send a LinkedIn connection request or accept an incoming one.

        The tool is annotated with destructiveHint so MCP clients will
        prompt for user confirmation before execution.

        Args:
            linkedin_username: LinkedIn username (e.g., "stickerdaniel", "williamhgates"). A full profile URL is accepted too and is reduced to the username.
            ctx: FastMCP context for progress reporting
            note: Optional note to include with the invitation
            include_profile: Also return the scraped profile page text under ``profile``. Off by default to keep the result small.

        Returns:
            Dict with url, status, message, and note_sent, plus ``profile``
            when include_profile is true.
            Statuses: pending, already_connected, connect_unavailable,
            unavailable, dialog_not_found, not_sent, send_unverified,
            send_failed, custom_note_limit_reached, connected, or accepted.

            ``connected`` is returned only after the invitation was found
            on the sent-invitations list. ``not_sent`` means the invite
            dialog was submitted but LinkedIn created no invitation (the
            ``message`` carries the dialog's raw text, e.g. a weekly limit
            notice). ``dialog_not_found`` means the invite deeplink opened
            no dialog at all; the ``message`` names the landing URL and any
            page notice. ``send_unverified`` means the dialog was submitted
            but the sent list could not be read; check it before retrying.

            When status is ``custom_note_limit_reached`` LinkedIn rejected
            personalized invite notes because the free note quota for the
            account is exhausted. The ``message`` is the raw Premium dialog
            text read from LinkedIn.

            A status of ``outcome_unknown`` comes from the transport rather
            than the page: the browser process went away with the call in
            flight, so whether the invitation was sent is unknown. It carries
            ``retry_safe: False`` and no ``note_sent``; check the profile
            before calling again, because a repeat may invite twice.
        """
        try:
            # Before the browser: a refused invite must not spend a page load.
            refuse_action(JobStore(), INVITES, datetime.now().astimezone())
            linkedin_username = normalize_person_identifier(linkedin_username)
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="connect_with_person"
            )
            logger.info(
                "Connecting with person: %s (note=%s)",
                linkedin_username,
                note is not None,
            )

            await ctx.report_progress(
                progress=0,
                total=100,
                message="Starting LinkedIn connection flow",
            )

            result = await extractor.connect_with_person(
                linkedin_username,
                note=note,
            )
            if result.get("status") in _INVITE_SUBMITTED:
                record_action(JobStore(), INVITES, datetime.now().astimezone())

            await ctx.report_progress(progress=100, total=100, message="Complete")

            if not include_profile:
                result = {k: v for k, v in result.items() if k != "profile"}
            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "connect_with_person")
        except Exception as e:
            raise_tool_error(e, "connect_with_person")  # NoReturn

    @mcp.tool(
        timeout=withdraw_timeout,
        title="Withdraw Invitations",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"person", "actions"},
        exclude_args=["extractor"],
    )
    async def withdraw_invitations(
        ctx: Context,
        older_than_days: Annotated[int, Field(ge=1)] = 30,
        exclude_usernames: StrList | None = None,
        max_withdrawals: Annotated[int, Field(ge=1, le=MAX_WITHDRAWALS_LIMIT)] = 20,
        dry_run: bool = True,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Withdraw pending sent connection invitations older than a threshold.

        The tool is annotated with destructiveHint so MCP clients will
        prompt for user confirmation before execution. It defaults to a
        dry run: nothing is withdrawn until dry_run is set to false (a dry run
        still scrolls the list to load every invitation).

        Args:
            older_than_days: Withdraw invitations sent at least this many days ago (minimum 1, default 30). Ages are read from LinkedIn's "Sent 3 weeks ago" text and are approximate (a week is 7 days, a month 30). An invitation whose age cannot be read is never withdrawn.
            ctx: FastMCP context for progress reporting
            exclude_usernames: LinkedIn usernames or /in/ profile URLs that must never be withdrawn
            max_withdrawals: Hard upper bound on withdrawals in one call (1-50, default 20); the oldest invitations go first
            dry_run: When true (default), only list what would be withdrawn

        Returns:
            Dict with withdrawn, unconfirmed and skipped_excluded and skipped_unparsed_age
            (usernames), would_withdraw (dry run only), rows_seen (invitations read
            from the page) and stopped_reason.
            withdrawn and would_withdraw rows carry username, name and
            age_text; withdrawn and unconfirmed rows also carry alert_text,
            the raw toast or banner text seen after the withdrawal. stopped_reason is null when the run finished; otherwise
            it says why it stopped early (no_rows_found when the page showed no
            invitations, so an empty result with rows_seen 0 means the page could
            not be read, not that nothing is old enough; list_not_fully_loaded when the list did not finish loading before the call deadline, nothing is withdrawn then; confirmation_dialog_name_mismatch when the dialog named someone else, nothing is pressed; account cooldown, the hourly cap, the call deadline, a LinkedIn
            throttle, or a withdraw control that could not be confirmed),
            and withdrawn holds what was done before that. unconfirmed lists an
            invitation whose confirm button was pressed but whose removal
            could not be verified (it may or may not be withdrawn; check the
            sent list before retrying).
        """
        try:
            # From arrival at the middleware, as the bulk tools do: the pauses
            # between withdrawals can outlast the tool timeout, and a call
            # cancelled by it would lose the report of what was withdrawn.
            arrived = request_arrived_at.get()
            deadline = (
                time.monotonic() if arrived is None else arrived
            ) + withdraw_timeout * DEADLINE_FRACTION
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="withdraw_invitations"
            )
            logger.info(
                "Withdrawing invitations older than %s days (dry_run=%s)",
                older_than_days,
                dry_run,
            )

            await ctx.report_progress(
                progress=0, total=100, message="Reading sent invitations"
            )

            result = await extractor.withdraw_invitations(
                older_than_days,
                exclude_usernames=exclude_usernames,
                max_withdrawals=max_withdrawals,
                dry_run=dry_run,
                deadline=deadline,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "withdraw_invitations")
        except Exception as e:
            raise_tool_error(e, "withdraw_invitations")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Sidebar Profiles",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_sidebar_profiles(
        linkedin_username: str,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Get profile links from sidebar recommendation sections on a LinkedIn profile page.

        Extracts profiles from "More profiles for you", "Explore premium profiles",
        and "People you may know" sidebar sections. Follows "Show all" links to
        return the full list from each section. Sections that redirect to
        linkedin.com/premium are skipped.

        Args:
            linkedin_username: LinkedIn username of the profile page to scrape; a full profile URL is accepted too
                (e.g., "stickerdaniel", "williamhgates")
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url and sidebar_profiles mapping section key to a list of
            /in/username/ paths. Only sections present on the page are included.
        """
        try:
            linkedin_username = normalize_person_identifier(linkedin_username)
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_sidebar_profiles"
            )
            logger.info("Getting sidebar profiles for: %s", linkedin_username)

            await ctx.report_progress(
                progress=0, total=100, message="Extracting sidebar profiles"
            )

            result = await extractor.get_sidebar_profiles(linkedin_username)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_sidebar_profiles")
        except Exception as e:
            raise_tool_error(e, "get_sidebar_profiles")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get My Profile",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_my_profile(
        ctx: Context,
        sections: str | None = None,
        max_scrolls: Annotated[int, Field(ge=1, le=50)] | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Get the authenticated user's own LinkedIn profile.

        Navigates to /in/me/ and resolves the redirect to obtain the real
        username before scraping, so the url field in the result is the actual
        profile URL (e.g. linkedin.com/in/johndoe/) rather than /in/me/.

        Args:
            ctx: FastMCP context for progress reporting
            sections: Comma-separated list of extra sections to scrape.
                The main profile page is always included.
                Available sections: experience, education, interests, honors, languages, certifications, skills, projects, contact_info, posts
                Examples: "experience,education", "contact_info", "skills,projects"
                Default (None) scrapes only the main profile page.
            max_scrolls: Maximum pagination attempts per section (same as get_person_profile).

        Returns:
            Dict with url, sections (name -> raw text), and optional references and section_errors.
            The url field reflects the resolved profile URL, revealing the real username.
            Sections may be absent if extraction yielded no content for that page.
            contact_info is read only from an accepted contact-overlay root. If no such root
            is found, the section is omitted and section_errors explains the failure;
            underlying profile text and links are never substituted. Existing suspected
            rate-limit retry and stop behavior is retained.
            Includes unknown_sections when unrecognised names are passed.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_my_profile"
            )
            requested, unknown = parse_person_sections(sections)

            logger.info("Scraping own profile (sections=%s)", sections)

            cb = MCPContextProgressCallback(ctx)
            result = await extractor.get_my_profile(
                sections=requested,
                callbacks=cb,
                max_scrolls=max_scrolls,
            )

            if unknown:
                result["unknown_sections"] = unknown

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_my_profile")
        except Exception as e:
            raise_tool_error(e, "get_my_profile")  # NoReturn
