"""Sales Navigator reads: account and lead search, accounts and account lists.

Every read is a page load of a URL built here, read from the responses Sales
Navigator itself fetches; nothing is replayed or evaluated in the page. Lead
pages are never opened (they register profile views), and a lead who asked
LinkedIn not to share data with third-party tools is left out and counted.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeVar, cast
from urllib.parse import urlparse

from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
    LinkedInScraperException,
)
from linkedin_mcp_server.scraping.contracts import FilterValidationError
from linkedin_mcp_server.scraping.facets import FacetResolver
from linkedin_mcp_server.scraping.identifiers import (
    SALES_ACCOUNT_LISTS_URL,
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
    ListedAccount,
    SearchFilter,
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
_LISTS_PATH = urlparse(SALES_ACCOUNT_LISTS_URL).path
_LIST_PATH = "/sales/accounts/dashboard"
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
    ) -> tuple[list[_Row], dict[str, str] | None, int | None, int]:
        """The rows of a paged table from ``first_page``, one load per 25 rows.

        Page N is LinkedIn's own Nth page: the URL carries ``page=N`` and only
        the response that starts at ``(N - 1) * 25`` is read. Reads at most
        ``MAX_PAGE`` pages; a longer table stops with a ``page_limit`` error
        and the page to continue from; so does a page that does not arrive
        (the page to retry). Also stops with a ``page_limit`` error and no page
        to continue from when the reported total changes between pages or a
        row repeats one of an earlier page. The last value is LinkedIn's
        ``paging.total``.
        """
        if first_page < 1:
            raise FilterValidationError("page must be 1 or greater.")
        rows: list[_Row] = []
        seen: set[str | None] = set()
        total = 0
        page = first_page - 1
        while page < first_page or page * PAGE_SIZE < total:
            if page - first_page + 1 == MAX_PAGE:
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
            page += 1
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
        """Load one search page and return the response for exactly what was asked."""
        criteria = [search_filter for _, search_filter in filters]
        url = search_page_url(kind, build_search_query(keywords, criteria), page)
        start = (page - 1) * PAGE_SIZE

        def is_page(response: SnResponse) -> bool:
            return _is(response, resource, finder="searchQuery") and _starts_at(
                response, start
            )

        async with self._capture(resource) as capture:
            await self._open(
                capture, url, expected_path=f"/sales/search/{kind}", ready=is_page
            )
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
            ignored = ", ".join(names.get(item, item) for item in missing)
            raise LinkedInScraperException(
                f"Sales Navigator ignored the {ignored} filter, so the results "
                "were not filtered as asked and nothing was reported."
            )
        added = ", ".join(unrequested_echoes(keywords, criteria, sent))
        raise LinkedInScraperException(
            f"Sales Navigator also applied the {added} filter, which was not "
            "asked for, so the results were not filtered as asked and nothing "
            "was reported."
        )

    @staticmethod
    def _check_page(page: int) -> None:
        if not 1 <= page <= MAX_PAGE:
            raise FilterValidationError(
                f"page must be between 1 and {MAX_PAGE}: Sales Navigator shows "
                f"at most {MAX_PAGE * PAGE_SIZE} results per search."
            )

    async def search_accounts(
        self,
        *,
        keywords: str | None = None,
        industry: Sequence[str] | None = None,
        hq_location: Sequence[str] | None = None,
        size: Sequence[str] | None = None,
        headcount_growth_min: int | None = None,
        headcount_growth_max: int | None = None,
        page: int = 1,
    ) -> dict[str, Any]:
        """One page (25) of Sales Navigator account search results."""
        self._check_page(page)
        keywords = (keywords or "").strip() or None
        industries = industry_ids(_criteria(industry))
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
        if not (keywords or industries or sizes or locations or growth):
            raise FilterValidationError(
                "Give at least one search criterion: keywords, industry, "
                "hq_location, size or a headcount growth bound."
            )
        filters: list[tuple[str, SearchFilter]] = []
        if industries:
            filters.append(("industry", SearchFilter(INDUSTRY, ids=tuple(industries))))
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

        url, response = await self._search(
            "company", _ACCOUNT_SEARCH, keywords, filters, page
        )
        cards = build_elements(response.payload, build_account_card)
        paging = build_paging(response.payload, page)
        if cards is None or paging is None:
            raise _unverified("the account search results")
        result: dict[str, Any] = {"url": url, "sections": {}, "accounts": cards}
        if text := render_account_cards(cards):
            result["sections"]["accounts"] = text
        result["paging"] = paging
        return result

    async def search_leads(
        self,
        *,
        keywords: str | None = None,
        title: Sequence[str] | None = None,
        seniority: Sequence[str] | None = None,
        current_company: Sequence[str] | None = None,
        location: Sequence[str] | None = None,
        industry: Sequence[str] | None = None,
        page: int = 1,
    ) -> dict[str, Any]:
        """One page (25) of Sales Navigator lead search results.

        Leads with LinkedIn's third-party sharing block are left out and
        counted in ``withheld``.
        """
        self._check_page(page)
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
        locations = _criteria(location)
        industries = industry_ids(_criteria(industry))
        if not (keywords or titles or levels or companies or locations or industries):
            raise FilterValidationError(
                "Give at least one search criterion: keywords, title, seniority, "
                "current_company, location or industry."
            )
        filters: list[tuple[str, SearchFilter]] = []
        if titles:
            filters.append(("title", SearchFilter(CURRENT_TITLE, texts=tuple(titles))))
        if levels:
            ids = tuple(SENIORITY_IDS[cast(SeniorityLevel, level)] for level in levels)
            filters.append(("seniority", SearchFilter(SENIORITY_LEVEL, ids=ids)))
        if companies:
            company_ids = tuple(await self._company_ids(companies))
            filters.append(
                ("current_company", SearchFilter(CURRENT_COMPANY, ids=company_ids))
            )
        if locations:
            geo = await self._geo_ids(locations, "location")
            filters.append(("location", SearchFilter(REGION, ids=tuple(geo))))
        if industries:
            filters.append(("industry", SearchFilter(INDUSTRY, ids=tuple(industries))))

        url, response = await self._search(
            "people", _LEAD_SEARCH, keywords, filters, page
        )
        elements = envelope_elements(response.payload)
        built = build_leads(elements) if elements is not None else None
        paging = build_paging(response.payload, page)
        if built is None or paging is None:
            raise _unverified("the lead search results")
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

    async def list_account_lists(self, page: int = 1) -> dict[str, Any]:
        """The member's account lists from ``page``, up to 1,000 per call."""
        url = SALES_ACCOUNT_LISTS_URL

        def rows_at(start: int) -> Callable[[SnResponse], bool]:
            def match(response: SnResponse) -> bool:
                return (
                    _is(response, _LISTS, finder="listType")
                    and response.call.params.get("listType") == "ACCOUNT"
                    and _starts_at(response, start)
                )

            return match

        lists, limit, next_page, total = await self._read_all(
            lambda n: url if n == 1 else f"{url}?page={n}",
            first_page=page,
            expected_path=_LISTS_PATH,
            resources=(_LISTS,),
            rows_at=rows_at,
            builder=build_account_list,
            name="account lists",
        )
        result: dict[str, Any] = {"url": url, "sections": {}, "account_lists": lists}
        if text := render_account_lists(lists):
            result["sections"]["account_lists"] = text
        if limit is not None:
            result["section_errors"] = {"account_lists": limit}
        result["paging"] = _read_paging(page, len(lists), total, next_page)
        if next_page is not None:
            result["next_page"] = next_page
        return result

    async def get_account_list(self, list_id: str, page: int = 1) -> dict[str, Any]:
        """One account list and its accounts from ``page``, up to 1,000 per call."""
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
