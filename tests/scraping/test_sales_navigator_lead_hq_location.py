"""Lead search's company_hq_location: the query it sends, and its echo check.

Golden strings, measured against Sales Navigator's own filter panel on
2026-11-27 (Company headquarters location = United Kingdom): a changed filter
type or value shape changes what Sales Navigator is asked, and the echo check
cannot catch that, because it compares the page's request against the same
builder.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.scraping.contracts import FilterValidationError

from .test_sales_navigator import GEO, _boot, _Browser, _run, _search

UK = "101165590"


def _sent_query(url: str) -> str:
    return parse_qs(urlparse(url).query)["query"][0]


class _Echoing(_Browser):
    """A page whose API request carries the query its URL was loaded with."""

    def __init__(self, page, rewrite=None):
        super().__init__(page)
        self.rewrite = rewrite or (lambda query: query)

    async def navigate(self, url: str) -> None:
        if "/sales/search/" in url:
            self.loads[url] = [
                *_boot(),
                _search("salesApiLeadSearch", self.rewrite(_sent_query(url)), 0, [], 0),
            ]
        await super().navigate(url)


@pytest.mark.parametrize("value", [UK, f"urn:li:geo:{UK}"])
async def test_a_geo_id_is_sent_as_the_hq_filter_without_a_lookup(mock_page, value):
    browser = _Echoing(mock_page)

    await _run(
        mock_page, browser, lambda r: r.search_leads(company_hq_location=[value])
    )

    assert [_sent_query(url) for url in browser.navigated] == [
        f"(filters:List((type:COMPANY_HEADQUARTERS,values:List("
        f"(id:{UK},selectionType:INCLUDED)))))"
    ]
    browser.facets.resolve_geo_urn.assert_not_awaited()


async def test_a_place_name_is_resolved_like_location(mock_page):
    browser = _Echoing(mock_page)

    await _run(
        mock_page,
        browser,
        lambda r: r.search_leads(company_hq_location=["United Kingdom"], location=[UK]),
    )

    assert browser.events.count("geo United Kingdom") == 1
    assert _sent_query(browser.navigated[-1]) == (
        f"(filters:List((type:REGION,values:List((id:{UK},selectionType:INCLUDED))),"
        f"(type:COMPANY_HEADQUARTERS,values:List((id:{GEO},selectionType:INCLUDED)))))"
    )


async def test_an_unresolved_place_is_refused_naming_the_parameter(mock_page):
    browser = _Echoing(mock_page)
    browser.geo["Nowhere"] = None

    with pytest.raises(FilterValidationError, match="company_hq_location 'Nowhere'"):
        await _run(
            mock_page,
            browser,
            lambda r: r.search_leads(company_hq_location=["Nowhere"]),
        )


async def test_a_page_that_drops_the_hq_filter_is_an_error(mock_page):
    browser = _Echoing(mock_page, rewrite=lambda _query: "()")

    with pytest.raises(LinkedInScraperException, match="company_hq_location"):
        await _run(
            mock_page, browser, lambda r: r.search_leads(company_hq_location=[UK])
        )


async def test_a_page_that_sends_it_as_the_lead_location_is_an_error(mock_page):
    browser = _Echoing(
        mock_page, rewrite=lambda q: q.replace("COMPANY_HEADQUARTERS", "REGION")
    )

    with pytest.raises(LinkedInScraperException, match="company_hq_location"):
        await _run(
            mock_page, browser, lambda r: r.search_leads(company_hq_location=[UK])
        )
