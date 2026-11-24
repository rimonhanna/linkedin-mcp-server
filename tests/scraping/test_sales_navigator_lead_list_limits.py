"""Where a lead list read stops, and what it keeps (synthetic data)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from linkedin_mcp_server.exceptions import ActionLimitError

from .test_sales_navigator import _boot, _Browser, _lead, _response, _run
from .test_sales_navigator_lead_lists import (
    LEAD_LIST_ID,
    QUERY,
    _leads_page,
    _page_url,
)

RESUME_AT = datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc)


class _Capped(_Browser):
    """A pacing cap that refuses page ``refused_from`` and every page after."""

    def __init__(self, page, refused_from: int) -> None:
        super().__init__(page)
        self.refused = {_page_url(n) for n in range(refused_from, 41)}

    async def navigate(self, url: str) -> None:
        if url in self.refused:
            raise ActionLimitError(
                "search", limit=100, window="24 h", resume_at=RESUME_AT
            )
        await super().navigate(url)


async def test_a_cap_on_a_later_page_keeps_the_leads_already_read(mock_page):
    browser = _Capped(mock_page, refused_from=3)
    browser.loads[_page_url(1)] = [*_boot(), _leads_page(1, 25, 80)]
    browser.loads[_page_url(2)] = [_leads_page(2, 25, 80)]

    result = await _run(mock_page, browser, lambda r: r.get_lead_list(LEAD_LIST_ID))

    assert browser.navigated == [_page_url(1), _page_url(2)]
    assert len(result["leads"]) == 50
    error = result["section_errors"]["leads"]
    assert error["error_type"] == "limit_exceeded"
    assert (error["kind"], error["limit"], error["window"]) == ("search", 100, "24 h")
    assert error["resume_at"] == RESUME_AT.isoformat()
    assert error["next_page"] == 3
    assert result["next_page"] == 3
    assert result["paging"]["more"] is True
    assert result["paging"]["next_page"] == 3


async def test_a_cap_on_the_first_page_is_raised(mock_page):
    browser = _Capped(mock_page, refused_from=1)

    with pytest.raises(ActionLimitError):
        await _run(mock_page, browser, lambda r: r.get_lead_list(LEAD_LIST_ID))


async def test_one_call_reads_at_most_twenty_pages(mock_page):
    browser = _Browser(mock_page)
    browser.loads[_page_url(1)] = [*_boot(), _leads_page(1, 25, 1000)]
    for page in range(2, 41):
        browser.loads[_page_url(page)] = [_leads_page(page, 25, 1000)]

    result = await _run(mock_page, browser, lambda r: r.get_lead_list(LEAD_LIST_ID))

    assert browser.navigated == [_page_url(n) for n in range(1, 21)]
    assert len(result["leads"]) == 500
    assert result["next_page"] == 21
    assert result["paging"]["more"] is True
    message = result["section_errors"]["leads"]["error_message"]
    assert "one call reads at most 20 pages" in message


async def test_no_page_past_the_last_search_page_is_loaded(mock_page):
    browser = _Browser(mock_page)
    # Paging that claims more rows after page 40, which a search never has.
    last = _response(
        f"salesApiLeadSearch?q=searchQuery&query={QUERY}&start=975&count=25",
        {
            "elements": [_lead("ACwAAA0975", "Person 975")],
            "paging": {"start": 975, "count": 10, "total": 5000},
        },
    )
    browser.loads[_page_url(40)] = [*_boot(), last]

    result = await _run(
        mock_page, browser, lambda r: r.get_lead_list(LEAD_LIST_ID, page=40)
    )

    assert browser.navigated == [_page_url(40)]
    assert "next_page" not in result
    assert "at most 1,000" in result["section_errors"]["leads"]["error_message"]


async def test_a_short_read_is_never_more_without_an_error(mock_page):
    browser = _Browser(mock_page)
    # Paging says the page covers the list, but it carried fewer rows.
    short = _response(
        f"salesApiLeadSearch?q=searchQuery&query={QUERY}&start=0&count=25",
        {
            "elements": [_lead("ACwAAA0000", "Person 0")],
            "paging": {"start": 0, "count": 30, "total": 30},
        },
    )
    browser.loads[_page_url(1)] = [*_boot(), short]

    result = await _run(mock_page, browser, lambda r: r.get_lead_list(LEAD_LIST_ID))

    assert result["paging"]["more"] is True
    assert result["paging"]["next_page"] is None
    error = result["section_errors"]["leads"]
    assert error["error_type"] == "page_limit"
    assert "the pages returned fewer rows" in error["error_message"]


async def test_the_list_record_on_the_first_page_confirms_the_list(mock_page):
    browser = _Browser(mock_page)
    browser.loads[_page_url(1)] = [
        *_boot(),
        _response(
            f"salesApiLists/{LEAD_LIST_ID}?decoration=example",
            {"id": int(LEAD_LIST_ID), "name": "Example lead list", "entityCount": 2},
        ),
        _leads_page(1, 2, 2),
    ]

    result = await _run(mock_page, browser, lambda r: r.get_lead_list(LEAD_LIST_ID))

    assert result["lead_list"] == {
        "id": LEAD_LIST_ID,
        "name": "Example lead list",
        "entityCount": 2,
    }


async def test_another_lists_record_does_not_confirm_this_one(mock_page):
    browser = _Browser(mock_page)
    browser.loads[_page_url(1)] = [
        *_boot(),
        _response(
            "salesApiLists/100000999?decoration=example",
            {"id": 100000999, "name": "Other list", "entityCount": 9},
        ),
        _leads_page(1, 0, 0),
    ]

    result = await _run(mock_page, browser, lambda r: r.get_lead_list(LEAD_LIST_ID))

    assert result["lead_list"] == {"id": LEAD_LIST_ID}
    assert result["leads"] == []
