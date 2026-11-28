"""Account search's department headcount filter: validation, encoding, relaxation."""

from __future__ import annotations

from typing import Any

import pytest

from linkedin_mcp_server.scraping.contracts import FilterValidationError
from linkedin_mcp_server.scraping.sales_navigator_lead_filters import (
    DEPARTMENT_HEADCOUNT,
    department_headcount,
)
from linkedin_mcp_server.scraping.sales_navigator_payload import (
    SearchFilter,
    build_search_query,
    search_page_url,
)

from .test_sales_navigator import _boot, _Browser, _card, _response, _run

ENGINEERING_50_TO_500 = SearchFilter(
    DEPARTMENT_HEADCOUNT, ids=("8",), minimum=50, maximum=500
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"department": "Engineering", "min": 50, "max": 500}, ("8", 50, 500)),
        ({"department": "information technology", "min": 10}, ("13", 10, None)),
        ({"department": "13", "max": 0}, ("13", None, 0)),
        ({"department": "sales", "min": 5, "max": 5}, ("25", 5, 5)),
    ],
)
def test_department_headcount_resolves_the_department_and_bounds(value, expected):
    assert department_headcount(value) == expected


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"department": "Underwater Basketry", "min": 1}, "Unknown function"),
        ({"department": "99", "min": 1}, "Unknown function"),
        ({"department": " ", "min": 1}, "needs a department"),
        ({"min": 1}, "needs a department"),
        ({"department": "engineering"}, "needs a min or a max"),
        (
            {"department": "engineering", "min": 500, "max": 50},
            "min must not be greater than its max",
        ),
        ({"department": "engineering", "min": -1}, "min must be a whole number"),
        ({"department": "engineering", "max": -5}, "max must be a whole number"),
        ({"department": "engineering", "min": True}, "min must be a whole number"),
    ],
)
def test_department_headcount_refuses_a_bad_value(value, message):
    with pytest.raises(FilterValidationError, match=message):
        department_headcount(value)


def test_department_headcount_is_sent_as_an_id_with_a_range():
    assert build_search_query(None, [ENGINEERING_50_TO_500]) == (
        "(filters:List((type:DEPARTMENT_HEADCOUNT,"
        "values:List((id:8,selectionType:INCLUDED)),"
        "rangeValue:(min:50,max:500))))"
    )


def _page(query: str, metadata: dict[str, Any], total: int):
    return _response(
        "salesApiAccountSearch?q=searchQuery"
        f"&query={query}&start=0&count=25&decorationId=example",
        {
            "metadata": metadata,
            "elements": [_card()],
            "paging": {"start": 0, "count": 25, "total": total},
        },
    )


def _search(r):
    return r.search_accounts(
        department_headcount={"department": "engineering", "min": 50, "max": 500}
    )


async def test_an_account_search_sends_the_department_headcount(mock_page):
    browser = _Browser(mock_page)
    query = build_search_query(None, [ENGINEERING_50_TO_500])
    url = search_page_url("company", query, 1)
    browser.loads[url] = [*_boot(), _page(query, {}, 1)]

    result = await _run(mock_page, browser, _search)

    assert browser.navigated == [url]
    assert [card["id"] for card in result["accounts"]] == ["100000001"]


async def test_a_relaxed_department_headcount_is_named_in_filters_relaxed(
    mock_page,
):
    browser = _Browser(mock_page)
    query = build_search_query(None, [ENGINEERING_50_TO_500])
    url = search_page_url("company", query, 1)
    relaxed = {
        "appliedRelaxation": {"droppedFilters": [{"filterType": DEPARTMENT_HEADCOUNT}]}
    }
    browser.loads[url] = [*_boot(), _page(query, relaxed, 12)]

    result = await _run(mock_page, browser, _search)

    assert result["accounts"] == []
    note = result["section_errors"]["accounts"]
    assert note["error_type"] == "filters_relaxed"
    assert "dropped the department_headcount filter" in note["error_message"]


async def test_an_account_search_refuses_a_bad_department_before_loading(
    mock_page,
):
    browser = _Browser(mock_page)

    with pytest.raises(FilterValidationError, match="Unknown function"):
        await _run(
            mock_page,
            browser,
            lambda r: r.search_accounts(
                department_headcount={"department": "nope", "min": 1}
            ),
        )
    assert browser.navigated == []
