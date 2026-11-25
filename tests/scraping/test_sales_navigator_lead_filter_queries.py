"""The exact query each ICP lead filter sends, and the whole function table.

Golden strings: a changed filter type, id or selection type changes what
Sales Navigator is asked, and the echo check cannot catch it because it
compares the page's request against the same builder.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from linkedin_mcp_server.scraping.sales_navigator_lead_filters import (
    FUNCTION_IDS,
    function_ids,
)

from .test_sales_navigator import _boot, _Browser, _run, _search
from .test_sales_navigator_lead_lists import _own_lists


def _sent_query(url: str) -> str:
    return parse_qs(urlparse(url).query)["query"][0]


class _Echoing(_Browser):
    """A page whose API request carries the query its URL was loaded with."""

    async def navigate(self, url: str) -> None:
        if "/sales/search/" in url:
            self.loads[url] = [
                *_boot(),
                _search("salesApiLeadSearch", _sent_query(url), 0, [], 0),
            ]
        await super().navigate(url)


@pytest.mark.parametrize(
    ("arguments", "query"),
    [
        (
            {"network": ["F", "S"]},
            "(filters:List((type:RELATIONSHIP,values:List("
            "(id:F,selectionType:INCLUDED),(id:S,selectionType:INCLUDED)))))",
        ),
        (
            {"function": ["engineering", "26"]},
            "(filters:List((type:FUNCTION,values:List("
            "(id:8,selectionType:INCLUDED),(id:26,selectionType:INCLUDED)))))",
        ),
        (
            {"company_size": ["11-50", "I"]},
            "(filters:List((type:COMPANY_HEADCOUNT,values:List("
            "(id:C,selectionType:INCLUDED),(id:I,selectionType:INCLUDED)))))",
        ),
        (
            {"years_in_current_role": ["less_than_1", "more_than_10"]},
            "(filters:List((type:YEARS_IN_CURRENT_POSITION,values:List("
            "(id:1,selectionType:INCLUDED),(id:5,selectionType:INCLUDED)))))",
        ),
        (
            {"years_at_current_company": ["3_to_5"]},
            "(filters:List((type:YEARS_AT_CURRENT_COMPANY,values:List("
            "(id:3,selectionType:INCLUDED)))))",
        ),
        (
            {"account_list": ["100000401"]},
            "(filters:List((type:ACCOUNT_LIST,values:List("
            "(id:100000401,selectionType:INCLUDED)))))",
        ),
        (
            {"lead_list": ["100000402"]},
            "(filters:List((type:LEAD_LIST,values:List("
            "(id:100000402,selectionType:INCLUDED)))))",
        ),
        (
            {"exclude_lead_list": ["100000403"]},
            "(filters:List((type:LEAD_LIST,values:List("
            "(id:100000403,selectionType:EXCLUDED)))))",
        ),
    ],
)
async def test_each_icp_filter_sends_its_exact_query(
    mock_page, arguments: dict[str, Any], query: str
):
    browser = _Echoing(mock_page)
    lists = arguments.get("account_list") or [
        *arguments.get("lead_list", ()),
        *arguments.get("exclude_lead_list", ()),
    ]
    list_type = "ACCOUNT" if "account_list" in arguments else "LEAD"
    hub = _own_lists(browser, *lists, list_type=list_type)

    await _run(mock_page, browser, lambda r: r.search_leads(**arguments))

    searches = [url for url in browser.navigated if url != hub]
    assert len(searches) == 1
    assert _sent_query(searches[0]) == query


def test_the_function_table_is_complete_and_exact():
    assert dict(FUNCTION_IDS) == {
        "accounting": "1",
        "administrative": "2",
        "arts_and_design": "3",
        "business_development": "4",
        "community_and_social_services": "5",
        "consulting": "6",
        "education": "7",
        "engineering": "8",
        "entrepreneurship": "9",
        "finance": "10",
        "healthcare_services": "11",
        "human_resources": "12",
        "information_technology": "13",
        "legal": "14",
        "marketing": "15",
        "media_and_communication": "16",
        "military_and_protective_services": "17",
        "operations": "18",
        "product_management": "19",
        "program_and_project_management": "20",
        "purchasing": "21",
        "quality_assurance": "22",
        "real_estate": "23",
        "research": "24",
        "sales": "25",
        "customer_success_and_support": "26",
    }


@pytest.mark.parametrize(("name", "function_id"), sorted(FUNCTION_IDS.items()))
def test_every_function_name_and_its_id_map_to_that_id(name, function_id):
    assert function_ids([name, function_id]) == [function_id, function_id]
