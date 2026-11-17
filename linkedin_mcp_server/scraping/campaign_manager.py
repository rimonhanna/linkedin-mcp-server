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
from typing import Any
from urllib.parse import urlparse

import logging

from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
    LinkedInScraperException,
)
from linkedin_mcp_server.scraping.campaign_manager_api import (
    DEMOGRAPHIC_PIVOTS,
    LEVEL_PIVOTS,
    CampaignManagerCapture,
    CmResponse,
    analytics_call_matches,
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
    build_ad_account,
    build_analytics_rows,
    build_boost_account,
    build_campaign,
    build_campaign_group,
    build_creative,
    merge_by_id,
    normalized_elements,
    normalized_record,
    plain_elements,
    render_accounts,
    render_analytics,
    render_campaign,
    render_campaigns,
    restli_date_range,
    restli_list,
)
from linkedin_mcp_server.scraping.identifiers import (
    CAMPAIGN_MANAGER_ACCOUNTS_URL,
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
_READY_TIMEOUT = 12.0
# For a response the page may not send; a miss is no throttle evidence.
_OPTIONAL_WAIT = 5.0
_BOOST_ON_LIST_WAIT = 3.0
_LEVEL_TABS = {
    "campaign_group": "/campaign-groups",
    "campaign": "/campaigns",
    "creative": "/creatives",
}
_ID_PARAMS = {
    "campaignGroupIds": "campaign_group",
    "campaignIds": "campaign",
    "creativeIds": "creative",
}
# Our bound on a requested period, not LinkedIn's.
_MAX_RANGE_DAYS = 366


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


def _total(payload: Any) -> int | None:
    paging = payload.get("paging") if isinstance(payload, dict) else None
    total = paging.get("total") if isinstance(paging, dict) else None
    return total if isinstance(total, int) and not isinstance(total, bool) else None


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
        items = plain_elements(response.payload)
        if items is None:
            return None
        elements.extend(items)
        reported = _total(response.payload)
        if reported is not None and (total is None or reported > total):
            total = reported
    return merge_by_id(elements), total


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
    ) -> CmResponse | None:
        """Load ``url`` and wait for its data; None if it stayed on setup.

        A dormant account's list page redirects to campaign setup on a full
        load (detected by URL path, in any UI language). One in-app route hop
        to ``tab_path`` reaches the list; a hop that still yields nothing
        returns None rather than raising.
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
        try:
            response = await capture.wait_for(
                ready, timeout=_READY_TIMEOUT, throttle=not redirected
            )
        except LinkedInScraperException:
            if not redirected:
                raise
            response = None
        await capture.settle()
        return response

    @staticmethod
    async def _optional(
        capture: CampaignManagerCapture,
        predicate: Callable[[CmResponse], bool],
        *,
        timeout: float | None = None,
    ) -> CmResponse | None:
        try:
            return await capture.wait_for(
                predicate,
                timeout=_OPTIONAL_WAIT if timeout is None else timeout,
                throttle=False,
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
            record = normalized_record(response.payload)
            # Sub-resources share the path prefix; only the account names itself.
            if record is not None and record.get("id") == int(account_id):
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
            )
            boost = await self._optional(capture, is_boost, timeout=_BOOST_ON_LIST_WAIT)
            if boost is None:
                await self._route_to(_BOOST_ACCOUNTS_PATH)
                boost = await self._optional(capture, is_boost)
                await capture.settle()

        assert listed is not None
        elements = plain_elements(listed.payload)
        if elements is None:
            raise _unverified("the ad account list")
        accounts: list[AdAccount] = []
        for element in elements:
            account = build_ad_account(element)
            if account is None or not isinstance(element.get("name"), str):
                raise _unverified("an ad account")
            accounts.append(account)
        total = _total(listed.payload)
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
            boost_elements = normalized_elements(boost.payload)
            if boost_elements is None:
                boost_elements = plain_elements(boost.payload)
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
                capture, url, tab_path=urlparse(url).path, ready=is_campaigns
            )
            if not capture.matching(is_groups):
                await self._optional(capture, is_groups)
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
            record = normalized_record(response.payload)
            return (
                _resource(response, "campaignManagerCampaigns")
                and response.call.entity_id == campaign_ref
                and record is not None
                and record.get("id") == int(campaign_ref)
            )

        def is_creatives(response: CmResponse) -> bool:
            return _resource(
                response, "campaignManagerCreatives", finder="campaignIds"
            ) and restli_list(response.call.params.get("campaignIds", "")) == [
                campaign_ref
            ]

        async with self._reading() as capture:
            found = await self._open(capture, url, tab_path=None, ready=is_campaign)
            assert found is not None
            record = normalized_record(found.payload) or {}
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
                )
            creatives_response = await self._optional(capture, is_creatives)

        campaign = build_campaign(record)
        if campaign is None:
            raise _unverified("the campaign")

        group: CampaignGroup | None = None
        payload = group_response.payload if group_response is not None else None
        if (
            isinstance(payload, dict)
            and payload.get("id") == group_id
            and payload.get("accountId", int(account)) == int(account)
        ):
            group = build_campaign_group(payload)
        if group is None:
            section_errors["campaign_groups"] = _error(
                "unavailable", "Campaign Manager did not load the campaign's group."
            )

        creatives: list[Creative] | None = None
        elements = (
            normalized_elements(creatives_response.payload)
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
        requested = _requested_range(start_date, end_date)
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

            found = await self._open(
                capture, url, tab_path=urlparse(url).path, ready=is_level
            )
            if found is None:
                return {
                    "url": url,
                    "sections": {},
                    "section_errors": {"analytics": _redirect_error("reporting table")},
                }
            payload, window = await _level_payload(
                capture, capture.matching(is_level), requested, pivot, echoes
            )
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

                opened = await self._open(
                    capture,
                    demographics_url,
                    tab_path=urlparse(demographics_url).path,
                    ready=is_demographic,
                )
                for facet in wanted:
                    name = f"demographics_{facet}"
                    if opened is None:
                        section_errors[name] = _redirect_error("demographics")
                        continue
                    try:
                        rows = await _demographic_rows(
                            capture, facet, window, echoes, is_demographic
                        )
                    except LinkedInScraperException as exc:
                        section_errors[name] = _error("unavailable", str(exc))
                        continue
                    demographic_rows[facet] = rows

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
        sections = {"analytics": render_analytics(rows, date_window, currency)}
        if demographic_rows:
            analytics["demographics"] = demographic_rows
            for facet, items in demographic_rows.items():
                sections[f"demographics_{facet}"] = render_analytics(
                    items, date_window, currency
                )
        result: dict[str, Any] = {
            "url": url,
            "sections": sections,
            "analytics": analytics,
        }
        if section_errors:
            result["section_errors"] = section_errors
        return result


def _requested_range(
    start_date: date | None, end_date: date | None
) -> tuple[date, date] | None:
    if start_date is None and end_date is None:
        return None
    if start_date is None or end_date is None:
        raise InvalidReferenceError("Pass both start_date and end_date, or neither.")
    if start_date > end_date:
        raise InvalidReferenceError("start_date must not be after end_date.")
    if end_date > date.today():
        raise InvalidReferenceError("end_date must not be in the future.")
    if (end_date - start_date).days + 1 > _MAX_RANGE_DAYS:
        raise InvalidReferenceError(
            f"The date range may span at most {_MAX_RANGE_DAYS} days."
        )
    return start_date, end_date


def _known_ids(capture: CampaignManagerCapture, account_id: str) -> dict[str, set[str]]:
    """Ids of this account's entities, from the reporting lists the page loaded."""
    resources = {
        "campaignManagerReportingCampaignGroups": "campaign_group",
        "campaignManagerReportingCampaigns": "campaign",
        "campaignManagerReportingCreatives": "creative",
    }
    known: dict[str, set[str]] = {kind: set() for kind in resources.values()}
    for response in capture.matching(
        lambda r: r.ok and r.call.resource in resources and _for_account(r, account_id)
    ):
        for element in plain_elements(response.payload) or []:
            if isinstance(element.get("id"), int):
                known[resources[response.call.resource]].add(str(element["id"]))
    return known


def _echoes_account(
    capture: CampaignManagerCapture, call: CmCall, account_id: str
) -> bool:
    """Whether an analytics query is scoped to this account and nothing else."""
    accounts = call.params.get("accountIds")
    if accounts is not None:
        return restli_list(accounts) == [account_id]
    scoped = [key for key in _ID_PARAMS if key in call.params]
    if not scoped:
        return False
    known = _known_ids(capture, account_id)
    return all(
        (ids := restli_list(call.params[key])) and set(ids) <= known[_ID_PARAMS[key]]
        for key in scoped
    )


def _range_of(call: CmCall) -> tuple[date, date] | None:
    return restli_date_range(call.params.get("dateRange", ""))


async def _level_payload(
    capture: CampaignManagerCapture,
    passive: list[CmResponse],
    requested: tuple[date, date] | None,
    pivot: str,
    echoes: Callable[[CmCall], bool],
) -> tuple[Any, tuple[date, date]]:
    """The reporting table's reply for the period wanted, replaying if needed."""
    if requested is None:
        # The UI also fetches the previous period to compare; the latest is shown.
        latest = max(passive, key=lambda r: _range_of(r.call) or (date.min, date.min))
        window = _range_of(latest.call)
        assert window is not None
        return latest.payload, window
    for response in passive:
        if _range_of(response.call) == requested:
            return response.payload, requested
    template = capture.analytics_template(q="statistics", pivot=pivot)
    if template is None or not echoes(template_call(template)):
        raise LinkedInScraperException(
            "Campaign Manager did not expose its reporting query, so the "
            "requested period could not be read."
        )
    payload = await capture.replay_analytics(template, date_range=requested, pivot=None)
    return payload, requested


async def _demographic_rows(
    capture: CampaignManagerCapture,
    facet: str,
    window: tuple[date, date],
    echoes: Callable[[CmCall], bool],
    is_demographic: Callable[[CmResponse], bool],
) -> list[AnalyticsRow]:
    payload: Any = None
    for response in capture.matching(is_demographic):
        if (
            response.call.params.get("pivot") == facet
            and _range_of(response.call) == window
        ):
            payload = response.payload
            break
    else:
        template = capture.analytics_template(q="analytics", pivot=None)
        if template is None or not echoes(template_call(template)):
            raise LinkedInScraperException(
                "Campaign Manager did not expose its demographics query."
            )
        payload = await capture.replay_analytics(
            template, date_range=window, pivot=facet
        )
    rows = build_analytics_rows(payload, facet)
    if rows is None:
        raise LinkedInScraperException(
            f"Campaign Manager returned {facet} demographics in an unrecognised shape."
        )
    return rows
