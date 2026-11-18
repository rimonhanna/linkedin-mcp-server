"""Campaign Manager reads: ad accounts, campaign groups, campaigns and analytics.

Naming follows the API, never the UI, whose labels moved in 2026: an API
campaign group is the UI's "Campaign", an API campaign its "Ad set" and an API
creative its "Ad". Every read comes from the responses Campaign Manager itself
fetches; the one request of ours is a replay of its own analytics query for a
date range or a demographic pivot the page did not ask for.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, NamedTuple, cast
from urllib.parse import urlparse

import logging
import re

from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
    LinkedInScraperException,
)
from linkedin_mcp_server.scraping.campaign_manager_api import (
    DEMOGRAPHIC_PIVOTS,
    LEVEL_PIVOTS,
    SCOPE_ID_PARAMS,
    CampaignManagerCapture,
    CmResponse,
    analytics_call_matches,
    period_order,
    template_call,
)
from linkedin_mcp_server.scraping.campaign_manager_guard import (
    without_campaign_manager_writes,
)
from linkedin_mcp_server.scraping.campaign_manager_payload import (
    AdAccount,
    AnalyticsRow,
    CampaignGroup,
    CmCall,
    Creative,
    DateWindow,
    _REPORTED_METRICS,
    build_ad_account,
    build_analytics_rows,
    build_boost_account,
    build_campaign,
    build_campaign_group,
    build_creative,
    envelope_elements,
    envelope_record,
    envelope_total,
    merge_by_id,
    render_accounts,
    render_analytics,
    render_campaign,
    render_campaigns,
    restli_date_range,
    restli_list,
    same_id,
)
from linkedin_mcp_server.scraping.identifiers import (
    CAMPAIGN_MANAGER_ACCOUNTS_URL,
    campaign_manager_date_range,
    campaign_manager_url,
    normalize_ad_account_id,
    normalize_campaign_id,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

logger = logging.getLogger(__name__)

_LINKEDIN = "https://www.linkedin.com"
# Where a dormant account's list pages land after a full page load.
_SETUP_PATH = "/campaigns/new/"
# Campaign Manager's in-app route change, which reaches a list page that a
# full load of the same URL redirects away from.
_ROUTE_SCRIPT = """(path) => {
    /* linkedin-mcp:cm-route */
    history.pushState(history.state, '', path);
    window.dispatchEvent(new PopStateEvent('popstate', { state: history.state }));
    return true;
}"""
# The boost manager loads the member's personal boost account, which the
# account list's own finder leaves out.
_BOOST_ACCOUNTS_PATH = "/campaignmanager/boost/boostaccounts/"
# A boost account's ``type``; its reporting is not on the accounts pages.
_BOOST_ACCOUNT_TYPE = "PERSONAL"
_READY_TIMEOUT = 12.0
# For a response the page may not send; a miss is no throttle evidence.
_OPTIONAL_WAIT = 5.0
_BOOST_ON_LIST_WAIT = 3.0
_LEVEL_TABS = {
    "campaign_group": "/campaign-groups",
    "campaign": "/campaigns",
    "creative": "/creatives",
}
_REPORTING_LISTS = {
    "campaignManagerReportingCampaignGroups": "campaign_group",
    "campaignManagerReportingCampaigns": "campaign",
    "campaignManagerReportingCreatives": "creative",
}
# A draft never served, so analytics need not cover it.
_NEVER_DELIVERED = "DRAFT"
# What LinkedIn answers for an entity the member cannot open or that is gone.
_REFUSED_STATUSES = frozenset({403, 404})
_FIELD_NAME = re.compile(r"[A-Za-z]+")
_PARTIAL = "partial, not the ad account's total: see section_errors\n"


def _error(error_type: str, message: str) -> dict[str, str]:
    return {"error_type": error_type, "error_message": message}


def _redirect_error(what: str) -> dict[str, str]:
    return _error(
        "account_setup_redirect",
        "Campaign Manager opened its campaign setup page for this ad account "
        f"instead of its {what}, which it does for an account with no active "
        f"campaign, so the {what} could not be read.",
    )


def _resource(
    response: CmResponse, resource: str, *, finder: str | None = None
) -> bool:
    return (
        response.ok
        and response.call.resource == resource
        and response.call.finder == finder
    )


def _for_account(response: CmResponse, account_id: str) -> bool:
    return response.call.params.get("accountId") == account_id


def _page_limit(name: str, total: int, seen: int) -> dict[str, str]:
    return _error(
        "page_limit",
        f"Campaign Manager reported {total} {name} and returned {seen} on its "
        "first page; the rest were not read.",
    )


def _merged_elements(
    responses: list[CmResponse],
) -> tuple[list[dict[str, Any]], int | None] | None:
    """Elements of every response merged by id, and the largest reported total."""
    elements: list[dict[str, Any]] = []
    total: int | None = None
    for response in responses:
        items = envelope_elements(response.payload)
        if items is None:
            return None
        elements.extend(items)
        reported = envelope_total(response.payload)
        if reported is not None and (total is None or reported > total):
            total = reported
    return merge_by_id(elements), total


def _refusal(
    account_id: str, campaign_id: str | None = None
) -> Callable[[CmResponse], str | None]:
    """The input field a refused response names, so a wrong id fails at once."""

    def refused(response: CmResponse) -> str | None:
        if response.status not in _REFUSED_STATUSES:
            return None
        call = response.call
        if (
            campaign_id is not None
            and call.resource == "campaignManagerCampaigns"
            and call.entity_id == campaign_id
        ):
            return "campaign_id"
        if (
            call.resource in _REPORTING_LISTS and _for_account(response, account_id)
        ) or _account_wide(call, account_id):
            return "ad_account_id"
        return None

    return refused


def _account_wide(call: CmCall, account_id: str) -> bool:
    return restli_list(call.params.get("accountIds", "")) == [account_id]


def _unverified(what: str) -> LinkedInScraperException:
    return LinkedInScraperException(
        f"Campaign Manager returned {what} in a shape this tool does not "
        "recognise, so nothing was reported."
    )


class CampaignManagerReader:
    """Own every workflow whose subject is a Campaign Manager ad account."""

    def __init__(self, session: ScrapingSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    @asynccontextmanager
    async def _reading(self) -> AsyncIterator[CampaignManagerCapture]:
        page = self._session.page
        async with without_campaign_manager_writes(page):
            async with CampaignManagerCapture(page) as capture:
                yield capture

    async def _route_to(self, path: str) -> None:
        """Change the app's route in place; charged like the page view it is."""
        self._navigator._charge_navigation(f"{_LINKEDIN}{path}")
        await self._session.page.evaluate(_ROUTE_SCRIPT, path)

    async def _open(
        self,
        capture: CampaignManagerCapture,
        url: str,
        *,
        tab_path: str | None,
        ready: Callable[[CmResponse], bool],
        refused: Callable[[CmResponse], str | None] | None = None,
        throttle: bool = True,
        wanted: tuple[str, ...] = (),
    ) -> CmResponse | None:
        """Load ``url`` and wait for its data; None if it stayed on setup.

        A dormant account's list page redirects to campaign setup on a full
        load (detected by URL path, in any UI language). One in-app route hop
        to ``tab_path`` reaches the list; a hop that still yields nothing
        returns None rather than raising. A response ``refused`` names ends the
        wait as a wrong id, not as a slow page.
        """
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        await self._session.dismiss_modal()
        redirected = False
        if (
            tab_path is not None
            and _SETUP_PATH in urlparse(self._session.page.url).path
        ):
            redirected = True
            logger.debug("Campaign Manager redirected to setup; routing in-app")
            await self._route_to(tab_path)

        def done(response: CmResponse) -> bool:
            return ready(response) or (
                refused is not None and refused(response) is not None
            )

        try:
            response = await capture.wait_for(
                done,
                timeout=_READY_TIMEOUT,
                throttle=throttle and not redirected,
                wanted=wanted,
            )
        except LinkedInScraperException:
            if not redirected:
                raise
            response = None
        if response is not None and refused is not None:
            field = refused(response)
            if field is not None:
                raise InvalidReferenceError(
                    f"Campaign Manager refused that {field} (HTTP "
                    f"{response.status}). Check the {field}: pass an id exactly "
                    "as list_ad_accounts or list_campaigns returned it, for an "
                    "account this member can open."
                )
        await capture.settle()
        return response

    @staticmethod
    async def _optional(
        capture: CampaignManagerCapture,
        predicate: Callable[[CmResponse], bool],
        *,
        timeout: float | None = None,
        wanted: tuple[str, ...] = (),
    ) -> CmResponse | None:
        try:
            return await capture.wait_for(
                predicate,
                timeout=_OPTIONAL_WAIT if timeout is None else timeout,
                throttle=False,
                wanted=wanted,
            )
        except LinkedInScraperException:
            return None

    @staticmethod
    def _account_record(
        capture: CampaignManagerCapture, account_id: str
    ) -> AdAccount | None:
        for response in capture.matching(
            lambda r: (
                _resource(r, "campaignManagerAccounts")
                and r.call.entity_id == account_id
            )
        ):
            record = envelope_record(response.payload)
            # Sub-resources share the path prefix; only the account names itself.
            if record is not None and same_id(record.get("id"), account_id):
                return build_ad_account(record)
        return None

    async def list_ad_accounts(self) -> dict[str, Any]:
        """Every ad account the member can open, including a personal boost account."""
        url = CAMPAIGN_MANAGER_ACCOUNTS_URL
        section_errors: dict[str, dict[str, str]] = {}

        def is_boost(response: CmResponse) -> bool:
            return _resource(
                response, "campaignManagerAccounts", finder="memberBoostAccounts"
            )

        async with self._reading() as capture:
            listed = await self._open(
                capture,
                url,
                tab_path=None,
                ready=lambda r: _resource(
                    r, "campaignManagerReportingAccounts", finder="findAll"
                ),
                wanted=("campaignManagerReportingAccounts",),
            )
            boost = await self._optional(
                capture,
                is_boost,
                timeout=_BOOST_ON_LIST_WAIT,
                wanted=("campaignManagerAccounts",),
            )
            if boost is None:
                await self._route_to(_BOOST_ACCOUNTS_PATH)
                boost = await self._optional(
                    capture, is_boost, wanted=("campaignManagerAccounts",)
                )
                await capture.settle()

        assert listed is not None
        elements = envelope_elements(listed.payload)
        if elements is None:
            raise _unverified("the ad account list")
        accounts: list[AdAccount] = []
        for element in elements:
            account = build_ad_account(element)
            if account is None or not isinstance(element.get("name"), str):
                raise _unverified("an ad account")
            accounts.append(account)
        total = envelope_total(listed.payload)
        if total is not None and total > len(elements):
            section_errors["ad_accounts"] = _page_limit(
                "ad accounts", total, len(elements)
            )

        if boost is None:
            section_errors["boost_accounts"] = _error(
                "unavailable",
                "Campaign Manager did not load the personal boost account, so "
                "boosted posts' ad account is not listed.",
            )
        else:
            boost_elements = envelope_elements(boost.payload)
            if boost_elements is None:
                raise _unverified("the boost account list")
            known = {account["id"] for account in accounts}
            for element in boost_elements:
                account = build_boost_account(element)
                if account is None:
                    raise _unverified("a boost account")
                if account["id"] not in known:
                    accounts.append(account)

        result: dict[str, Any] = {"url": url, "sections": {}, "ad_accounts": accounts}
        if text := render_accounts(accounts):
            result["sections"]["ad_accounts"] = text
        if section_errors:
            result["section_errors"] = section_errors
        return result

    async def list_campaigns(self, account_id: str) -> dict[str, Any]:
        """Campaign groups (UI "Campaigns") and campaigns (UI "Ad sets") of an account."""
        account = normalize_ad_account_id(account_id)
        url = campaign_manager_url(account, "/campaigns")
        section_errors: dict[str, dict[str, str]] = {}

        def is_groups(response: CmResponse) -> bool:
            return _resource(
                response, "campaignManagerReportingCampaignGroups", finder="accountId"
            ) and _for_account(response, account)

        def is_campaigns(response: CmResponse) -> bool:
            return (
                _resource(
                    response,
                    "campaignManagerReportingCampaigns",
                    finder="accountAndCampaignGroups",
                )
                and _for_account(response, account)
                # The account list asks for a count only.
                and response.call.params.get("count") != "0"
            )

        async with self._reading() as capture:
            listed = await self._open(
                capture,
                url,
                tab_path=urlparse(url).path,
                ready=is_campaigns,
                refused=_refusal(account),
                wanted=(
                    "campaignManagerReportingCampaigns",
                    "campaignManagerReportingCampaignGroups",
                ),
            )
            if not capture.matching(is_groups):
                await self._optional(
                    capture,
                    is_groups,
                    wanted=("campaignManagerReportingCampaignGroups",),
                )
                await capture.settle()

        account_record = self._account_record(capture, account)
        groups: list[CampaignGroup] = []
        group_responses = capture.matching(is_groups)
        groups_complete = False
        if group_responses:
            merged = _merged_elements(group_responses)
            if merged is None:
                raise _unverified("the campaign group list")
            elements, total = merged
            for element in elements:
                group = build_campaign_group(element)
                if group is None:
                    raise _unverified("a campaign group")
                groups.append(group)
            groups_complete = total is None or total <= len(elements)
            if not groups_complete and total is not None:
                section_errors["campaign_groups"] = _page_limit(
                    "campaign groups", total, len(elements)
                )
        else:
            section_errors["campaign_groups"] = _error(
                "unavailable", "Campaign Manager did not load the campaign groups."
            )

        campaigns = []
        if listed is None:
            section_errors["campaigns"] = _redirect_error("ad set list")
        else:
            merged = _merged_elements(capture.matching(is_campaigns))
            if merged is None:
                raise _unverified("the campaign list")
            elements, total = merged
            group_ids = {group["id"] for group in groups}
            for element in elements:
                campaign = build_campaign(element)
                if campaign is None:
                    raise _unverified("a campaign")
                if campaign.get("accountId", int(account)) != int(account):
                    raise _unverified("a campaign of another ad account")
                group_id = campaign.get("campaignGroupId")
                if (
                    groups_complete
                    and group_id is not None
                    and group_id not in group_ids
                ):
                    raise _unverified("a campaign outside the account's groups")
                campaigns.append(campaign)
            if total is not None and total > len(elements):
                section_errors["campaigns"] = _page_limit(
                    "campaigns", total, len(elements)
                )

        result: dict[str, Any] = {"url": url, "sections": {}}
        if text := render_campaigns(groups, campaigns):
            result["sections"]["campaigns"] = text
        if account_record is not None:
            result["ad_accounts"] = [account_record]
        result["campaign_groups"] = groups
        if listed is not None:
            result["campaigns"] = campaigns
        if section_errors:
            result["section_errors"] = section_errors
        return result

    async def get_campaign(self, account_id: str, campaign_id: str) -> dict[str, Any]:
        """One campaign (UI "Ad set") with its targeting, group and creatives."""
        account = normalize_ad_account_id(account_id)
        campaign_ref = normalize_campaign_id(campaign_id)
        url = campaign_manager_url(account, f"/campaigns/{campaign_ref}/details")
        section_errors: dict[str, dict[str, str]] = {}

        def is_campaign(response: CmResponse) -> bool:
            record = envelope_record(response.payload)
            return (
                _resource(response, "campaignManagerCampaigns")
                and response.call.entity_id == campaign_ref
                and record is not None
                and same_id(record.get("id"), campaign_ref)
            )

        def is_creatives(response: CmResponse) -> bool:
            return _resource(
                response, "campaignManagerCreatives", finder="campaignIds"
            ) and restli_list(response.call.params.get("campaignIds", "")) == [
                campaign_ref
            ]

        async with self._reading() as capture:
            found = await self._open(
                capture,
                url,
                tab_path=None,
                ready=is_campaign,
                refused=_refusal(account, campaign_ref),
                wanted=("campaignManagerCampaigns",),
            )
            assert found is not None
            record = envelope_record(found.payload) or {}
            if not isinstance(record.get("accountId"), int):
                raise _unverified("the campaign")
            if record.get("accountId") != int(account):
                raise InvalidReferenceError(
                    "That campaign does not belong to that ad account. Pass the "
                    "ad_account_id the campaign was listed under."
                )
            group_id = record.get("campaignGroupId")
            group_response = None
            if isinstance(group_id, int):
                group_response = await self._optional(
                    capture,
                    lambda r: (
                        _resource(r, "campaignManagerCampaignGroups")
                        and r.call.entity_id == str(group_id)
                    ),
                    wanted=("campaignManagerCampaignGroups",),
                )
            creatives_response = await self._optional(
                capture, is_creatives, wanted=("campaignManagerCreatives",)
            )

        campaign = build_campaign(record)
        if campaign is None:
            raise _unverified("the campaign")

        group: CampaignGroup | None = None
        group_record = (
            envelope_record(group_response.payload)
            if group_response is not None
            else None
        )
        if (
            group_record is not None
            and same_id(group_record.get("id"), str(group_id))
            and same_id(group_record.get("accountId", int(account)), account)
        ):
            group = build_campaign_group(group_record)
        if group is None:
            section_errors["campaign_groups"] = _error(
                "unavailable", "Campaign Manager did not load the campaign's group."
            )

        creatives: list[Creative] | None = None
        elements = (
            envelope_elements(creatives_response.payload)
            if creatives_response is not None
            else None
        )
        if elements is not None:
            built = [build_creative(element) for element in elements]
            if all(
                item is not None
                and item.get("campaignId", int(campaign_ref)) == int(campaign_ref)
                for item in built
            ):
                creatives = [item for item in built if item is not None]
        if creatives is None:
            section_errors["creatives"] = _error(
                "unavailable",
                "Campaign Manager did not load this campaign's creatives in a "
                "recognised shape.",
            )

        result: dict[str, Any] = {
            "url": url,
            "sections": {"campaign": render_campaign(campaign, group, creatives or [])},
            "campaign": campaign,
        }
        if group is not None:
            result["campaign_groups"] = [group]
        if creatives is not None:
            result["creatives"] = creatives
        if section_errors:
            result["section_errors"] = section_errors
        return result

    async def get_campaign_analytics(
        self,
        account_id: str,
        *,
        level: str = "campaign",
        start_date: date | None = None,
        end_date: date | None = None,
        demographics: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Performance per campaign group, campaign or creative, plus demographics."""
        account = normalize_ad_account_id(account_id)
        requested = campaign_manager_date_range(start_date, end_date)
        pivot = LEVEL_PIVOTS.get(level)
        if pivot is None:
            raise InvalidReferenceError(
                f"level must be one of {', '.join(LEVEL_PIVOTS)}."
            )
        wanted = list(dict.fromkeys(demographics or []))
        unknown = [item for item in wanted if item not in DEMOGRAPHIC_PIVOTS]
        if unknown:
            raise InvalidReferenceError(
                f"Unknown demographics {', '.join(unknown)}; use any of "
                f"{', '.join(sorted(DEMOGRAPHIC_PIVOTS))}."
            )
        url = campaign_manager_url(account, _LEVEL_TABS[level])
        section_errors: dict[str, dict[str, str]] = {}
        demographic_rows: dict[str, list[AnalyticsRow]] = {}
        partial_demographics: set[str] = set()
        uncovered: dict[tuple[str, int], dict[str, Any]] = {}

        async with self._reading() as capture:

            def echoes(call: CmCall) -> bool:
                return _echoes_account(capture, call, account)

            def is_level(response: CmResponse) -> bool:
                return (
                    response.ok
                    and analytics_call_matches(
                        response.call, q="statistics", pivot=pivot
                    )
                    and echoes(response.call)
                )

            def is_boost(response: CmResponse) -> bool:
                record = envelope_record(response.payload)
                return (
                    _resource(response, "campaignManagerAccounts")
                    and response.call.entity_id == account
                    and record is not None
                    and same_id(record.get("id"), account)
                    and record.get("type") == _BOOST_ACCOUNT_TYPE
                )

            try:
                found = await self._open(
                    capture,
                    url,
                    tab_path=urlparse(url).path,
                    ready=lambda r: is_level(r) or is_boost(r),
                    refused=_refusal(account),
                    wanted=("campaignManagerAdAnalytics",),
                )
            except LinkedInScraperException as exc:
                # Only the plain timeout: a refused id or a throttle stays as is.
                unmatched = _unmatched_analytics(capture, pivot)
                if type(exc) is not LinkedInScraperException or unmatched is None:
                    raise
                raise unmatched from exc
            boost = (
                found is not None and is_boost(found) and not capture.matching(is_level)
            )
            if boost:
                # The accounts pages never load a boost account's reporting.
                url = f"{_LINKEDIN}{_BOOST_ACCOUNTS_PATH}"
                await self._route_to(_BOOST_ACCOUNTS_PATH)
                found = await self._optional(
                    capture,
                    is_level,
                    timeout=_READY_TIMEOUT,
                    wanted=("campaignManagerAdAnalytics",),
                )
                await capture.settle()
                if found is None:
                    detail = _unmatched_detail(capture, pivot)
                    raise LinkedInScraperException(
                        "This ad account is a personal boost account, the one "
                        "LinkedIn keeps for boosted posts. Campaign Manager's boost "
                        "manager did not load its reporting analytics"
                        + (f" ({detail})" if detail else "")
                        + ", so nothing was reported."
                    )
            if found is None:
                unmatched = _unmatched_analytics(capture, pivot)
                if unmatched is not None:
                    raise unmatched
                return {
                    "url": url,
                    "sections": {},
                    "section_errors": {"analytics": _redirect_error("reporting table")},
                }
            payload, window, level_error, gaps = await _level_payload(
                capture, capture.matching(is_level), requested, pivot, account
            )
            if payload is None:
                assert level_error is not None
                return {
                    "url": url,
                    "sections": {},
                    "section_errors": {
                        "analytics": level_error,
                        **{f"demographics_{facet}": level_error for facet in wanted},
                    },
                }
            if level_error is not None:
                section_errors["analytics"] = level_error
            for entity in _uncovered(gaps):
                uncovered.setdefault((entity["kind"], entity["id"]), entity)
            if wanted:
                demographics_url = campaign_manager_url(account, "/demographics")

                def is_demographic(response: CmResponse) -> bool:
                    return (
                        response.ok
                        and analytics_call_matches(
                            response.call, q="analytics", pivot=None
                        )
                        and echoes(response.call)
                    )

                failure: dict[str, str] | None = None
                if boost:
                    failure = _error(
                        "unavailable",
                        "Campaign Manager has no demographics page for a personal "
                        "boost account, so its demographics were not read.",
                    )
                else:
                    try:
                        # Optional page: a miss is no throttle evidence.
                        opened = await self._open(
                            capture,
                            demographics_url,
                            tab_path=urlparse(demographics_url).path,
                            ready=is_demographic,
                            throttle=False,
                            wanted=("campaignManagerAdAnalytics",),
                        )
                    except LinkedInScraperException as exc:
                        failure = _error("unavailable", str(exc))
                    else:
                        if opened is None:
                            failure = _redirect_error("demographics")
                for facet in wanted:
                    name = f"demographics_{facet}"
                    if failure is not None:
                        section_errors[name] = failure
                        continue
                    try:
                        rows, facet_gaps = await _demographic_rows(
                            capture, facet, window, account, is_demographic
                        )
                    except LinkedInScraperException as exc:
                        section_errors[name] = _error("unavailable", str(exc))
                        continue
                    demographic_rows[facet] = rows
                    facet_error = _gap_error(facet_gaps, "demographics")
                    if facet_error is not None:
                        section_errors[name] = facet_error
                        partial_demographics.add(facet)
                    for entity in _uncovered(facet_gaps):
                        uncovered.setdefault((entity["kind"], entity["id"]), entity)

        rows = build_analytics_rows(payload, pivot)
        if rows is None:
            raise _unverified("the analytics")
        account_record = self._account_record(capture, account)
        currency = account_record.get("currencyCode") if account_record else None
        date_window: DateWindow = {
            "start": window[0].isoformat(),
            "end": window[1].isoformat(),
            "source": "requested" if requested else "page",
        }
        analytics: dict[str, Any] = {
            "level": level,
            "window": date_window,
            "currencyCode": currency,
            "rows": rows,
        }
        analytics_text = render_analytics(rows, date_window, currency)
        if section_errors.get("analytics", {}).get("error_type") == "page_limit":
            analytics_text = _PARTIAL + analytics_text
        sections = {"analytics": analytics_text}
        if demographic_rows:
            analytics["demographics"] = demographic_rows
            for facet, items in demographic_rows.items():
                text = render_analytics(items, date_window, currency)
                if facet in partial_demographics:
                    text = _PARTIAL + text
                sections[f"demographics_{facet}"] = text
        result: dict[str, Any] = {
            "url": url,
            "sections": sections,
            "analytics": analytics,
        }
        if uncovered:
            result["uncovered"] = list(uncovered.values())
        if section_errors:
            result["section_errors"] = section_errors
        return result


def _unmatched_analytics(
    capture: CampaignManagerCapture, pivot: str
) -> LinkedInScraperException | None:
    """The failure for a reporting table whose analytics query was not the one read.

    None when the page sent no ``q=statistics`` query at all, which is what a
    setup page that never shows the table looks like.
    """
    detail = _unmatched_detail(capture, pivot)
    if detail is None:
        return None
    return LinkedInScraperException(
        f"Campaign Manager loaded analytics for this ad account's reporting "
        f"table, but {detail}, so nothing was reported."
    )


def _unmatched_detail(capture: CampaignManagerCapture, pivot: str) -> str | None:
    seen = capture.matching(
        lambda r: (
            r.call.resource == "campaignManagerAdAnalytics"
            and r.call.finder == "statistics"
        )
    )
    if not seen:
        return None
    if all(not r.ok for r in seen):
        statuses = ", ".join(sorted({str(r.status) for r in seen}))
        return f"LinkedIn refused every one (HTTP {statuses})"
    return (
        "none was the query this tool reads: by "
        f"{pivot} (alone or with objective type), the whole period with "
        "metrics, for this ad account or ids within it"
    )


class _Listed(NamedTuple):
    """The entities a reporting list showed by id, its largest total, and whether it was filtered."""

    entities: dict[str, dict[str, Any]]
    total: int | None
    filtered: bool


def _status_filtered(call: CmCall) -> bool:
    """Whether a reporting list was narrowed by status, so it is not every entity.

    The page sends none today; a projection naming the status field is no filter.
    """
    return any(
        "status" in f"{key}={value}".casefold()
        for key, value in call.params.items()
        if key != "fields"
    )


def _reporting_lists(
    capture: CampaignManagerCapture, account_id: str
) -> dict[str, _Listed]:
    """Per kind, the entities the page's reporting lists showed by id, and the largest total."""
    lists: dict[str, _Listed] = {
        kind: _Listed({}, None, False) for kind in _REPORTING_LISTS.values()
    }
    for response in capture.matching(
        lambda r: (
            r.ok and r.call.resource in _REPORTING_LISTS and _for_account(r, account_id)
        )
    ):
        kind = _REPORTING_LISTS[response.call.resource]
        listed, total, filtered = lists[kind]
        for element in envelope_elements(response.payload) or []:
            if isinstance(element.get("id"), int):
                listed.setdefault(str(element["id"]), _listed(kind, element))
        reported = envelope_total(response.payload)
        if reported is not None and (total is None or reported > total):
            total = reported
        lists[kind] = _Listed(
            listed, total, filtered or _status_filtered(response.call)
        )
    return lists


def _listed(kind: str, element: dict[str, Any]) -> dict[str, Any]:
    entity: dict[str, Any] = {"kind": kind, "id": element["id"]}
    if isinstance(element.get("name"), str):
        entity["name"] = element["name"]
    status = element.get("status")
    if not isinstance(status, str):
        v2 = element.get("statusV2")
        status = v2.get("status") if isinstance(v2, dict) else None
    if isinstance(status, str):
        entity["status"] = status
    return entity


def _echoes_account(
    capture: CampaignManagerCapture, call: CmCall, account_id: str
) -> bool:
    """Whether an analytics query is scoped to this account and nothing else."""
    if call.params.get("accountIds") is not None:
        return _account_wide(call, account_id)
    scoped = [key for key in SCOPE_ID_PARAMS if key in call.params]
    if not scoped:
        return False
    lists = _reporting_lists(capture, account_id)
    return all(
        (ids := restli_list(call.params[key]))
        and set(ids) <= lists[SCOPE_ID_PARAMS[key]].entities.keys()
        for key in scoped
    )


def _scoped_ids(call: CmCall) -> dict[str, set[str]]:
    return {
        kind: set(restli_list(call.params[key]))
        for key, kind in SCOPE_ID_PARAMS.items()
        if key in call.params
    }


class _Gap(NamedTuple):
    """Entities that can have delivery which a query's ids leave out."""

    kind: str
    required: int
    missing: list[dict[str, Any]]
    unlisted: int
    # The reporting list was filtered by status, so coverage is unknown.
    filtered: bool = False


def _gaps(
    capture: CampaignManagerCapture, scoped: dict[str, set[str]], account_id: str
) -> list[_Gap]:
    """Per scoped kind, what the ids asked for leave out; empty when nothing is.

    A draft never ran, so only the other statuses must be covered. Entities the
    reporting list counted but did not show have no id or status to check.
    """
    lists = _reporting_lists(capture, account_id)
    gaps = []
    for kind, ids in scoped.items():
        listed, total, filtered = lists[kind]
        unlisted = max(0, (total or 0) - len(listed))
        required = [
            entity
            for entity in listed.values()
            if entity.get("status") != _NEVER_DELIVERED
        ]
        missing = [entity for entity in required if str(entity["id"]) not in ids]
        if missing or unlisted or filtered:
            gaps.append(
                _Gap(kind, len(required) + unlisted, missing, unlisted, filtered)
            )
    return gaps


def _full(capture: CampaignManagerCapture, call: CmCall, account_id: str) -> bool:
    """Whether a query reads every entity that can have delivery, so a replay is no subset."""
    if call.params.get("accountIds") is not None:
        return _account_wide(call, account_id)
    scoped = _scoped_ids(call)
    return bool(scoped) and not _gaps(capture, scoped, account_id)


def _gap_error(gaps: list[_Gap], what: str) -> dict[str, str] | None:
    """The page_limit naming what the reporting table's ids left out."""
    if not gaps:
        return None
    parts = []
    for gap in gaps:
        name = f"{gap.kind.replace('_', ' ')}s"
        if gap.filtered:
            parts.append(
                f"its list of {name} was filtered by status, so whether every one "
                "that can have delivery was covered is unknown"
            )
            continue
        left = len(gap.missing) + gap.unlisted
        part = (
            f"{left} of its {gap.required} {name} that can have delivery "
            f"{'was' if left == 1 else 'were'} not covered"
        )
        if gap.missing:
            part += ": " + ", ".join(str(entity["id"]) for entity in gap.missing)
        if gap.unlisted:
            part += (
                f" ({gap.unlisted} not shown in Campaign Manager's list, so their "
                "ids are unknown)"
            )
        parts.append(part)
    return _error(
        "page_limit",
        f"Campaign Manager's reporting table asked LinkedIn for {what} of only "
        f"part of this ad account: {'; '.join(parts)}. Drafts never run, so "
        "they are left out. The missing ones are listed in uncovered, so these "
        "figures are not the ad account's totals.",
    )


def _uncovered(gaps: list[_Gap]) -> list[dict[str, Any]]:
    return [entity for gap in gaps for entity in gap.missing]


def _range_of(call: CmCall) -> tuple[date, date] | None:
    return restli_date_range(call.params.get("dateRange", ""))


def _shape(response: CmResponse) -> tuple[str, ...]:
    return tuple(restli_list(response.call.params.get("pivots", "")))


def _reported(responses: list[CmResponse]) -> set[str]:
    """The reported metrics a set of replies projects or carries."""
    names: set[str] = set()
    for response in responses:
        names.update(_FIELD_NAME.findall(response.call.params.get("fields", "")))
        for element in envelope_elements(response.payload) or []:
            for value in element.values():
                if isinstance(value, dict):
                    names.update(value)
    return names & _REPORTED_METRICS


def _merged_level(
    capture: CampaignManagerCapture, responses: list[CmResponse], account_id: str
) -> tuple[Any, list[_Gap]]:
    """One period's reporting replies as one payload, and what they leave out.

    The page can ask by the level's pivot alone and with objective type for the
    same period; mixing them would count an entity twice, so only one shape is
    read: the one carrying the most reported metrics, then the fewer pivots.
    An account-wide reply holds every entity. Otherwise the page asked for
    the ids its status filter shows, in chunks, so the rows merge by their
    pivot URNs (one element per entity and objective type) and the ids asked
    for are checked against the reporting list. Replies projecting different metrics
    for one row add up to the union of those metrics.
    """
    shapes: dict[tuple[str, ...], list[CmResponse]] = {}
    for response in responses:
        shapes.setdefault(_shape(response), []).append(response)
    chosen = shapes[min(shapes, key=lambda s: (-len(_reported(shapes[s])), len(s), s))]
    rows: dict[tuple[str, ...], dict[str, Any]] = {}
    scoped: dict[str, set[str]] = {}
    account_wide = False
    for response in chosen:
        account_wide = account_wide or _account_wide(response.call, account_id)
        elements = envelope_elements(response.payload)
        if elements is None:
            raise _unverified("the analytics")
        for element in elements:
            values = element.get("pivotValues")
            urns = tuple(
                value.get("id") if isinstance(value, dict) else None
                for value in (values if isinstance(values, list) else [])
            )
            if not urns or not all(isinstance(urn, str) for urn in urns):
                raise _unverified("the analytics")
            row = rows.setdefault(cast("tuple[str, ...]", urns), {})
            for name, value in element.items():
                if isinstance(value, dict) and isinstance(row.get(name), dict):
                    row[name] = {**value, **row[name]}
                else:
                    row.setdefault(name, value)
        for key, kind in SCOPE_ID_PARAMS.items():
            if key in response.call.params:
                scoped.setdefault(kind, set()).update(
                    restli_list(response.call.params[key])
                )
    payload = {"elements": list(rows.values())}
    if account_wide:
        return payload, []
    return payload, _gaps(capture, scoped, account_id)


async def _level_payload(
    capture: CampaignManagerCapture,
    passive: list[CmResponse],
    requested: tuple[date, date] | None,
    pivot: str,
    account_id: str,
) -> tuple[Any, tuple[date, date], dict[str, str] | None, list[_Gap]]:
    """The reporting table's rows for the period wanted, replaying if needed.

    The payload is None, with the error, when the period cannot be read.
    A query whose ids leave entities out is still read, with its gaps.
    """
    if requested is None:
        latest = max(passive, key=lambda r: period_order(r.call.params))
        window = _range_of(latest.call)
        assert window is not None
    else:
        window = requested
    same = [response for response in passive if _range_of(response.call) == window]
    if same:
        payload, gaps = _merged_level(capture, same, account_id)
        return payload, window, _gap_error(gaps, "analytics"), gaps
    template, gaps = _template(capture, "statistics", pivot, account_id)
    if template is None:
        return (
            None,
            window,
            _error(
                "unavailable",
                "Campaign Manager did not expose a reporting query for this ad "
                "account, so the requested period could not be read.",
            ),
            [],
        )
    payload = await capture.replay_analytics(template, date_range=window, pivot=None)
    return payload, window, _gap_error(gaps, "analytics"), gaps


def _template(
    capture: CampaignManagerCapture, q: str, pivot: str | None, account_id: str
) -> tuple[Any, list[_Gap]]:
    """The page's query to replay, one covering the account first, and its gaps."""
    template = capture.analytics_template(
        q=q, pivot=pivot, accept=lambda call: _full(capture, call, account_id)
    )
    if template is not None:
        return template, []
    template = capture.analytics_template(
        q=q,
        pivot=pivot,
        accept=lambda call: _echoes_account(capture, call, account_id),
    )
    if template is None:
        return None, []
    return template, _gaps(capture, _scoped_ids(template_call(template)), account_id)


async def _demographic_rows(
    capture: CampaignManagerCapture,
    facet: str,
    window: tuple[date, date],
    account_id: str,
    is_demographic: Callable[[CmResponse], bool],
) -> tuple[list[AnalyticsRow], list[_Gap]]:
    """One facet's rows for the window, and what the query's ids left out."""
    seen = [
        response
        for response in capture.matching(is_demographic)
        if response.call.params.get("pivot") == facet
        and _range_of(response.call) == window
    ]
    full = [r for r in seen if _full(capture, r.call, account_id)]
    if full or seen:
        response = (full or seen)[0]
        payload = response.payload
        gaps = [] if full else _gaps(capture, _scoped_ids(response.call), account_id)
    else:
        template, gaps = _template(capture, "analytics", None, account_id)
        if template is None:
            raise LinkedInScraperException(
                "Campaign Manager did not expose a demographics query for this "
                "ad account."
            )
        payload = await capture.replay_analytics(
            template, date_range=window, pivot=facet
        )
    rows = build_analytics_rows(payload, facet)
    if rows is None:
        raise LinkedInScraperException(
            f"Campaign Manager returned {facet} demographics in an unrecognised shape."
        )
    return rows, gaps
