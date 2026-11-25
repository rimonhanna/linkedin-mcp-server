"""The exact query each exclusion filter sends, and its echo check.

Golden strings from Sales Navigator's own UI (2026-11-25): an exclusion is a
value with ``selectionType:EXCLUDED`` on the same filter as the include. An
excluded company has no ``parent:(id:0)``, and its URN is percent-encoded once
inside ``query`` (encoded twice, Sales Navigator answered HTTP 500).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.scraping.sales_navigator_payload import (
    CURRENT_COMPANY,
    INDUSTRY,
    SearchFilter,
    build_search_query,
    missing_echoes,
    search_page_url,
)

from .test_sales_navigator import (
    RESOLVED_COMPANY,
    _boot,
    _Browser,
    _run,
    _search,
)

_RESOURCES = {"company": "salesApiAccountSearch", "people": "salesApiLeadSearch"}


def _sent_query(url: str) -> str:
    return parse_qs(urlparse(url).query)["query"][0]


class _Echoing(_Browser):
    """A page whose API request carries the query its URL was loaded with."""

    async def navigate(self, url: str) -> None:
        for kind, resource in _RESOURCES.items():
            if f"/sales/search/{kind}?" in url:
                self.loads[url] = [
                    *_boot(),
                    _search(resource, _sent_query(url), 0, [], 0),
                ]
        await super().navigate(url)


@pytest.mark.parametrize(
    ("method", "arguments", "query"),
    [
        (
            "search_accounts",
            {"exclude_industry": ["Software Development"]},
            "(filters:List((type:INDUSTRY,values:List("
            "(id:4,selectionType:EXCLUDED)))))",
        ),
        (
            "search_leads",
            {"exclude_industry": ["4"]},
            "(filters:List((type:INDUSTRY,values:List("
            "(id:4,selectionType:EXCLUDED)))))",
        ),
        (
            "search_leads",
            {"exclude_current_company": ["urn:li:organization:1033"]},
            "(filters:List((type:CURRENT_COMPANY,values:List("
            "(id:urn%3Ali%3Aorganization%3A1033,selectionType:EXCLUDED)))))",
        ),
        (
            "search_leads",
            {
                "current_company": ["100000001"],
                "exclude_current_company": ["Example Company"],
            },
            "(filters:List((type:CURRENT_COMPANY,values:List("
            "(id:urn%3Ali%3Aorganization%3A100000001,selectionType:INCLUDED,"
            "parent:(id:0)),"
            f"(id:urn%3Ali%3Aorganization%3A{RESOLVED_COMPANY},"
            "selectionType:EXCLUDED)))))",
        ),
        (
            "search_accounts",
            {"industry": ["6"], "exclude_industry": ["4"]},
            "(filters:List((type:INDUSTRY,values:List("
            "(id:6,selectionType:INCLUDED),(id:4,selectionType:EXCLUDED)))))",
        ),
    ],
    ids=[
        "account-industry",
        "lead-industry",
        "lead-company",
        "lead-company-include-and-exclude",
        "account-industry-include-and-exclude",
    ],
)
async def test_each_exclusion_sends_its_exact_query(
    mock_page, method: str, arguments: dict[str, Any], query: str
):
    browser = _Echoing(mock_page)

    await _run(mock_page, browser, lambda r: getattr(r, method)(**arguments))

    assert len(browser.navigated) == 1
    assert _sent_query(browser.navigated[0]) == query


def test_the_echo_check_misses_an_excluded_value_the_page_did_not_send():
    asked = [
        SearchFilter(
            CURRENT_COMPANY,
            ids=("urn:li:organization:100000001",),
            excluded_ids=("urn:li:organization:1033",),
        )
    ]
    included_only = build_search_query(
        None, [SearchFilter(CURRENT_COMPANY, ids=("urn:li:organization:100000001",))]
    )

    assert missing_echoes(None, asked, build_search_query(None, asked)) == []
    assert missing_echoes(None, asked, included_only) == [CURRENT_COMPANY]


async def test_a_page_that_dropped_an_exclusion_is_refused(mock_page):
    browser = _Browser(mock_page)
    asked = build_search_query(
        None, [SearchFilter(INDUSTRY, ids=("6",), excluded_ids=("4",))]
    )
    sent = build_search_query(None, [SearchFilter(INDUSTRY, ids=("6",))])
    url = search_page_url("company", asked, 1)
    browser.loads[url] = [
        *_boot(),
        _search("salesApiAccountSearch", sent, 0, [], 0),
    ]

    with pytest.raises(LinkedInScraperException) as error:
        await _run(
            mock_page,
            browser,
            lambda r: r.search_accounts(industry=["6"], exclude_industry=["4"]),
        )

    assert "dropped or rewrote the industry/exclude_industry filter" in str(error.value)
