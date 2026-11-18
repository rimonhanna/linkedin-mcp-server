"""An account-wide analytics query may name the account by id or by URN (synthetic data)."""

from __future__ import annotations

import pytest

from linkedin_mcp_server.core.exceptions import LinkedInScraperException

from scraping.test_campaign_manager import (
    ACCOUNT,
    CAMPAIGN,
    CURRENT,
    _analytics,
    _Browser,
    _row,
    _run,
    _statistics_body,
    _url,
)

URN = f"urn%3Ali%3AsponsoredAccount%3A{ACCOUNT}"


async def test_an_account_urn_scope_is_this_accounts_whole_table(mock_page):
    browser = _Browser(mock_page)
    url = _url("/campaigns")
    body = _statistics_body(CURRENT).replace(
        f"accountIds=List({ACCOUNT})", f"accountIds=List({URN})"
    )
    browser.loads[url] = [
        _analytics(body, [_row(f"urn:li:sponsoredCampaign:{CAMPAIGN}", 6)])
    ]

    result = await _run(mock_page, browser, lambda r: r.get_campaign_analytics(ACCOUNT))

    assert result["analytics"]["rows"][0]["impressions"] == 6
    assert "section_errors" not in result


@pytest.mark.parametrize(
    "scope",
    [
        "List(urn%3Ali%3AsponsoredAccount%3A100000002)",
        f"List({URN},100000002)",
        f"List(urn%3Ali%3AsponsoredCampaign%3A{ACCOUNT})",
    ],
)
async def test_an_account_urn_scope_for_more_or_other_accounts_is_not_read(
    mock_page, scope
):
    browser = _Browser(mock_page)
    url = _url("/campaigns")
    body = _statistics_body(CURRENT).replace(
        f"accountIds=List({ACCOUNT})", f"accountIds={scope}"
    )
    browser.loads[url] = [_analytics(body, [])]

    with pytest.raises(LinkedInScraperException, match="none was the query"):
        await _run(mock_page, browser, lambda r: r.get_campaign_analytics(ACCOUNT))
