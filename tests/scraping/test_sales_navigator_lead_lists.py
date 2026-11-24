"""Sales Navigator lead lists: listing them and reading their leads (synthetic data)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.scraping import sales_navigator as reader_module
from linkedin_mcp_server.scraping.identifiers import (
    SALES_LEAD_LISTS_URL,
    normalize_sales_list_id,
)
from linkedin_mcp_server.scraping.sales_navigator_lead_filters import LEAD_LIST
from linkedin_mcp_server.scraping.sales_navigator_payload import (
    SearchFilter,
    build_search_query,
    search_page_url,
)

from .test_sales_navigator import _boot, _Browser, _lead, _response, _run, _search

LEAD_LIST_ID = "100000402"
QUERY = build_search_query(None, [SearchFilter(LEAD_LIST, ids=(LEAD_LIST_ID,))])
SECONDS_PER_PAGE = 7.5


def _lead_lists(start: int, count: int, total: int, list_type: str = "LEAD"):
    return _response(
        f"salesApiLists?q=listType&listType={list_type}&isMetadataNeeded=true"
        f"&start={start}&count=25",
        {
            "elements": [
                {
                    "id": 100000500 + start + index,
                    "name": "Example lead list",
                    "listType": list_type,
                    "entityCount": 2,
                }
                for index in range(count)
            ],
            "paging": {"start": start, "count": 25, "total": total},
        },
    )


def _page_url(page: int) -> str:
    return search_page_url("people", QUERY, page)


def _leads_page(page: int, count: int, total: int, **extra: Any):
    start = (page - 1) * 25
    leads = [
        _lead(f"ACwAAA{start + index:04d}", f"Person {start + index}", **extra)
        for index in range(count)
    ]
    return _search("salesApiLeadSearch", QUERY, start, leads, total)


async def test_lead_lists_are_read_from_the_people_lists_page(mock_page):
    browser = _Browser(mock_page)
    # The page also lists account lists; only the LEAD response is read.
    browser.loads[SALES_LEAD_LISTS_URL] = [
        *_boot(),
        _lead_lists(0, 1, 1, list_type="ACCOUNT"),
        _lead_lists(0, 2, 2),
    ]

    result = await _run(mock_page, browser, lambda r: r.list_lead_lists())

    assert browser.navigated == [SALES_LEAD_LISTS_URL]
    assert [item["id"] for item in result["lead_lists"]] == ["100000500", "100000501"]
    assert {item["listType"] for item in result["lead_lists"]} == {"LEAD"}
    assert result["paging"]["total"] == 2
    assert "account_lists" not in result


async def test_a_lead_list_is_read_as_a_search_filtered_to_it(mock_page):
    browser = _Browser(mock_page)
    browser.loads[_page_url(1)] = [*_boot(), _leads_page(1, 25, 27, degree=1)]
    withheld = _lead("ACwAAA9999", "Withheld", blockThirdPartyDataSharing=True)
    browser.loads[_page_url(2)] = [
        _search(
            "salesApiLeadSearch",
            QUERY,
            25,
            [_lead("ACwAAA0025", "Person 25", degree=2), withheld],
            27,
        )
    ]

    result = await _run(mock_page, browser, lambda r: r.get_lead_list(LEAD_LIST_ID))

    assert browser.navigated == [_page_url(1), _page_url(2)]
    assert len(result["leads"]) == 26
    assert result["lead_list"] == {"id": LEAD_LIST_ID}
    assert result["withheld"]["count"] == 1
    assert "ACwAAA9999" not in repr(result)
    last = result["leads"][-1]
    assert last["degree"] == 2
    assert last["profileUrl"] == "https://www.linkedin.com/in/ACwAAA0025"
    assert last["salesProfileUrn"].startswith("urn:li:fs_salesProfile:(ACwAAA0025,")
    assert result["paging"] == {
        "page": 1,
        "start": 0,
        "count": 26,
        "total": 27,
        "resultCap": 1000,
        "capped": False,
        "more": False,
        "next_page": None,
    }
    assert "section_errors" not in result


async def test_a_page_that_does_not_load_returns_the_page_to_retry(mock_page):
    browser = _Browser(mock_page)
    browser.loads[_page_url(1)] = [*_boot(), _leads_page(1, 25, 30)]

    result = await _run(mock_page, browser, lambda r: r.get_lead_list(LEAD_LIST_ID))

    assert len(result["leads"]) == 25
    assert result["next_page"] == 2
    assert result["paging"]["more"] is True
    assert result["section_errors"]["leads"]["error_type"] == "page_limit"
    assert "page 2 did not load" in result["section_errors"]["leads"]["error_message"]


async def test_a_total_that_moves_stops_the_read_without_a_next_page(mock_page):
    browser = _Browser(mock_page)
    browser.loads[_page_url(1)] = [*_boot(), _leads_page(1, 25, 30)]
    browser.loads[_page_url(2)] = [_leads_page(2, 5, 31)]

    result = await _run(mock_page, browser, lambda r: r.get_lead_list(LEAD_LIST_ID))

    assert len(result["leads"]) == 25
    assert result["paging"]["more"] is True
    assert result["paging"]["next_page"] is None
    assert "total changed to 31" in result["section_errors"]["leads"]["error_message"]


class _Slow(_Browser):
    now = 0.0

    async def navigate(self, url: str) -> None:
        self.now += SECONDS_PER_PAGE
        await super().navigate(url)


async def _read_slowly(mock_page, deadline: float) -> tuple[_Slow, dict[str, Any]]:
    browser = _Slow(mock_page)
    for page in range(1, 5):
        browser.loads[_page_url(page)] = [_leads_page(page, 25, 100)]
    clock = SimpleNamespace(monotonic=lambda: browser.now)
    with patch.object(reader_module, "time", clock):
        result = await _run(
            mock_page,
            browser,
            lambda r: r.get_lead_list(LEAD_LIST_ID, deadline=deadline),
        )
    return browser, result


async def test_a_read_stops_at_a_page_boundary_before_the_deadline(mock_page):
    browser, result = await _read_slowly(mock_page, 20.0)

    # Two pages end at 15 s; a third would end at 22.5 s, past the deadline.
    assert len(browser.navigated) == 2
    assert result["next_page"] == 3
    assert result["paging"]["more"] is True
    assert "ran out of time" in result["section_errors"]["leads"]["error_message"]


async def test_the_second_page_is_not_started_when_it_would_end_late(mock_page):
    browser, result = await _read_slowly(mock_page, 10.0)

    # The first page ends at 7.5 s; the second would end at 15 s.
    assert len(browser.navigated) == 1
    assert result["next_page"] == 2


async def test_a_later_page_starts_at_its_own_offset(mock_page):
    browser = _Browser(mock_page)
    browser.loads[_page_url(2)] = [*_boot(), _leads_page(2, 5, 30)]

    result = await _run(
        mock_page, browser, lambda r: r.get_lead_list(LEAD_LIST_ID, page=2)
    )

    assert browser.navigated == [_page_url(2)]
    assert result["paging"]["start"] == 25
    assert result["paging"]["more"] is False


@pytest.mark.parametrize(
    "value",
    [
        LEAD_LIST_ID,
        f"https://www.linkedin.com/sales/lists/people/{LEAD_LIST_ID}",
        f"https://www.linkedin.com/sales/lists/people/{LEAD_LIST_ID}"
        "?sortCriteria=CREATED_TIME&sortOrder=DESCENDING",
    ],
)
def test_a_lead_list_id_or_url_names_the_list(value):
    assert normalize_sales_list_id(value) == LEAD_LIST_ID


@pytest.mark.parametrize(
    "value",
    [
        "https://www.linkedin.com/sales/lists/people/abc",
        "https://www.linkedin.com/sales/lists/people/1/2",
        "https://www.linkedin.com/sales/lists/people",
    ],
)
def test_a_lead_list_url_without_a_numeric_id_is_refused(value):
    with pytest.raises(InvalidReferenceError):
        normalize_sales_list_id(value)
