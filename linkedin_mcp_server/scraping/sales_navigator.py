"""Sales Navigator reads: account and lead search, accounts and account lists.

Every read is a page load of a URL built here, read from the responses Sales
Navigator itself fetches; nothing is replayed or evaluated in the page. Lead
pages are never opened (they register profile views), and a lead who asked
LinkedIn not to share data with third-party tools is left out and counted.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeVar, cast
from urllib.parse import urlparse

from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
    LinkedInScraperException,
)
from linkedin_mcp_server.exceptions import ActionLimitError
from linkedin_mcp_server.scraping.contracts import (
    FilterValidationError,
    limit_exceeded_section_error,
)
from linkedin_mcp_server.scraping.facets import FacetResolver
from linkedin_mcp_server.scraping.identifiers import (
    SALES_ACCOUNT_LISTS_URL,
    SALES_LEAD_LISTS_URL,
    normalize_sales_company_id,
    normalize_sales_list_id,
    sales_account_list_url,
    sales_company_url,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.restli import (
    as_str,
    envelope_elements,
    envelope_record,
    envelope_total,
)
from linkedin_mcp_server.scraping.sales_navigator_api import (
    SalesNavigatorCapture,
    SnResponse,
)
from linkedin_mcp_server.scraping.sales_navigator_lead_filters import (
    ACCOUNT_LIST,
    COMPANY_HEADQUARTERS,
    DEPARTMENT_HEADCOUNT,
    FUNCTION,
    LEAD_LIST,
    RELATIONSHIP,
    YEARS_AT_CURRENT_COMPANY,
    YEARS_IN_CURRENT_POSITION,
    department_headcount as _department_headcount,
    function_ids,
    tenure_ids,
)
from linkedin_mcp_server.scraping.sales_navigator_payload import (
    COMPANY_FILTER_ID,
    COMPANY_HEADCOUNT,
    COMPANY_HEADCOUNT_GROWTH,
    CURRENT_COMPANY,
    CURRENT_TITLE,
    INDUSTRY,
    MAX_PAGE,
    PAGE_SIZE,
    REGION,
    RESULT_CAP,
    SENIORITY_IDS,
    SENIORITY_LEVEL,
    AccountList,
    Lead,
    ListedAccount,
    SearchFilter,
    SearchPaging,
    SeniorityLevel,
    build_account,
    build_account_card,
    build_account_list,
    build_elements,
    build_leads,
    build_listed_account,
    build_paging,
    build_search_query,
    build_withheld,
    dropped_filters,
    missing_echoes,
    render_account,
    render_account_cards,
    render_account_lists,
    render_leads,
    render_listed_accounts,
    search_page_url,
    unrequested_echoes,
)
from linkedin_mcp_server.scraping.search_urls import (
    company_size_letters,
    industry_ids,
    network_tokens,
)
from linkedin_mcp_server.scraping.session import ScrapingSession

_T = TypeVar("_T")
_Row = TypeVar("_Row", AccountList, ListedAccount)

_READY_TIMEOUT = 12.0
# For a response the page may not send; a miss is no throttle evidence.
_OPTIONAL_WAIT = 3.0
_ACCOUNT_SEARCH = "salesApiAccountSearch"
_LEAD_SEARCH = "salesApiLeadSearch"
_COMPANIES = "salesApiCompanies"
_LISTS = "salesApiLists"
_ACCOUNT_TABLE = "salesApiDashboardAccountTable"
# The boot calls that answer 401/403 when the member has no usable seat.
_SEAT_RESOURCES = frozenset({"salesApiAccess", "salesApiEntitlements"})
_SEAT_REFUSED = frozenset({401, 403})
# What LinkedIn answers for an entity the member cannot open or that is gone.
_REFUSED_STATUSES = frozenset({403, 404})
_LIST_PATH = "/sales/accounts/dashboard"
# Search pages one get_lead_list call may load: a fifth of the default
# SEARCH_PAGES_MAX (100 per 24 h), the share enrich_companies takes per call
# (BUNCH_SEARCHES_MAX), so one long list cannot spend the day's search cap.
_LEAD_LIST_PAGES_PER_CALL = 20
_HEADCOUNT_FIELDS = ("employeeCount", "employeeCountRange", "employeeDisplayCount")
_NO_SEAT_ADVICE = (
    "This account may have no Sales Navigator seat, or needs a contract chosen "
    "in the browser."
)


def _error(error_type: str, message: str) -> dict[str, str]:
    return {"error_type": error_type, "error_message": message}


def _unverified(what: str) -> LinkedInScraperException:
    return LinkedInScraperException(
        f"Sales Navigator returned {what} in a shape this tool does not "
        "recognise, so nothing was reported."
    )


def _is(response: SnResponse, resource: str, *, finder: str | None = None) -> bool:
    return (
        response.ok
        and response.call.resource == resource
        and response.call.finder == finder
    )


def _starts_at(response: SnResponse, start: int) -> bool:
    return response.call.params.get("start", "0") == str(start)


def _seat_refused(response: SnResponse) -> bool:
    return (
        response.call.resource in _SEAT_RESOURCES and response.status in _SEAT_REFUSED
    )


def _seat_refusal(responses: Sequence[SnResponse]) -> SnResponse | None:
    """A seat check that answered 401/403 and was never answered OK.

    The first boot of a Sales Navigator session can answer 401 before the app
    mints its session cookie and retries, then the page loads normally.
    """
    for response in responses:
        resource = response.call.resource
        if _seat_refused(response) and not any(
            r.ok and r.call.resource == resource for r in responses
        ):
            return response
    return None


def _on_path(path: str, expected: str) -> bool:
    return path == expected or path.startswith(expected.rstrip("/") + "/")


@dataclass(frozen=True)
class _Refusal:
    """How LinkedIn answers a wrong ``field``: a 403/404 for that entity."""

    field: str
    resources: frozenset[str]
    entity_id: str

    def _names(self, response: SnResponse) -> bool:
        call = response.call
        return call.resource in self.resources and (
            call.entity_id == self.entity_id
            or call.params.get("listId") == self.entity_id
        )

    def refuses(self, response: SnResponse) -> bool:
        return response.status in _REFUSED_STATUSES and self._names(response)

    def stands(self, responses: Sequence[SnResponse]) -> bool:
        """Refused, and no other call for the same entity answered OK.

        One decoration can be refused for a valid id while another loads.
        """
        return any(self.refuses(r) for r in responses) and not any(
            r.ok and self._names(r) for r in responses
        )


def _first_built(
    responses: Sequence[SnResponse], builder: Callable[[Any], _T | None]
) -> tuple[list[_T], int] | None:
    """Rows and ``paging.total`` of the first response whose every row builds."""
    for response in responses:
        rows = build_elements(response.payload, builder)
        total = envelope_total(response.payload)
        if rows is not None and total is not None:
            return rows, total
    return None


def _page_limit(name: str, total: int, read: int, why: str) -> dict[str, str]:
    return _error(
        "page_limit",
        f"Sales Navigator reported {total} {name} and {read} were read; {why}, "
        "so the rest were not read.",
    )


def _read_paging(
    first_page: int, read: int, total: int, next_page: int | None
) -> dict[str, Any]:
    """Where a multi-page read started, how many rows it returned, what remains.

    ``more`` true with ``next_page`` None means the read stopped early (see
    ``section_errors``). Unlike search paging there is no resultCap/capped:
    a list is read on with ``next_page`` past 1,000 rows.
    """
    start = (first_page - 1) * PAGE_SIZE
    return {
        "page": first_page,
        "start": start,
        "count": read,
        "total": total,
        "more": start + read < total,
        "next_page": next_page,
    }


def _criteria(values: Sequence[str] | None) -> list[str]:
    return [value.strip() for value in values or () if value.strip()]


def _field(name: str, included: Sequence[str], excluded: Sequence[str]) -> str:
    """The parameter names behind one filter, as a caller passed them."""
    return "/".join(
        label
        for label, values in ((name, included), (f"exclude_{name}", excluded))
        if values
    )


def _refuse_overlap(name: str, included: Sequence[str], excluded: Sequence[str]):
    if both := [value for value in dict.fromkeys(included) if value in excluded]:
        raise FilterValidationError(
            f"{', '.join(both)} is in both {name} and exclude_{name}. Pass each "
            "value in only one of them."
        )


def _relaxed_note(
    payload: Any, filters: Sequence[tuple[str, SearchFilter]], rows: str, near: int
) -> dict[str, str] | None:
    """A note when Sales Navigator dropped a filter to answer; else None.

    The echo checks already proved the request carried exactly the asked
    filters, so any dropped filter means the rows match a looser search than
    the one asked, and none of them is reported as a match.
    """
    asked = {search_filter.type: field for field, search_filter in filters}
    dropped = list(
        dict.fromkeys(asked.get(kind, kind) for kind in dropped_filters(payload))
    )
    if not dropped:
        return None
    if len(dropped) > 1:
        named = f"{', '.join(dropped[:-1])} and {dropped[-1]} filters"
        those = "those filters"
    else:
        named = f"{dropped[0]} filter"
        those = "that filter"
    return _error(
        "filters_relaxed",
        f"No {rows} match every filter. Sales Navigator dropped the {named} "
        f"to show {near} near matches instead, which are not reported. Widen "
        f"or remove {those} to see them.",
    )


def _relaxed(
    payload: Any,
    filters: Sequence[tuple[str, SearchFilter]],
    paging: SearchPaging,
    rows: str,
) -> tuple[SearchPaging, dict[str, str]] | None:
    """Empty paging and the note when Sales Navigator relaxed the search."""
    note = _relaxed_note(payload, filters, rows, paging["total"])
    if note is None:
        return None
    empty: SearchPaging = {
        **paging,
        "count": 0,
        "total": 0,
        "capped": False,
        "more": False,
    }
    return empty, note


class SalesNavigatorReader:
    """Own every workflow whose subject is a Sales Navigator page."""

    def __init__(
        self,
        session: ScrapingSession,
        navigator: PageNavigator,
        facets: FacetResolver,
    ):
        self._session = session
        self._navigator = navigator
        self._facets = facets

    def _capture(self, *resources: str) -> SalesNavigatorCapture:
        return SalesNavigatorCapture(self._session.page, resources=frozenset(resources))

    async def _open(
        self,
        capture: SalesNavigatorCapture,
        url: str,
        *,
        expected_path: str,
        ready: Callable[[SnResponse], bool],
        refused: _Refusal | None = None,
        required: bool = True,
    ) -> SnResponse | None:
        """Load ``url`` (one charged page load) and wait for its data.

        The seat check is locale-free: the page must stay on ``expected_path``,
        and a data wait that times out is a missing seat only when a boot seat
        check refused (401/403) and never recovered. The data response itself
        proves a seat. With ``required=False`` any other data wait that times
        out returns None instead of raising.
        """
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        await self._session.dismiss_modal()
        landed = urlparse(self._session.page.url).path
        if not _on_path(landed, expected_path):
            # The path only: a search query names what the member looked for.
            raise LinkedInScraperException(
                f"Sales Navigator did not open (LinkedIn showed {landed}). "
                + _NO_SEAT_ADVICE
            )

        def answered(response: SnResponse) -> bool:
            return ready(response) or (
                refused is not None and refused.refuses(response)
            )

        def done(response: SnResponse) -> bool:
            return answered(response) or _seat_refused(response)

        try:
            response = await capture.wait_for(done, timeout=_READY_TIMEOUT)
            if _seat_refused(response):
                # A refusal the app recovers from ends with the data; a real
                # one is no evidence of a throttle.
                response = await capture.wait_for(
                    answered, timeout=_READY_TIMEOUT, throttle=False
                )
            if refused is not None and refused.refuses(response):
                await capture.settle()
                if not refused.stands(capture.responses):
                    response = await capture.wait_for(ready, timeout=_READY_TIMEOUT)
        except LinkedInScraperException as exc:
            await capture.settle()
            seat = _seat_refusal(capture.responses)
            if seat is not None:
                raise LinkedInScraperException(
                    f"Sales Navigator refused this account (HTTP {seat.status}). "
                    + _NO_SEAT_ADVICE
                ) from exc
            if required:
                raise
            return None
        if refused is not None and refused.refuses(response):
            raise InvalidReferenceError(
                f"Sales Navigator refused that {refused.field} (HTTP "
                f"{response.status}). Check the {refused.field}: pass an id "
                "exactly as a previous Sales Navigator result returned it."
            )
        await capture.settle()
        return response

    @staticmethod
    async def _optional(
        capture: SalesNavigatorCapture, predicate: Callable[[SnResponse], bool]
    ) -> SnResponse | None:
        try:
            return await capture.wait_for(
                predicate, timeout=_OPTIONAL_WAIT, throttle=False
            )
        except LinkedInScraperException:
            return None

    async def _read_all(
        self,
        page_url: Callable[[int], str],
        *,
        first_page: int,
        expected_path: str,
        resources: tuple[str, ...],
        rows_at: Callable[[int], Callable[[SnResponse], bool]],
        builder: Callable[[Any], _Row | None],
        name: str,
        refused: _Refusal | None = None,
        on_first: Callable[[SalesNavigatorCapture], Awaitable[None]] | None = None,
        deadline: float | None = None,
        until: Callable[[list[_Row]], bool] | None = None,
    ) -> tuple[list[_Row], dict[str, str] | None, int | None, int]:
        """The rows of a paged table from ``first_page``, one load per 25 rows.

        Page N is LinkedIn's own Nth page: the URL carries ``page=N`` and only
        the response that starts at ``(N - 1) * 25`` is read. Reads at most
        ``MAX_PAGE`` pages; a longer table stops with a ``page_limit`` error
        and the page to continue from; so does a page that does not arrive
        (the page to retry), and a read whose next page would not finish
        before ``deadline`` (a ``time.monotonic()`` instant), judged by the
        slowest page so far, pacing wait included. The first page is always
        read. Also stops with a ``page_limit`` error and no page
        to continue from when the reported total changes between pages or a
        row repeats one of an earlier page. With ``until`` the read ends,
        without an error, once the rows read so far satisfy it. The last value
        is LinkedIn's ``paging.total``.
        """
        if first_page < 1:
            raise FilterValidationError("page must be 1 or greater.")
        rows: list[_Row] = []
        seen: set[str | None] = set()
        total = 0
        slowest = 0.0
        page = first_page - 1
        while page < first_page or page * PAGE_SIZE < total:
            read_pages = page - first_page + 1
            if read_pages == MAX_PAGE:
                return (
                    rows,
                    _page_limit(
                        name,
                        total,
                        len(rows),
                        f"one call reads at most {RESULT_CAP:,} (pass "
                        f"next_page={page + 1} to continue)",
                    ),
                    page + 1,
                    total,
                )
            if (
                deadline is not None
                and read_pages > 0
                and time.monotonic() + slowest > deadline
            ):
                return (
                    rows,
                    _page_limit(
                        name,
                        total,
                        len(rows),
                        f"this call ran out of time after {read_pages} pages (pass "
                        f"next_page={page + 1} to continue)",
                    ),
                    page + 1,
                    total,
                )
            page += 1
            began = time.monotonic()
            first = page == first_page
            ready = rows_at((page - 1) * PAGE_SIZE)
            async with self._capture(*resources) as capture:
                found = await self._open(
                    capture,
                    page_url(page),
                    expected_path=expected_path,
                    ready=ready,
                    refused=refused,
                    required=first,
                )
                if first and on_first is not None:
                    await on_first(capture)
            if found is None:
                return (
                    rows,
                    _page_limit(
                        name,
                        total,
                        len(rows),
                        f"page {page} did not load (pass next_page={page} to retry)",
                    ),
                    page,
                    total,
                )
            built = _first_built(capture.matching(ready), builder)
            if built is None:
                raise _unverified(f"the {name}")
            items, reported = built
            if first:
                total = reported
            elif reported != total:
                return (
                    rows,
                    _page_limit(
                        name,
                        total,
                        len(rows),
                        f"the total changed to {reported} while it was read",
                    ),
                    None,
                    total,
                )
            ids = [item.get("id") for item in items]
            if seen.intersection(ids):
                return (
                    rows,
                    _page_limit(
                        name,
                        total,
                        len(rows),
                        f"the list changed while reading (page {page} repeated a row)",
                    ),
                    None,
                    total,
                )
            seen.update(ids)
            rows.extend(items)
            if until is not None and until(rows):
                return rows, None, None, total
            slowest = max(slowest, time.monotonic() - began)
        if len(rows) < total - (first_page - 1) * PAGE_SIZE:
            return (
                rows,
                _page_limit(name, total, len(rows), "the pages returned fewer rows"),
                None,
                total,
            )
        return rows, None, None, total

    async def _geo_ids(self, values: Sequence[str], field: str) -> list[str]:
        ids: list[str] = []
        for value in values:
            geo_id = value.removeprefix("urn:li:geo:")
            if not geo_id.isdigit():
                resolved = await self._facets.resolve_geo_urn(value)
                if resolved is None:
                    raise FilterValidationError(
                        f"Could not resolve {field} {value!r} to a LinkedIn "
                        "location; pass its numeric geo id instead."
                    )
                geo_id = resolved
            ids.append(geo_id)
        return ids

    async def _company_ids(self, values: Sequence[str]) -> list[str]:
        ids: list[str] = []
        for value in values:
            try:
                company_id = normalize_sales_company_id(value)
            except InvalidReferenceError:
                company_id = await self._facets.resolve_company_urn(value)
            ids.append(COMPANY_FILTER_ID.format(company_id))
        return ids

    async def _search(
        self,
        kind: Literal["company", "people"],
        resource: str,
        keywords: str | None,
        filters: Sequence[tuple[str, SearchFilter]],
        page: int,
    ) -> tuple[str, SnResponse]:
        """Load one search page; return the response whose request sent exactly the criteria asked."""
        url, response = await self._search_page(
            kind, resource, keywords, filters, page, required=True
        )
        if response is None:
            raise _unverified("the search results")
        return url, response

    async def _search_page(
        self,
        kind: Literal["company", "people"],
        resource: str,
        keywords: str | None,
        filters: Sequence[tuple[str, SearchFilter]],
        page: int,
        *,
        required: bool,
    ) -> tuple[str, SnResponse | None]:
        """``_search``; with ``required=False`` a page that never answered is None."""
        criteria = [search_filter for _, search_filter in filters]
        url = search_page_url(kind, build_search_query(keywords, criteria), page)
        start = (page - 1) * PAGE_SIZE

        def is_page(response: SnResponse) -> bool:
            return _is(response, resource, finder="searchQuery") and _starts_at(
                response, start
            )

        async with self._capture(resource) as capture:
            found = await self._open(
                capture,
                url,
                expected_path=f"/sales/search/{kind}",
                ready=is_page,
                required=required,
            )
        if found is None:
            return url, None
        responses = capture.matching(is_page)
        for response in responses:
            sent = response.call.params.get("query", "")
            if not missing_echoes(keywords, criteria, sent) and not unrequested_echoes(
                keywords, criteria, sent
            ):
                return url, response
        sent = responses[0].call.params.get("query", "")
        names = {search_filter.type: field for field, search_filter in filters}
        if missing := missing_echoes(keywords, criteria, sent):
            dropped = ", ".join(names.get(item, item) for item in missing)
            raise LinkedInScraperException(
                f"The Sales Navigator page dropped or rewrote the {dropped} "
                "filter, so the results would not be filtered as asked and "
                "nothing was reported."
            )
        added = ", ".join(unrequested_echoes(keywords, criteria, sent))
        raise LinkedInScraperException(
            f"The Sales Navigator page also sent the {added} filter, which was "
            "not asked for, so the results would not be filtered as asked and "
            "nothing was reported."
        )

    @staticmethod
    def _check_page(page: int) -> None:
        if not 1 <= page <= MAX_PAGE:
            raise FilterValidationError(
                f"page must be between 1 and {MAX_PAGE}: Sales Navigator shows "
                f"at most {MAX_PAGE * PAGE_SIZE} results per search."
            )

    async def account_criteria(
        self,
        *,
        keywords: str | None = None,
        industry: Sequence[str] | None = None,
        exclude_industry: Sequence[str] | None = None,
        hq_location: Sequence[str] | None = None,
        size: Sequence[str] | None = None,
        headcount_growth_min: int | None = None,
        headcount_growth_max: int | None = None,
        department_headcount: Mapping[str, object] | None = None,
    ) -> tuple[str | None, list[tuple[str, SearchFilter]]]:
        """The keywords and filters an account search sends, ids resolved.

        Shared by the search and by saving the search, so both send the same
        query.
        """
        keywords = (keywords or "").strip() or None
        industries = industry_ids(_criteria(industry))
        not_industries = industry_ids(_criteria(exclude_industry))
        sizes = company_size_letters(_criteria(size))
        locations = _criteria(hq_location)
        growth = headcount_growth_min is not None or headcount_growth_max is not None
        if (
            headcount_growth_min is not None
            and headcount_growth_max is not None
            and headcount_growth_min > headcount_growth_max
        ):
            raise FilterValidationError(
                "headcount_growth_min must not be greater than headcount_growth_max."
            )
        department = (
            None
            if department_headcount is None
            else _department_headcount(department_headcount)
        )
        if not (
            keywords
            or industries
            or not_industries
            or sizes
            or locations
            or growth
            or department
        ):
            raise FilterValidationError(
                "Give at least one search criterion: keywords, industry, "
                "exclude_industry, hq_location, size, a headcount growth bound "
                "or department_headcount."
            )
        _refuse_overlap("industry", industries, not_industries)
        filters: list[tuple[str, SearchFilter]] = []
        if industries or not_industries:
            filters.append(
                (
                    _field("industry", industries, not_industries),
                    SearchFilter(
                        INDUSTRY,
                        ids=tuple(industries),
                        excluded_ids=tuple(not_industries),
                    ),
                )
            )
        if locations:
            geo = await self._geo_ids(locations, "hq_location")
            filters.append(("hq_location", SearchFilter(REGION, ids=tuple(geo))))
        if sizes:
            filters.append(("size", SearchFilter(COMPANY_HEADCOUNT, ids=tuple(sizes))))
        if growth:
            filters.append(
                (
                    "headcount growth",
                    SearchFilter(
                        COMPANY_HEADCOUNT_GROWTH,
                        minimum=headcount_growth_min,
                        maximum=headcount_growth_max,
                    ),
                )
            )
        if department:
            department_id, minimum, maximum = department
            filters.append(
                (
                    "department_headcount",
                    SearchFilter(
                        DEPARTMENT_HEADCOUNT,
                        ids=(department_id,),
                        minimum=minimum,
                        maximum=maximum,
                    ),
                )
            )

        return keywords, filters

    async def search_accounts(
        self,
        *,
        keywords: str | None = None,
        industry: Sequence[str] | None = None,
        exclude_industry: Sequence[str] | None = None,
        hq_location: Sequence[str] | None = None,
        size: Sequence[str] | None = None,
        headcount_growth_min: int | None = None,
        headcount_growth_max: int | None = None,
        department_headcount: Mapping[str, object] | None = None,
        page: int = 1,
    ) -> dict[str, Any]:
        """One page (25) of Sales Navigator account search results.

        When Sales Navigator dropped an asked filter to find anything, no
        account is reported and ``section_errors.accounts`` says which.
        """
        self._check_page(page)
        keywords, filters = await self.account_criteria(
            keywords=keywords,
            industry=industry,
            exclude_industry=exclude_industry,
            hq_location=hq_location,
            size=size,
            headcount_growth_min=headcount_growth_min,
            headcount_growth_max=headcount_growth_max,
            department_headcount=department_headcount,
        )
        url, response = await self._search(
            "company", _ACCOUNT_SEARCH, keywords, filters, page
        )
        cards = build_elements(response.payload, build_account_card)
        paging = build_paging(response.payload, page, len(cards or ()))
        if cards is None or paging is None:
            raise _unverified("the account search results")
        if relaxed := _relaxed(response.payload, filters, paging, "accounts"):
            empty, note = relaxed
            return {
                "url": url,
                "sections": {},
                "accounts": [],
                "section_errors": {"accounts": note},
                "paging": empty,
            }
        result: dict[str, Any] = {"url": url, "sections": {}, "accounts": cards}
        if text := render_account_cards(cards):
            result["sections"]["accounts"] = text
        result["paging"] = paging
        return result

    async def lead_criteria(
        self,
        *,
        keywords: str | None = None,
        title: Sequence[str] | None = None,
        seniority: Sequence[str] | None = None,
        current_company: Sequence[str] | None = None,
        exclude_current_company: Sequence[str] | None = None,
        location: Sequence[str] | None = None,
        company_hq_location: Sequence[str] | None = None,
        industry: Sequence[str] | None = None,
        exclude_industry: Sequence[str] | None = None,
        network: Sequence[str] | None = None,
        function: Sequence[str] | None = None,
        company_size: Sequence[str] | None = None,
        years_in_current_role: Sequence[str] | None = None,
        years_at_current_company: Sequence[str] | None = None,
        account_list: Sequence[str] | None = None,
        lead_list: Sequence[str] | None = None,
        exclude_lead_list: Sequence[str] | None = None,
    ) -> tuple[str | None, list[tuple[str, SearchFilter]]]:
        """The keywords and filters a lead search sends, ids resolved.

        Shared by the search and by saving the search, so both send the same
        query.
        """
        keywords = (keywords or "").strip() or None
        titles = _criteria(title)
        levels = _criteria(seniority)
        unknown = [level for level in levels if level not in SENIORITY_IDS]
        if unknown:
            raise FilterValidationError(
                f"Unknown seniority {unknown!r}; expected one of "
                f"{list(SENIORITY_IDS)!r}."
            )
        companies = _criteria(current_company)
        not_companies = _criteria(exclude_current_company)
        locations = _criteria(location)
        hq_locations = _criteria(company_hq_location)
        industries = industry_ids(_criteria(industry))
        not_industries = industry_ids(_criteria(exclude_industry))
        degrees = network_tokens(_criteria(network))
        functions = function_ids(_criteria(function))
        sizes = company_size_letters(_criteria(company_size))
        in_role = tenure_ids(_criteria(years_in_current_role), "years_in_current_role")
        at_company = tenure_ids(
            _criteria(years_at_current_company), "years_at_current_company"
        )
        account_lists = [normalize_sales_list_id(v) for v in _criteria(account_list)]
        lead_lists = [normalize_sales_list_id(v) for v in _criteria(lead_list)]
        not_lead_lists = [
            normalize_sales_list_id(v) for v in _criteria(exclude_lead_list)
        ]
        if not (
            keywords
            or titles
            or levels
            or companies
            or not_companies
            or locations
            or hq_locations
            or industries
            or not_industries
            or degrees
            or functions
            or sizes
            or in_role
            or at_company
            or account_lists
            or lead_lists
            or not_lead_lists
        ):
            raise FilterValidationError(
                "Give at least one search criterion: keywords, title, seniority, "
                "current_company, exclude_current_company, location, "
                "company_hq_location, industry, "
                "exclude_industry, network, function, company_size, years_in_current_role, years_at_current_company, "
                "account_list, lead_list or exclude_lead_list."
            )
        _refuse_overlap("industry", industries, not_industries)
        _refuse_overlap("lead_list", lead_lists, not_lead_lists)
        if lead_lists or not_lead_lists:
            await self._known_lists("LEAD", [*lead_lists, *not_lead_lists])
        if account_lists:
            await self._known_lists("ACCOUNT", account_lists)
        filters: list[tuple[str, SearchFilter]] = []
        if titles:
            filters.append(("title", SearchFilter(CURRENT_TITLE, texts=tuple(titles))))
        if levels:
            ids = tuple(SENIORITY_IDS[cast(SeniorityLevel, level)] for level in levels)
            filters.append(("seniority", SearchFilter(SENIORITY_LEVEL, ids=ids)))
        if companies or not_companies:
            company_ids = await self._company_ids(companies)
            not_company_ids = await self._company_ids(not_companies)
            _refuse_overlap("current_company", company_ids, not_company_ids)
            filters.append(
                (
                    _field("current_company", companies, not_companies),
                    SearchFilter(
                        CURRENT_COMPANY,
                        ids=tuple(company_ids),
                        excluded_ids=tuple(not_company_ids),
                    ),
                )
            )
        if locations:
            geo = await self._geo_ids(locations, "location")
            filters.append(("location", SearchFilter(REGION, ids=tuple(geo))))
        if hq_locations:
            geo = await self._geo_ids(hq_locations, "company_hq_location")
            filters.append(
                (
                    "company_hq_location",
                    SearchFilter(COMPANY_HEADQUARTERS, ids=tuple(geo)),
                )
            )
        if industries or not_industries:
            filters.append(
                (
                    _field("industry", industries, not_industries),
                    SearchFilter(
                        INDUSTRY,
                        ids=tuple(industries),
                        excluded_ids=tuple(not_industries),
                    ),
                )
            )
        if degrees:
            filters.append(("network", SearchFilter(RELATIONSHIP, ids=tuple(degrees))))
        if functions:
            filters.append(("function", SearchFilter(FUNCTION, ids=tuple(functions))))
        if sizes:
            filters.append(
                ("company_size", SearchFilter(COMPANY_HEADCOUNT, ids=tuple(sizes)))
            )
        if in_role:
            filters.append(
                (
                    "years_in_current_role",
                    SearchFilter(YEARS_IN_CURRENT_POSITION, ids=tuple(in_role)),
                )
            )
        if at_company:
            filters.append(
                (
                    "years_at_current_company",
                    SearchFilter(YEARS_AT_CURRENT_COMPANY, ids=tuple(at_company)),
                )
            )
        if account_lists:
            filters.append(
                ("account_list", SearchFilter(ACCOUNT_LIST, ids=tuple(account_lists)))
            )
        if lead_lists or not_lead_lists:
            filters.append(
                (
                    _field("lead_list", lead_lists, not_lead_lists),
                    SearchFilter(
                        LEAD_LIST,
                        ids=tuple(lead_lists),
                        excluded_ids=tuple(not_lead_lists),
                    ),
                )
            )

        return keywords, filters

    async def search_leads(
        self,
        *,
        keywords: str | None = None,
        title: Sequence[str] | None = None,
        seniority: Sequence[str] | None = None,
        current_company: Sequence[str] | None = None,
        exclude_current_company: Sequence[str] | None = None,
        location: Sequence[str] | None = None,
        company_hq_location: Sequence[str] | None = None,
        industry: Sequence[str] | None = None,
        exclude_industry: Sequence[str] | None = None,
        network: Sequence[str] | None = None,
        function: Sequence[str] | None = None,
        company_size: Sequence[str] | None = None,
        years_in_current_role: Sequence[str] | None = None,
        years_at_current_company: Sequence[str] | None = None,
        account_list: Sequence[str] | None = None,
        lead_list: Sequence[str] | None = None,
        exclude_lead_list: Sequence[str] | None = None,
        page: int = 1,
    ) -> dict[str, Any]:
        """One page (25) of Sales Navigator lead search results.

        Leads with LinkedIn's third-party sharing block are left out and
        counted in ``withheld``. When Sales Navigator dropped an asked filter
        to find anyone, no lead is reported and ``section_errors.leads`` says
        which.
        """
        self._check_page(page)
        keywords, filters = await self.lead_criteria(
            keywords=keywords,
            title=title,
            seniority=seniority,
            current_company=current_company,
            exclude_current_company=exclude_current_company,
            location=location,
            company_hq_location=company_hq_location,
            industry=industry,
            exclude_industry=exclude_industry,
            network=network,
            function=function,
            company_size=company_size,
            years_in_current_role=years_in_current_role,
            years_at_current_company=years_at_current_company,
            account_list=account_list,
            lead_list=lead_list,
            exclude_lead_list=exclude_lead_list,
        )
        url, response = await self._search(
            "people", _LEAD_SEARCH, keywords, filters, page
        )
        elements = envelope_elements(response.payload)
        built = build_leads(elements) if elements is not None else None
        paging = build_paging(response.payload, page, len(built[0]) if built else 0)
        if built is None or paging is None:
            raise _unverified("the lead search results")
        if relaxed := _relaxed(response.payload, filters, paging, "leads"):
            empty, note = relaxed
            return {
                "url": url,
                "sections": {},
                "leads": [],
                "section_errors": {"leads": note},
                "paging": empty,
            }
        leads, withheld = built
        result: dict[str, Any] = {"url": url, "sections": {}, "leads": leads}
        if text := render_leads(leads):
            result["sections"]["leads"] = text
        result["paging"] = paging
        if (note := build_withheld(withheld)) is not None:
            result["withheld"] = note
        return result

    async def get_account(self, company_id: str) -> dict[str, Any]:
        """One account's firmographics from its Sales Navigator page."""
        company = normalize_sales_company_id(company_id)
        url = sales_company_url(company)
        section_errors: dict[str, dict[str, str]] = {}

        def is_company(response: SnResponse) -> bool:
            return (
                _is(response, _COMPANIES)
                and response.call.entity_id == company
                and envelope_record(response.payload) is not None
            )

        def is_named(response: SnResponse) -> bool:
            record = envelope_record(response.payload) or {}
            return is_company(response) and as_str(record.get("name")) is not None

        def is_headcount(response: SnResponse) -> bool:
            record = envelope_record(response.payload) or {}
            return is_company(response) and any(
                field in record for field in _HEADCOUNT_FIELDS
            )

        async with self._capture(_COMPANIES) as capture:
            await self._open(
                capture,
                url,
                expected_path=urlparse(url).path,
                ready=is_named,
                refused=_Refusal("company_id", frozenset({_COMPANIES}), company),
            )
            if not capture.matching(is_headcount):
                await self._optional(capture, is_headcount)
                await capture.settle()

        records = [
            record
            for response in capture.matching(is_company)
            if (record := envelope_record(response.payload)) is not None
        ]
        account = build_account(records, company)
        if account is None:
            raise _unverified("the account")
        if not capture.matching(is_headcount):
            section_errors["headcount"] = _error(
                "unavailable", "Sales Navigator did not load the account's headcount."
            )
        result: dict[str, Any] = {
            "url": url,
            "sections": {"account": render_account(account)},
            "account": account,
        }
        if section_errors:
            result["section_errors"] = section_errors
        return result

    async def list_account_lists(
        self, page: int = 1, *, deadline: float | None = None
    ) -> dict[str, Any]:
        """The member's account lists from ``page``, up to 1,000 per call.

        Fewer when the next page would not finish before ``deadline``.
        """
        return await self._list_lists(
            SALES_ACCOUNT_LISTS_URL, "ACCOUNT", "account_lists", page, deadline
        )

    async def list_lead_lists(
        self, page: int = 1, *, deadline: float | None = None
    ) -> dict[str, Any]:
        """The member's lead lists from ``page``, up to 1,000 per call.

        Fewer when the next page would not finish before ``deadline``.
        """
        return await self._list_lists(
            SALES_LEAD_LISTS_URL, "LEAD", "lead_lists", page, deadline
        )

    async def _list_lists(
        self,
        url: str,
        list_type: str,
        key: Literal["account_lists", "lead_lists"],
        page: int,
        deadline: float | None,
    ) -> dict[str, Any]:
        lists, limit, next_page, total = await self._read_lists(
            url, list_type, key, page, deadline
        )
        result: dict[str, Any] = {"url": url, "sections": {}, key: lists}
        if text := render_account_lists(lists):
            result["sections"][key] = text
        if limit is not None:
            result["section_errors"] = {key: limit}
        result["paging"] = _read_paging(page, len(lists), total, next_page)
        if next_page is not None:
            result["next_page"] = next_page
        return result

    async def _read_lists(
        self,
        url: str,
        list_type: str,
        key: Literal["account_lists", "lead_lists"],
        page: int,
        deadline: float | None,
        until: Callable[[list[AccountList]], bool] | None = None,
    ) -> tuple[list[AccountList], dict[str, str] | None, int | None, int]:
        def rows_at(start: int) -> Callable[[SnResponse], bool]:
            def match(response: SnResponse) -> bool:
                return (
                    _is(response, _LISTS, finder="listType")
                    and response.call.params.get("listType") == list_type
                    and _starts_at(response, start)
                )

            return match

        return await self._read_all(
            lambda n: url if n == 1 else f"{url}?page={n}",
            first_page=page,
            expected_path=urlparse(url).path,
            resources=(_LISTS,),
            rows_at=rows_at,
            builder=build_account_list,
            name=key.replace("_", " "),
            deadline=deadline,
            until=until,
        )

    async def _known_lists(
        self,
        list_type: Literal["LEAD", "ACCOUNT"],
        ids: Sequence[str],
        deadline: float | None = None,
    ) -> dict[str, AccountList]:
        """The member's own lists named by ``ids``, read from the list hub.

        Sales Navigator's search does not check a list id: an id it does not
        know is ignored and the search returns leads the list does not hold.
        So every id is looked up first, reading list pages only until all are
        found; one not among the member's lists is refused before any search.
        """
        if list_type == "LEAD":
            url, key, kind = SALES_LEAD_LISTS_URL, "lead_lists", "lead"
        else:
            url, key, kind = SALES_ACCOUNT_LISTS_URL, "account_lists", "account"
        wanted = set(ids)

        def found_all(rows: list[AccountList]) -> bool:
            return wanted <= {row.get("id") for row in rows}

        lists, limit, _, _ = await self._read_lists(
            url, list_type, key, 1, deadline, until=found_all
        )
        known = {
            list_id: row
            for row in lists
            if (list_id := row.get("id")) is not None and list_id in wanted
        }
        missing = ", ".join(sorted(wanted - known.keys()))
        if not missing:
            return known
        if limit is not None:
            raise LinkedInScraperException(
                f"Could not confirm the {kind} list {missing}: {limit['error_message']}"
            )
        raise InvalidReferenceError(
            f"{missing} is not one of this account's Sales Navigator {kind} "
            f"lists. Pass a list id as list_sales_navigator_{kind}_lists "
            "returned it."
        )

    async def get_lead_list(
        self, list_id: str, page: int = 1, *, deadline: float | None = None
    ) -> dict[str, Any]:
        """The leads of one lead list from ``page``, read as a lead search.

        Sales Navigator's lead search filtered to the list, one search page of
        25 per load, so the rows are the same lead records a search returns
        and the 1,000-result cap of a search applies. One call loads at most
        ``_LEAD_LIST_PAGES_PER_CALL`` pages, and fewer when the next one would
        not finish before ``deadline`` or a pacing cap refuses it; each stop
        leaves ``next_page``.

        The id is first looked up among the member's lead lists (see
        ``_known_lists``); ``lead_list`` is that record. When its entityCount
        is more than the search's total, ``section_errors.lead_list`` says
        how many leads of the list the search does not serve; when it is
        less, it says the search may not have been limited to the list. A
        page Sales Navigator answered by dropping a filter ends the read with
        no leads and ``section_errors.leads`` (``filters_relaxed``).
        """
        list_ref = normalize_sales_list_id(list_id)
        self._check_page(page)
        lead_list = (await self._known_lists("LEAD", [list_ref], deadline))[list_ref]
        filters = [("lead_list", SearchFilter(LEAD_LIST, ids=(list_ref,)))]
        leads: list[Lead] = []
        seen: set[str] = set()
        withheld = 0
        total: int | None = None
        limit: dict[str, Any] | None = None
        relaxed: dict[str, str] | None = None
        next_page: int | None = None
        slowest = 0.0
        current = page
        first_url = ""

        while True:
            if current > page:
                if current > MAX_PAGE:
                    limit = _page_limit(
                        "leads",
                        total or 0,
                        len(leads),
                        f"a search shows at most {RESULT_CAP:,}",
                    )
                    break
                if current - page == _LEAD_LIST_PAGES_PER_CALL:
                    limit = _page_limit(
                        "leads",
                        total or 0,
                        len(leads),
                        f"one call reads at most {_LEAD_LIST_PAGES_PER_CALL} pages "
                        f"(pass next_page={current} to continue)",
                    )
                    next_page = current
                    break
                if deadline is not None and time.monotonic() + slowest > deadline:
                    limit = _page_limit(
                        "leads",
                        total or 0,
                        len(leads),
                        f"this call ran out of time after {current - page} pages "
                        f"(pass next_page={current} to continue)",
                    )
                    next_page = current
                    break
            began = time.monotonic()
            try:
                url, response = await self._search_page(
                    "people",
                    _LEAD_SEARCH,
                    None,
                    filters,
                    current,
                    required=current == page,
                )
            except ActionLimitError as exc:
                # Refused before the load, so nothing was charged; the first
                # page has no rows to keep and raises.
                if current == page:
                    raise
                limit = {
                    **limit_exceeded_section_error(exc),
                    "next_page": current,
                }
                next_page = current
                break
            first_url = first_url or url
            if response is None:
                limit = _page_limit(
                    "leads",
                    total or 0,
                    len(leads),
                    f"page {current} did not load (pass next_page={current} to retry)",
                )
                next_page = current
                break
            elements = envelope_elements(response.payload)
            built = build_leads(elements) if elements is not None else None
            paging = build_paging(
                response.payload, current, len(built[0]) if built else 0
            )
            if built is None or paging is None:
                raise _unverified("the lead list")
            relaxed = _relaxed_note(response.payload, filters, "leads", paging["total"])
            if relaxed is not None:
                break
            rows, hidden = built
            if total is None:
                total = paging["total"]
            elif paging["total"] != total:
                limit = _page_limit(
                    "leads",
                    total,
                    len(leads),
                    f"the total changed to {paging['total']} while it was read",
                )
                break
            ids = [row["id"] for row in rows]
            if seen.intersection(ids):
                limit = _page_limit(
                    "leads",
                    total,
                    len(leads),
                    f"the list changed while reading (page {current} repeated a lead)",
                )
                break
            seen.update(ids)
            leads.extend(rows)
            withheld += hidden
            slowest = max(slowest, time.monotonic() - began)
            if not paging["more"]:
                if paging["capped"]:
                    limit = _page_limit(
                        "leads",
                        total,
                        len(leads),
                        f"a search shows at most {RESULT_CAP:,}",
                    )
                break
            current += 1
        if relaxed is not None:
            leads, seen, withheld, total, limit, next_page = [], set(), 0, 0, None, None
        reported = total or 0
        start = (page - 1) * PAGE_SIZE
        # Unique leads: a lead a page repeats is one row read, not two.
        read = len(seen) + withheld
        if limit is None and start + read < min(reported, RESULT_CAP):
            limit = _page_limit(
                "leads", reported, len(leads), "the pages returned fewer rows"
            )
        result: dict[str, Any] = {
            "url": first_url,
            "sections": {},
            "lead_list": lead_list,
            "leads": leads,
        }
        if text := render_leads(leads):
            result["sections"]["leads"] = text
        section_errors: dict[str, dict[str, Any]] = {}
        if limit is not None:
            section_errors["leads"] = limit
        if relaxed is not None:
            section_errors["leads"] = relaxed
        held = lead_list.get("entityCount")
        if held is not None and relaxed is None and reported > held:
            section_errors["lead_list"] = _error(
                "unverified",
                f"The list holds {held} leads but Sales Navigator's search "
                f"reported {reported}, so the search may not have been limited "
                "to the list and some rows may not be in it.",
            )
        elif held is not None and relaxed is None and reported < held:
            section_errors["lead_list"] = _error(
                "unavailable",
                f"The list holds {held} leads but Sales Navigator's search "
                f"serves {reported} of them, so {held - reported} cannot be "
                "read here (for example a lead Sales Navigator no longer "
                "shows).",
            )
        if section_errors:
            result["section_errors"] = section_errors
        result["paging"] = {
            "page": page,
            "start": start,
            "count": len(leads),
            "total": reported,
            "resultCap": RESULT_CAP,
            "capped": reported > RESULT_CAP,
            "more": start + read < min(reported, RESULT_CAP) or limit is not None,
            "next_page": next_page,
        }
        if next_page is not None:
            result["next_page"] = next_page
        if (note := build_withheld(withheld)) is not None:
            result["withheld"] = note
        return result

    async def get_account_list(
        self, list_id: str, page: int = 1, *, deadline: float | None = None
    ) -> dict[str, Any]:
        """One account list and its accounts from ``page``, up to 1,000 per call.

        Fewer when the next page would not finish before ``deadline``.
        """
        list_ref = normalize_sales_list_id(list_id)
        url = sales_account_list_url(list_ref)
        metadata: list[SnResponse] = []

        def is_metadata(response: SnResponse) -> bool:
            return _is(response, _LISTS) and response.call.entity_id == list_ref

        def rows_at(start: int) -> Callable[[SnResponse], bool]:
            def match(response: SnResponse) -> bool:
                return (
                    _is(response, _ACCOUNT_TABLE, finder="list")
                    and response.call.params.get("listId") == list_ref
                    and _starts_at(response, start)
                )

            return match

        async def read_metadata(capture: SalesNavigatorCapture) -> None:
            if not capture.matching(is_metadata):
                await capture.wait_for(is_metadata, timeout=_READY_TIMEOUT)
            metadata.extend(capture.matching(is_metadata))

        accounts, limit, next_page, total = await self._read_all(
            lambda n: url if n == 1 else f"{url}&page={n}",
            first_page=page,
            expected_path=_LIST_PATH,
            resources=(_LISTS, _ACCOUNT_TABLE),
            rows_at=rows_at,
            builder=build_listed_account,
            name="accounts",
            refused=_Refusal("list_id", frozenset({_LISTS, _ACCOUNT_TABLE}), list_ref),
            on_first=read_metadata,
            deadline=deadline,
        )
        account_list = (
            build_account_list(envelope_record(metadata[0].payload))
            if metadata
            else None
        )
        if account_list is None or account_list.get("id") != list_ref:
            raise _unverified("the account list")
        result: dict[str, Any] = {"url": url, "sections": {}}
        result["sections"]["account_list"] = render_account_lists([account_list])
        if text := render_listed_accounts(accounts):
            result["sections"]["accounts"] = text
        result["account_list"] = account_list
        result["accounts"] = accounts
        if limit is not None:
            result["section_errors"] = {"accounts": limit}
        result["paging"] = _read_paging(page, len(accounts), total, next_page)
        if next_page is not None:
            result["next_page"] = next_page
        return result
