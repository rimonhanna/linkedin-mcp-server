"""A value passed both as an include and as its exclusion is refused before any search."""

from __future__ import annotations

from typing import Any

import pytest

from linkedin_mcp_server.scraping.contracts import FilterValidationError

from .test_sales_navigator import RESOLVED_COMPANY, _Browser, _run


@pytest.mark.parametrize(
    ("method", "arguments", "message"),
    [
        (
            "search_accounts",
            {"industry": ["Software Development"], "exclude_industry": ["4"]},
            "4 is in both industry and exclude_industry.",
        ),
        (
            "search_leads",
            {"industry": ["4"], "exclude_industry": ["4", "6"]},
            "4 is in both industry and exclude_industry.",
        ),
        (
            "search_leads",
            {
                "current_company": [RESOLVED_COMPANY],
                "exclude_current_company": ["Example Company"],
            },
            f"urn:li:organization:{RESOLVED_COMPANY} is in both current_company "
            "and exclude_current_company.",
        ),
        (
            "search_leads",
            {"lead_list": ["100000402"], "exclude_lead_list": ["100000402"]},
            "100000402 is in both lead_list and exclude_lead_list.",
        ),
    ],
    ids=["account-industry", "lead-industry", "lead-company", "lead-list"],
)
async def test_a_value_both_included_and_excluded_is_refused(
    mock_page, method: str, arguments: dict[str, Any], message: str
):
    browser = _Browser(mock_page)

    with pytest.raises(FilterValidationError) as error:
        await _run(mock_page, browser, lambda r: getattr(r, method)(**arguments))

    assert str(error.value) == f"{message} Pass each value in only one of them."
    assert browser.navigated == []
