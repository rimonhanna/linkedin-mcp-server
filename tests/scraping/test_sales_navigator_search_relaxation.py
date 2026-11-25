"""A search Sales Navigator relaxed by dropping an asked filter reports no rows.

Sample shape from a live lead search (2026-11-25, sanitised): no lead matched
every filter, so Sales Navigator answered 27 near matches with the network
filter removed and named it in ``metadata.appliedRelaxation``.
"""

from __future__ import annotations

from typing import Any

from linkedin_mcp_server.scraping.sales_navigator_lead_filters import RELATIONSHIP
from linkedin_mcp_server.scraping.sales_navigator_payload import (
    INDUSTRY,
    REGION,
    SearchFilter,
    build_search_query,
    dropped_filters,
    search_page_url,
)

from .test_sales_navigator import (
    GEO,
    _boot,
    _Browser,
    _card,
    _lead,
    _response,
    _run,
)

RELAXED_METADATA = {
    "totalDisplayCount": "27",
    "appliedRelaxation": {"droppedFilters": [{"filterType": "RELATIONSHIP"}]},
    "suggestedFilters": [],
    "filters": [],
}


def _page(
    resource: str, query: str, elements: list, total: int, metadata: dict[str, Any]
):
    return _response(
        f"{resource}?q=searchQuery&query={query}&start=0&count=25&decorationId=example",
        {
            "metadata": metadata,
            "elements": elements,
            "paging": {"start": 0, "count": 25, "total": total},
        },
    )


def _lead_search(browser: _Browser, metadata: dict[str, Any]) -> str:
    query = build_search_query(
        None,
        [SearchFilter(REGION, ids=(GEO,)), SearchFilter(RELATIONSHIP, ids=("F",))],
    )
    url = search_page_url("people", query, 1)
    browser.loads[url] = [
        *_boot(),
        _page(
            "salesApiLeadSearch",
            query,
            [_lead("ACwAAA0001", "Near Match")],
            27,
            metadata,
        ),
    ]
    return url


def _leads(r):
    return r.search_leads(location=[f"urn:li:geo:{GEO}"], network=["F"])


def test_dropped_filters_reads_the_relaxation_metadata():
    assert dropped_filters({"metadata": RELAXED_METADATA}) == ["RELATIONSHIP"]


def test_dropped_filters_is_empty_without_relaxation():
    assert dropped_filters({"metadata": {"totalDisplayCount": "0"}}) == []
    assert dropped_filters({"elements": []}) == []
    assert dropped_filters(None) == []


async def test_a_lead_search_relaxed_past_an_asked_filter_reports_no_leads(
    mock_page,
):
    browser = _Browser(mock_page)
    _lead_search(browser, RELAXED_METADATA)

    result = await _run(mock_page, browser, _leads)

    assert result["leads"] == []
    assert "leads" not in result["sections"]
    assert "Near Match" not in repr(result)
    assert result["paging"]["total"] == 0
    assert result["paging"]["count"] == 0
    assert result["paging"]["more"] is False
    note = result["section_errors"]["leads"]
    assert note["error_type"] == "filters_relaxed"
    assert note["error_message"] == (
        "No leads match every filter. Sales Navigator dropped the network "
        "filter to show 27 near matches instead, which are not reported. "
        "Widen or remove that filter to see them."
    )


async def test_a_lead_search_without_relaxation_returns_its_rows(mock_page):
    browser = _Browser(mock_page)
    _lead_search(browser, {"totalDisplayCount": "27"})

    result = await _run(mock_page, browser, _leads)

    assert [lead["id"] for lead in result["leads"]] == ["ACwAAA0001"]
    assert result["paging"]["total"] == 27
    assert "section_errors" not in result


async def test_a_dropped_filter_that_was_not_asked_keeps_the_rows(mock_page):
    browser = _Browser(mock_page)
    _lead_search(
        browser,
        {"appliedRelaxation": {"droppedFilters": [{"filterType": "FUNCTION"}]}},
    )

    result = await _run(mock_page, browser, _leads)

    assert [lead["id"] for lead in result["leads"]] == ["ACwAAA0001"]
    assert "section_errors" not in result


async def test_an_account_search_relaxed_past_an_exclusion_reports_no_accounts(
    mock_page,
):
    browser = _Browser(mock_page)
    query = build_search_query(
        None,
        [
            SearchFilter(INDUSTRY, ids=("6",), excluded_ids=("4",)),
            SearchFilter(REGION, ids=(GEO,)),
        ],
    )
    url = search_page_url("company", query, 1)
    browser.loads[url] = [
        *_boot(),
        _page(
            "salesApiAccountSearch",
            query,
            [_card()],
            12,
            {
                "appliedRelaxation": {
                    "droppedFilters": [
                        {"filterType": "INDUSTRY"},
                        {"filterType": "REGION"},
                    ]
                }
            },
        ),
    ]

    result = await _run(
        mock_page,
        browser,
        lambda r: r.search_accounts(
            industry=["6"], exclude_industry=["4"], hq_location=[GEO]
        ),
    )

    assert result["accounts"] == []
    assert "accounts" not in result["sections"]
    assert result["paging"]["total"] == 0
    assert result["section_errors"]["accounts"] == {
        "error_type": "filters_relaxed",
        "error_message": (
            "No accounts match every filter. Sales Navigator dropped the "
            "industry/exclude_industry and hq_location filters to show 12 "
            "near matches instead, which are not reported. Widen or remove "
            "those filters to see them."
        ),
    }


async def test_an_account_search_without_relaxation_returns_its_rows(mock_page):
    browser = _Browser(mock_page)
    query = build_search_query(None, [SearchFilter(INDUSTRY, excluded_ids=("4",))])
    url = search_page_url("company", query, 1)
    browser.loads[url] = [
        *_boot(),
        _page("salesApiAccountSearch", query, [_card()], 1, {}),
    ]

    result = await _run(
        mock_page, browser, lambda r: r.search_accounts(exclude_industry=["4"])
    )

    assert [card["id"] for card in result["accounts"]] == ["100000001"]
    assert result["paging"]["total"] == 1
    assert "section_errors" not in result
