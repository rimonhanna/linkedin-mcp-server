"""
Company page admin tools: invite 1st-degree connections to follow a page.

Both open the page's own "Invite to follow" dialog in the signed-in member's
browser and read what the dialog receives. They work only on a page the
member administers; any other page is reported as such.
"""

import logging
import time
from datetime import datetime
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.debug_trace import retain_trace_for_failed_action
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.pacing import (
    PAGE_INVITES,
    JobStore,
    refuse_action,
    request_arrived_at,
)
from linkedin_mcp_server.scraping.identifiers import normalize_page_company_id
from linkedin_mcp_server.scraping.page_invite_payload import invitee_ref
from linkedin_mcp_server.scraping.page_invites import MAX_CANDIDATES, MAX_INVITEES
from linkedin_mcp_server.tools.enrichment import DEADLINE_FRACTION
from linkedin_mcp_server.tools.person import StrList

logger = logging.getLogger(__name__)

# Statuses after which the call needs no explaining; every other keeps the
# run's trace (a selection that did not match, an unknown outcome).
_INVITE_SUCCEEDED = frozenset(
    {"dry_run", "sent", "nothing_to_invite", "not_page_admin"}
)
_MAX_KEYWORDS_LENGTH = 100


def _deadline(tool_timeout: float) -> float:
    arrived = request_arrived_at.get()
    return (
        time.monotonic() if arrived is None else arrived
    ) + tool_timeout * DEADLINE_FRACTION


def _people(values: list[str]) -> list[str]:
    """The people in the caller's order, once each; refuses an unreadable one."""
    people: list[str] = []
    for index, value in enumerate(values):
        if invitee_ref(value) is None:
            raise ToolError(
                f"people[{index}] is not a LinkedIn profile, lead or member "
                "identifier. Pass a profile id, profile URL or username, a Sales "
                "Navigator lead id, URN or URL, or a urn:li:member URN."
            )
        if value.strip() not in people:
            people.append(value.strip())
    if not people:
        raise ToolError("people must name at least one person.")
    if len(people) > MAX_INVITEES:
        raise ToolError(f"people may name at most {MAX_INVITEES} people per call.")
    return people


def _names(values: list[str], names: list[str] | None) -> dict[str, str | None]:
    """Each person's search name, keyed as ``_people`` keeps them."""
    if names is None:
        return {}
    if len(names) != len(values):
        raise ToolError(
            "names must give one name per person, in the order of people "
            "(an empty string for a person without one)."
        )
    by_person: dict[str, str | None] = {}
    for index, (value, name) in enumerate(zip(values, names, strict=True)):
        try:
            clean = _keywords(name)
        except ToolError:
            raise ToolError(
                f"names[{index}] must be plain text of at most "
                f"{_MAX_KEYWORDS_LENGTH} characters."
            ) from None
        by_person.setdefault(value.strip(), clean)
    return by_person


def _keywords(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = " ".join(value.split())
    if len(value) > _MAX_KEYWORDS_LENGTH or any(ord(c) < 32 for c in value):
        raise ToolError(
            f"keywords must be plain text of at most {_MAX_KEYWORDS_LENGTH} characters."
        )
    return value


def register_company_page_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register the company page admin tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="List Page Follow Candidates",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"company", "scraping"},
        exclude_args=["extractor"],
    )
    async def list_page_follow_candidates(
        company_id: str,
        ctx: Context,
        keywords: str | None = None,
        limit: Annotated[int, Field(ge=1, le=MAX_CANDIDATES)] = 20,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        List the connections a page admin can invite to follow a company page,
        and the page's monthly invitation credits. Read-only: nothing is ticked
        or sent.

        Works only for a page the signed-in account administers; for any other
        page it says so. One page load; each 10 candidates past the first 20
        cost one "Show more results" in the dialog.

        Args:
            company_id: The page's numeric company id, or its /company/<id>/
                admin URL.
            ctx: FastMCP context for progress reporting
            keywords: Search the connections by name (the dialog's own search,
                up to 40 results, no paging).
            limit: Most candidates to return, 1 to 200 (default 20).

        Returns:
            Dict with url, company_id, sections (candidates -> one line each),
            candidates (profileId, name, headline, publicIdentifier when known,
            profileUrl, invitable: true, false or null when LinkedIn did not
            say, with section_errors.invitable then), credits (remaining,
            total, nextGrantOn; null with section_errors.credits when not
            shown) and paging (count, more,
            reason: limit, complete, search, max_pages, deadline,
            more_control_not_found or page_not_loaded).
        """
        try:
            company_id = normalize_page_company_id(company_id)
            keywords = _keywords(keywords)
            deadline = _deadline(tool_timeout)
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="list_page_follow_candidates"
            )
            logger.info("Listing follow-invite candidates for page %s", company_id)
            await ctx.report_progress(
                progress=0, total=100, message="Opening the page's invite dialog"
            )
            result = await extractor.list_page_follow_candidates(
                company_id, keywords=keywords, limit=limit, deadline=deadline
            )
            await ctx.report_progress(progress=100, total=100, message="Complete")
            return result
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "list_page_follow_candidates")
        except Exception as e:
            raise_tool_error(e, "list_page_follow_candidates")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Invite To Follow Page",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"company", "actions"},
        exclude_args=["extractor"],
    )
    async def invite_to_follow_page(
        company_id: str,
        people: StrList,
        confirm_send: bool,
        ctx: Context,
        names: StrList | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Invite connections to follow a company page the account administers.

        Invites exactly the people passed and no one else. With confirm_send
        false nothing is ticked or sent: the dialog is read and the result
        says who would be invited and how many credits that uses. Call it that
        way first, show the user the plan, and confirm only what they approved.

        Each invitation costs the page one monthly credit (refunded when
        accepted) and counts against the account's daily page-invite cap
        (PAGE_INVITES_MAX, default 20 per 24 h); people past the remaining
        credits or the cap are not invited. Each person is matched to a row of
        the dialog's candidate list by member id or public profile name, never
        by display name; up to 300 candidates are paged through. The dialog
        lists only about the oldest 100 connections, so pass names for anyone
        else: a person not among the candidates is looked up once with the
        dialog's own name search, and a result is taken only when its id is
        theirs. Paging finds its control by its English label, so on a
        LinkedIn interface in another language only the first 20 candidates
        are read, and anyone past them is reported not_found with
        "more_control_not_found" in the detail. Someone who is not a
        1st-degree connection, already follows or was already invited is
        reported not_invitable, not as an error. An invitation whose outcome
        is unknown is never sent again: check the page before calling again.

        Sends are refused outside working hours, which is this server's own
        schedule and not a LinkedIn limit; a dry run then still plans in full
        under the caps and says in schedule when sends resume. Only
        WORKING_HOURS_DISABLED=1 lifts it; this tool has no ignore_schedule.

        Args:
            company_id: The page's numeric company id, or its /company/<id>/
                admin URL.
            people: The people to invite (1 to 20), in any form the other tools
                return: a profileId from list_page_follow_candidates, a
                profile URL or username, a Sales Navigator lead id, lead URN
                (urn:li:fs_salesProfile:(...)) or lead URL, or a
                urn:li:member URN (a lead's memberUrn).
            confirm_send: Must be true to send; false is a dry run.
            ctx: FastMCP context for progress reporting
            names: Optional display names, one per person in the order of
                people ("" for none). Used only to search the dialog for a
                person its candidate list does not show; the match is still
                by id. A search result carries no username, so a person
                given as a username or /in/<username>/ URL is not searched
                for: pass their id or URN.

        Returns:
            Dict with url, company_id, status, message, dry_run, retry_safe,
            people and credits (remaining, total, nextGrantOn, needed), plus
            pacing when a cap or cooldown held people back (prefixed
            outside_working_hours when only the schedule did), and schedule
            on a dry run outside working hours. When LinkedIn
            does not show the credits, credits is null and
            section_errors.credits says why: a dry run still reports each
            person, and would_invite is not limited by credits, while a send
            stops with credits_unavailable and invites no one.
            status: dry_run, sent, send_unconfirmed, refused_by_linkedin,
            nothing_to_invite, not_page_admin, credits_unavailable,
            selection_failed, selection_mismatch, invite_control_not_found,
            deadline (too little time was left to send; nothing was ticked)
            or blocked_by_guard. retry_safe is false once an invitation
            request may have left the browser.
            Each people entry has input (as passed), status (would_invite,
            invited, refused, unconfirmed, not_sent, not_invitable, not_found,
            invitability_unknown, no_credits, pacing_cap or duplicate), and
            profileId, name, headline, publicIdentifier and detail when known.
            A not_invitable detail is not_a_candidate (the whole list was
            read, or the person's name search returned, and no row matched
            this identifier), ambiguous_identifier
            (more than one row matched it, so none was chosen) or
            linkedin_declined (LinkedIn's own status label follows when it
            gave one). A not_found detail is not_in_candidates_read (paging
            stopped before the end; says how many were read and why) or
            unmatchable_identifier (a Sales Navigator id that names no
            dialog row) or name_search_not_run (deadline) (too little time
            was left to search for them). A refused detail is the HTTP status, or
            rate_limited when LinkedIn answered 429. An invitability_unknown
            detail is status_not_received: LinkedIn did not say whether the
            person can be invited, so they were not; calling again may.
        """
        try:
            company_id = normalize_page_company_id(company_id)
            search_names = _names(people, names)
            people = _people(people)
            if confirm_send:
                # Before the browser: a refused invite must not spend a page load.
                refuse_action(JobStore(), PAGE_INVITES, datetime.now().astimezone())
            deadline = _deadline(tool_timeout)
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="invite_to_follow_page"
            )
            logger.info(
                "Inviting %d people to follow page %s (confirm_send=%s)",
                len(people),
                company_id,
                confirm_send,
            )
            await ctx.report_progress(
                progress=0, total=100, message="Opening the page's invite dialog"
            )
            result = await extractor.invite_to_follow_page(
                company_id,
                people,
                confirm_send=confirm_send,
                names=[search_names.get(p) for p in people] if search_names else None,
                deadline=deadline,
            )
            retain_trace_for_failed_action(
                "invite_to_follow_page",
                result.get("status"),
                succeeded=_INVITE_SUCCEEDED,
            )
            await ctx.report_progress(progress=100, total=100, message="Complete")
            return result
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "invite_to_follow_page")
        except Exception as e:
            raise_tool_error(e, "invite_to_follow_page")  # NoReturn
