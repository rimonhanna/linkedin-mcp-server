"""Tests for Sales Navigator read workflows over a fake page (synthetic data)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import logging

import pytest

from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
    LinkedInScraperException,
)
from linkedin_mcp_server.scraping import api_capture as capture_module
from linkedin_mcp_server.scraping import sales_navigator as reader_module
from linkedin_mcp_server.scraping.contracts import FilterValidationError
from linkedin_mcp_server.scraping.identifiers import (
    SALES_ACCOUNT_LISTS_URL,
    sales_account_list_url,
    sales_company_url,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.sales_navigator import SalesNavigatorReader
from linkedin_mcp_server.scraping.sales_navigator_payload import (
    COMPANY_HEADCOUNT,
    COMPANY_HEADCOUNT_GROWTH,
    CURRENT_COMPANY,
    CURRENT_TITLE,
    INDUSTRY,
    REGION,
    SENIORITY_LEVEL,
    SearchFilter,
    build_search_query,
    search_page_url,
)
from linkedin_mcp_server.scraping.session import ScrapingSession

SN = "https://www.linkedin.com/sales-api/"
COMPANY = "100000001"
LIST = "100000301"
GEO = "100000099"
RESOLVED_COMPANY = "100000050"


def _response(
    path: str, payload: Any = None, *, method: str = "GET", status: int = 200
) -> SimpleNamespace:
    request = SimpleNamespace(method=method, url=SN + path)
    return SimpleNamespace(
        url=SN + path,
        status=status,
        request=request,
        json=AsyncMock(return_value=payload),
    )


def _boot() -> list[SimpleNamespace]:
    return [
        _response("salesApiAccess"),
        _response("salesApiEntitlements"),
        _response("salesApiWebAppBoot?action=recordBoot", method="POST"),
    ]


class _Browser:
    """Script what each page load makes the page emit."""

    def __init__(self, page: Any) -> None:
        self.page = page
        self.loads: dict[str, list[SimpleNamespace]] = {}
        self.landings: dict[str, str] = {}
        self.events: list[str] = []
        self.facets = MagicMock()
        self.facets.resolve_geo_urn = AsyncMock(side_effect=self._geo)
        self.facets.resolve_company_urn = AsyncMock(side_effect=self._company)
        self.geo: dict[str, str | None] = {}

    @property
    def navigated(self) -> list[str]:
        return [event[len("load ") :] for event in self.events if event[:5] == "load "]

    async def _geo(self, name: str) -> str | None:
        self.events.append(f"geo {name}")
        return self.geo.get(name, GEO)

    async def _company(self, name: str) -> str:
        self.events.append(f"company {name}")
        return RESOLVED_COMPANY

    async def navigate(self, url: str) -> None:
        self.events.append(f"load {url}")
        self.page.url = self.landings.get(url, url)
        for response in self.loads.get(url, []):
            for callback in list(self.page.listeners.get("request", [])):
                callback(response.request)
            for callback in list(self.page.listeners.get("response", [])):
                callback(response)

    @contextmanager
    def running(self) -> Iterator[None]:
        with (
            patch.object(
                PageNavigator, "_navigate_to_page", AsyncMock(side_effect=self.navigate)
            ),
            patch.object(ScrapingSession, "check_rate_limit", new_callable=AsyncMock),
            patch.object(ScrapingSession, "dismiss_modal", new_callable=AsyncMock),
            patch.object(capture_module, "note_throttle_signal", MagicMock()),
            patch.object(reader_module, "_READY_TIMEOUT", 0.2),
            patch.object(reader_module, "_OPTIONAL_WAIT", 0.1),
        ):
            yield


async def _run(
    page: Any, browser: _Browser, call: Callable[[SalesNavigatorReader], Any]
) -> Any:
    session = ScrapingSession(page)
    reader = SalesNavigatorReader(session, PageNavigator(session), browser.facets)
    with browser.running():
        return await call(reader)


def _search(resource: str, query: str, start: int, elements: list, total: int):
    return _response(
        f"{resource}?q=searchQuery&query={query}&start={start}&count=25"
        "&decorationId=example",
        {
            "metadata": {},
            "elements": elements,
            "paging": {"start": start, "count": 25, "total": total},
        },
    )


def _card(company_id: str = COMPANY) -> dict[str, Any]:
    return {
        "entityUrn": f"urn:li:fs_salesCompany:{company_id}",
        "companyName": "Example Company",
        "industry": "Example industry",
        "employeeCountRange": "11-50",
    }


# --- search_accounts --------------------------------------------------------

KEYWORDS = "cloud ai, saas: b2b (eu)"
ACCOUNT_FILTERS = [
    SearchFilter(INDUSTRY, ids=("4",)),
    SearchFilter(REGION, ids=(GEO,)),
    SearchFilter(COMPANY_HEADCOUNT, ids=("B",)),
    SearchFilter(COMPANY_HEADCOUNT_GROWTH, minimum=5),
]


def _account_search(**overrides: Any) -> Callable[[SalesNavigatorReader], Any]:
    args: dict[str, Any] = {
        "keywords": KEYWORDS,
        "industry": ["4"],
        "hq_location": [f"urn:li:geo:{GEO}"],
        "size": ["B"],
        "headcount_growth_min": 5,
        "page": 2,
        **overrides,
    }
    return lambda r: r.search_accounts(**args)


async def test_search_accounts_reads_the_requested_page(mock_page):
    browser = _Browser(mock_page)
    query = build_search_query(KEYWORDS, ACCOUNT_FILTERS)
    url = search_page_url("company", query, 2)
    browser.loads[url] = [
        *_boot(),
        _search("salesApiAccountSearch", query, 0, [_card("100000009")], 1500),
        _search("salesApiAccountSearch", query, 25, [_card()], 1500),
    ]

    result = await _run(mock_page, browser, _account_search())

    assert browser.navigated == [url]
    assert "recentSearchParam" not in url
    assert result["url"] == url
    assert [card["id"] for card in result["accounts"]] == [COMPANY]
    assert result["sections"]["accounts"].startswith("Example Company (id 100000001)")
    assert result["paging"] == {
        "page": 2,
        "start": 25,
        "count": 25,
        "total": 1500,
        "resultCap": 1000,
        "capped": True,
        "more": True,
    }
    browser.facets.resolve_geo_urn.assert_not_awaited()


async def test_search_accounts_refuses_a_page_that_starts_elsewhere(mock_page):
    browser = _Browser(mock_page)
    query = build_search_query(KEYWORDS, ACCOUNT_FILTERS)
    browser.loads[search_page_url("company", query, 2)] = [
        _search("salesApiAccountSearch", query, 0, [_card()], 30)
    ]

    with pytest.raises(LinkedInScraperException, match="expected data in time"):
        await _run(mock_page, browser, _account_search())


async def test_search_accounts_refuses_results_missing_a_filter(mock_page):
    browser = _Browser(mock_page)
    query = build_search_query(KEYWORDS, ACCOUNT_FILTERS)
    dropped = build_search_query(
        KEYWORDS, [f for f in ACCOUNT_FILTERS if f.type != REGION]
    )
    browser.loads[search_page_url("company", query, 2)] = [
        _search("salesApiAccountSearch", dropped, 25, [_card()], 30)
    ]

    with pytest.raises(
        LinkedInScraperException, match="ignored the hq_location filter"
    ):
        await _run(mock_page, browser, _account_search())


async def test_search_refuses_a_page_that_left_sales_navigator(mock_page):
    browser = _Browser(mock_page)
    query = build_search_query(KEYWORDS, ACCOUNT_FILTERS)
    url = search_page_url("company", query, 2)
    browser.landings[url] = "https://www.linkedin.com/premium/products/?from=hidden"

    with pytest.raises(LinkedInScraperException) as raised:
        await _run(mock_page, browser, _account_search())

    message = str(raised.value)
    assert "did not open (LinkedIn showed /premium/products/)" in message
    assert "hidden" not in message


async def test_search_refuses_when_the_seat_check_answers_403(mock_page):
    browser = _Browser(mock_page)
    query = build_search_query(KEYWORDS, ACCOUNT_FILTERS)
    browser.loads[search_page_url("company", query, 2)] = [
        _response("salesApiEntitlements", status=403)
    ]

    with pytest.raises(
        LinkedInScraperException, match=r"refused this account \(HTTP 403\)"
    ):
        await _run(mock_page, browser, _account_search())


@pytest.mark.parametrize(
    "overrides, message",
    [
        (
            {
                "keywords": None,
                "industry": None,
                "hq_location": None,
                "size": None,
                "headcount_growth_min": None,
            },
            "at least one search criterion",
        ),
        ({"page": 41}, "page must be between 1 and 40"),
        ({"headcount_growth_max": 1}, "must not be greater"),
    ],
)
async def test_search_accounts_checks_input_before_any_page_load(
    mock_page, overrides, message
):
    browser = _Browser(mock_page)

    with pytest.raises(FilterValidationError, match=message):
        await _run(mock_page, browser, _account_search(**overrides))

    assert browser.events == []


# --- search_leads -----------------------------------------------------------


def _lead(member: str, name: str, **extra: Any) -> dict[str, Any]:
    return {
        "entityUrn": f"urn:li:fs_salesProfile:({member},NAME_SEARCH,abc)",
        "objectUrn": "urn:li:member:100000011",
        "fullName": name,
        "geoRegion": "Example region",
        "seniorityV2s": [{"id": 310, "displayName": "CXO"}],
        "currentPositions": [
            {
                "title": "Example title",
                "companyName": "Example Company",
                "companyUrn": f"urn:li:fs_salesCompany:{COMPANY}",
                "current": True,
            }
        ],
        **extra,
    }


async def test_search_leads_resolves_names_first_and_withholds_blocked_leads(
    mock_page, caplog
):
    browser = _Browser(mock_page)
    query = build_search_query(
        None,
        [
            SearchFilter(CURRENT_TITLE, texts=("head of growth",)),
            SearchFilter(SENIORITY_LEVEL, ids=("310",)),
            SearchFilter(
                CURRENT_COMPANY, ids=(f"urn:li:organization:{RESOLVED_COMPANY}",)
            ),
            SearchFilter(REGION, ids=(GEO,)),
        ],
    )
    url = search_page_url("people", query, 1)
    withheld = _lead("ACwAAA0002", "Withheld Person", blockThirdPartyDataSharing=True)
    browser.loads[url] = [
        *_boot(),
        _response("salesApiLeadSearch?q=organization&organizationId=1&start=0"),
        _search(
            "salesApiLeadSearch",
            query,
            0,
            [_lead("ACwAAA0001", "Example Person"), withheld],
            2,
        ),
    ]

    with caplog.at_level(logging.DEBUG):
        result = await _run(
            mock_page,
            browser,
            lambda r: r.search_leads(
                title=["head of growth"],
                seniority=["cxo"],
                current_company=["Example Company"],
                location=["Example region"],
            ),
        )

    assert browser.events == [
        "company Example Company",
        "geo Example region",
        f"load {url}",
    ]
    assert [lead["id"] for lead in result["leads"]] == ["ACwAAA0001"]
    assert result["withheld"]["count"] == 1
    assert "Withheld" not in repr(result)
    assert "ACwAAA0002" not in repr(result)
    assert "Withheld" not in caplog.text
    assert result["paging"]["more"] is False


async def test_search_leads_reports_no_withheld_when_none_were(mock_page):
    browser = _Browser(mock_page)
    query = build_search_query("example", [])
    url = search_page_url("people", query, 1)
    browser.loads[url] = [
        _search("salesApiLeadSearch", query, 0, [_lead("ACwAAA0001", "A")], 1)
    ]

    result = await _run(
        mock_page, browser, lambda r: r.search_leads(keywords="example")
    )

    assert "withheld" not in result
    assert result["sections"]["leads"].startswith("A (id ACwAAA0001)")


async def test_search_leads_refuses_an_unresolvable_location_before_loading(
    mock_page,
):
    browser = _Browser(mock_page)
    browser.geo["Nowhere"] = None

    with pytest.raises(FilterValidationError, match="Could not resolve location"):
        await _run(mock_page, browser, lambda r: r.search_leads(location=["Nowhere"]))

    assert browser.navigated == []


async def test_search_leads_refuses_an_unknown_seniority(mock_page):
    browser = _Browser(mock_page)

    with pytest.raises(FilterValidationError, match="Unknown seniority"):
        await _run(mock_page, browser, lambda r: r.search_leads(seniority=["chief"]))

    assert browser.events == []


# --- get_account ------------------------------------------------------------

ACCOUNT_URL = sales_company_url(COMPANY)


def _company_record(**fields: Any) -> SimpleNamespace:
    return _response(f"salesApiCompanies/{COMPANY}?decoration=example", fields)


def _profile_record(company_id: str = COMPANY) -> SimpleNamespace:
    return _company_record(
        entityUrn=f"urn:li:fs_salesCompany:{company_id}",
        name="Example Company",
        website="https://example.com",
        employees=[{"fullName": "Example Person"}],
    )


async def test_get_account_merges_both_decorations(mock_page):
    browser = _Browser(mock_page)
    browser.loads[ACCOUNT_URL] = [
        *_boot(),
        _profile_record(),
        _company_record(employeeCount=42, employeeDisplayCount="42"),
        _response(
            f"salesApiCompanies?action=latestOutreach&id={COMPANY}", method="POST"
        ),
    ]

    result = await _run(mock_page, browser, lambda r: r.get_account(COMPANY))

    account = result["account"]
    assert result["url"] == ACCOUNT_URL
    assert account["id"] == COMPANY
    assert account["name"] == "Example Company"
    assert account["employeeCount"] == 42
    assert "employees" not in account
    assert "section_errors" not in result
    assert browser.navigated == [ACCOUNT_URL]


async def test_get_account_reports_missing_headcount(mock_page):
    browser = _Browser(mock_page)
    browser.loads[ACCOUNT_URL] = [_profile_record()]

    result = await _run(
        mock_page, browser, lambda r: r.get_account(f"urn:li:fs_salesCompany:{COMPANY}")
    )

    assert result["section_errors"]["headcount"]["error_type"] == "unavailable"
    assert result["account"]["name"] == "Example Company"


async def test_get_account_refuses_a_record_of_another_company(mock_page):
    browser = _Browser(mock_page)
    browser.loads[ACCOUNT_URL] = [_profile_record("100000002")]

    with pytest.raises(LinkedInScraperException, match="does not recognise"):
        await _run(mock_page, browser, lambda r: r.get_account(COMPANY))


async def test_get_account_refused_by_linkedin_names_the_id(mock_page):
    browser = _Browser(mock_page)
    browser.loads[ACCOUNT_URL] = [
        _response(f"salesApiCompanies/{COMPANY}?decoration=example", status=404)
    ]

    with pytest.raises(InvalidReferenceError, match="refused that company_id"):
        await _run(mock_page, browser, lambda r: r.get_account(COMPANY))


# --- list_account_lists -----------------------------------------------------


def _lists(start: int, count: int, total: int) -> SimpleNamespace:
    return _response(
        "salesApiLists?q=listType&listType=ACCOUNT&isMetadataNeeded=true"
        f"&start={start}&count=25",
        {
            "elements": [
                {
                    "id": 100000400 + start + index,
                    "name": "Example list",
                    "listType": "ACCOUNT",
                    "entityCount": 3,
                }
                for index in range(count)
            ],
            "paging": {"start": start, "count": 25, "total": total},
        },
    )


LISTS_PAGE_2 = f"{SALES_ACCOUNT_LISTS_URL}?page=2"


async def test_list_account_lists_reads_every_page(mock_page):
    browser = _Browser(mock_page)
    browser.loads[SALES_ACCOUNT_LISTS_URL] = [
        *_boot(),
        _response("salesApiLists?q=listTypeForDefault&listType=ACCOUNT"),
        _lists(0, 25, 30),
    ]
    browser.loads[LISTS_PAGE_2] = [_lists(25, 5, 30)]

    result = await _run(mock_page, browser, lambda r: r.list_account_lists())

    assert browser.navigated == [SALES_ACCOUNT_LISTS_URL, LISTS_PAGE_2]
    assert result["url"] == SALES_ACCOUNT_LISTS_URL
    assert len(result["account_lists"]) == 30
    assert len(set(item["id"] for item in result["account_lists"])) == 30
    assert "section_errors" not in result


async def test_list_account_lists_stops_when_a_page_does_not_arrive(mock_page):
    browser = _Browser(mock_page)
    browser.loads[SALES_ACCOUNT_LISTS_URL] = [_lists(0, 25, 30)]

    result = await _run(mock_page, browser, lambda r: r.list_account_lists())

    assert len(result["account_lists"]) == 25
    error = result["section_errors"]["account_lists"]
    assert error["error_type"] == "page_limit"
    assert "reported 30 account lists and 25 were read" in error["error_message"]


# --- get_account_list -------------------------------------------------------

LIST_URL = sales_account_list_url(LIST)


def _table(start: int, count: int, total: int, columns: str) -> SimpleNamespace:
    return _response(
        f"salesApiDashboardAccountTable?q=list&listId={LIST}"
        f"&accountDashboardListType=CUSTOM_LISTS&start={start}&count=25"
        f"&columns=List({columns})",
        {
            "elements": [
                {
                    "companyInfo": {
                        "entityUrn": f"urn:li:fs_salesCompany:{100000500 + start + i}",
                        "name": "Example Company",
                    },
                    "recommendedLead": {"fullName": "Example Person"},
                }
                if columns == "UNIFIED_RECOMMENDED_LEAD"
                else {"crm": {}}
                for i in range(count)
            ],
            "paging": {"start": start, "count": 25, "total": total},
        },
    )


def _list_metadata(list_id: str = LIST) -> SimpleNamespace:
    return _response(
        f"salesApiLists/{LIST}?decoration=example",
        {"id": int(list_id), "name": "Example list", "entityCount": 27},
    )


async def test_get_account_list_reads_every_page(mock_page):
    browser = _Browser(mock_page)
    page_2 = f"{LIST_URL}&page=2"
    browser.loads[LIST_URL] = [
        *_boot(),
        _table(0, 25, 27, "CRM_OPPORTUNITY,NOTES"),
        _table(0, 25, 27, "UNIFIED_RECOMMENDED_LEAD"),
        _list_metadata(),
    ]
    browser.loads[page_2] = [
        _table(25, 2, 27, "UNIFIED_RECOMMENDED_LEAD"),
        _table(25, 2, 27, "CRM_OPPORTUNITY,NOTES"),
    ]

    result = await _run(mock_page, browser, lambda r: r.get_account_list(LIST_URL))

    assert browser.navigated == [LIST_URL, page_2]
    assert result["url"] == LIST_URL
    assert result["account_list"]["id"] == LIST
    assert len(result["accounts"]) == 27
    assert "recommendedLead" not in repr(result["accounts"])
    assert "section_errors" not in result


async def test_get_account_list_stops_when_the_total_moves(mock_page):
    browser = _Browser(mock_page)
    browser.loads[LIST_URL] = [
        _table(0, 25, 27, "UNIFIED_RECOMMENDED_LEAD"),
        _list_metadata(),
    ]
    browser.loads[f"{LIST_URL}&page=2"] = [
        _table(25, 3, 28, "UNIFIED_RECOMMENDED_LEAD")
    ]

    result = await _run(mock_page, browser, lambda r: r.get_account_list(LIST))

    assert len(result["accounts"]) == 25
    error = result["section_errors"]["accounts"]
    assert error["error_type"] == "page_limit"
    assert "changed to 28" in error["error_message"]


async def test_get_account_list_refuses_metadata_of_another_list(mock_page):
    browser = _Browser(mock_page)
    browser.loads[LIST_URL] = [
        _table(0, 1, 1, "UNIFIED_RECOMMENDED_LEAD"),
        _list_metadata("100000302"),
    ]

    with pytest.raises(LinkedInScraperException, match="does not recognise"):
        await _run(mock_page, browser, lambda r: r.get_account_list(LIST))
