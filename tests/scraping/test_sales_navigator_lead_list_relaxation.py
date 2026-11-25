"""A lead list read that Sales Navigator relaxed, or that serves more leads than the list holds."""

from __future__ import annotations

from linkedin_mcp_server.scraping.sales_navigator_lead_filters import LEAD_LIST
from linkedin_mcp_server.scraping.sales_navigator_payload import (
    SearchFilter,
    build_search_query,
    search_page_url,
)

from .test_sales_navigator import _boot, _Browser, _lead, _response, _run
from .test_sales_navigator_lead_lists import (
    LEAD_LIST_ID,
    QUERY,
    _leads_page,
    _own_lists,
    _page_url,
)


def _relaxed_page(page: int, total: int):
    start = (page - 1) * 25
    return _response(
        f"salesApiLeadSearch?q=searchQuery&query={QUERY}&start={start}&count=25"
        "&decorationId=example",
        {
            "metadata": {
                "appliedRelaxation": {"droppedFilters": [{"filterType": LEAD_LIST}]}
            },
            "elements": [_lead(f"ACwAAA{start:04d}", "Near Match")],
            "paging": {"start": start, "count": 25, "total": total},
        },
    )


async def test_a_relaxed_first_page_reads_no_leads(mock_page):
    browser = _Browser(mock_page)
    hub = _own_lists(browser, LEAD_LIST_ID, entity_count=2)
    browser.loads[_page_url(1)] = [*_boot(), _relaxed_page(1, 900)]

    result = await _run(mock_page, browser, lambda r: r.get_lead_list(LEAD_LIST_ID))

    assert browser.navigated == [hub, _page_url(1)]
    assert result["leads"] == []
    assert "Near Match" not in repr(result)
    assert result["paging"]["total"] == 0
    assert result["paging"]["more"] is False
    assert result["paging"]["next_page"] is None
    assert result["section_errors"] == {
        "leads": {
            "error_type": "filters_relaxed",
            "error_message": (
                "No leads match every filter. Sales Navigator dropped the "
                "lead_list filter to show 900 near matches instead, which are "
                "not reported. Widen or remove that filter to see them."
            ),
        }
    }


async def test_a_relaxed_later_page_drops_the_leads_already_read(mock_page):
    browser = _Browser(mock_page)
    _own_lists(browser, LEAD_LIST_ID, entity_count=30)
    browser.loads[_page_url(1)] = [*_boot(), _leads_page(1, 25, 30)]
    browser.loads[_page_url(2)] = [_relaxed_page(2, 30)]

    result = await _run(mock_page, browser, lambda r: r.get_lead_list(LEAD_LIST_ID))

    assert result["leads"] == []
    assert result["paging"]["total"] == 0
    assert result["section_errors"]["leads"]["error_type"] == "filters_relaxed"
    assert "lead_list" not in result["section_errors"]


async def test_a_search_serving_more_than_the_list_holds_is_flagged(mock_page):
    browser = _Browser(mock_page)
    _own_lists(browser, LEAD_LIST_ID, entity_count=2)
    browser.loads[_page_url(1)] = [*_boot(), _leads_page(1, 3, 3)]

    result = await _run(mock_page, browser, lambda r: r.get_lead_list(LEAD_LIST_ID))

    assert len(result["leads"]) == 3
    assert result["section_errors"]["lead_list"] == {
        "error_type": "unverified",
        "error_message": (
            "The list holds 2 leads but Sales Navigator's search reported 3, "
            "so the search may not have been limited to the list and some "
            "rows may not be in it."
        ),
    }


async def test_a_relaxed_exclude_only_search_names_exclude_lead_list(mock_page):
    browser = _Browser(mock_page)
    _own_lists(browser, LEAD_LIST_ID)
    query = build_search_query(
        None, [SearchFilter(LEAD_LIST, excluded_ids=(LEAD_LIST_ID,))]
    )
    browser.loads[search_page_url("people", query, 1)] = [
        *_boot(),
        _response(
            f"salesApiLeadSearch?q=searchQuery&query={query}&start=0&count=25"
            "&decorationId=example",
            {
                "metadata": {
                    "appliedRelaxation": {"droppedFilters": [{"filterType": LEAD_LIST}]}
                },
                "elements": [_lead("ACwAAA0001", "Near Match")],
                "paging": {"start": 0, "count": 25, "total": 5},
            },
        ),
    ]

    result = await _run(
        mock_page, browser, lambda r: r.search_leads(exclude_lead_list=[LEAD_LIST_ID])
    )

    assert result["leads"] == []
    assert (
        "dropped the exclude_lead_list filter"
        in (result["section_errors"]["leads"]["error_message"])
    )
