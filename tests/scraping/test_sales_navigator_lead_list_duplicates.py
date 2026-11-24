"""A lead list's short-read check counts unique leads (synthetic data)."""

from __future__ import annotations

from .test_sales_navigator import _boot, _Browser, _lead, _run, _search
from .test_sales_navigator_lead_lists import (
    LEAD_LIST_ID,
    QUERY,
    _leads_page,
    _page_url,
)


def _page(ids: list[str], start: int, total: int):
    leads = [_lead(member, f"Person {member}") for member in ids]
    return _search("salesApiLeadSearch", QUERY, start, leads, total)


async def test_a_lead_repeated_across_pages_is_not_a_short_read(mock_page):
    browser = _Browser(mock_page)
    browser.loads[_page_url(1)] = [*_boot(), _leads_page(1, 25, 30)]
    repeated = ["ACwAAA0024", *(f"ACwAAA{n:04d}" for n in range(25, 29))]
    browser.loads[_page_url(2)] = [_page(repeated, 25, 30)]

    result = await _run(mock_page, browser, lambda r: r.get_lead_list(LEAD_LIST_ID))

    message = result["section_errors"]["leads"]["error_message"]
    assert "page 2 repeated a lead" in message
    assert "fewer rows" not in message


async def test_a_lead_repeated_on_one_page_is_counted_once(mock_page):
    browser = _Browser(mock_page)
    # 25 rows for a list of 25, but one lead twice: one lead was not read.
    ids = [f"ACwAAA{n:04d}" for n in range(24)] + ["ACwAAA0000"]
    browser.loads[_page_url(1)] = [*_boot(), _page(ids, 0, 25)]

    result = await _run(mock_page, browser, lambda r: r.get_lead_list(LEAD_LIST_ID))

    error = result["section_errors"]["leads"]
    assert error["error_type"] == "page_limit"
    assert "the pages returned fewer rows" in error["error_message"]


async def test_a_repeated_lead_with_every_lead_read_is_not_short(mock_page):
    browser = _Browser(mock_page)
    ids = [f"ACwAAA{n:04d}" for n in range(24)] + ["ACwAAA0000"]
    browser.loads[_page_url(1)] = [*_boot(), _page(ids, 0, 24)]

    result = await _run(mock_page, browser, lambda r: r.get_lead_list(LEAD_LIST_ID))

    assert "leads" not in result.get("section_errors", {})
