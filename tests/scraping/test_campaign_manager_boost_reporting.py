"""Which personal accounts Campaign Manager shows reporting for (synthetic data).

Its account route refuses a ``PERSONAL`` account whose reference is a member
and that was made on or after 2024-01-01 UTC; any other account, an older
personal one included, gets the reporting table as usual.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from linkedin_mcp_server.scraping.campaign_manager import _boost_without_reporting

from scraping.test_campaign_manager import (
    ACCOUNT,
    CAMPAIGN,
    CURRENT,
    _analytics,
    _Browser,
    _response,
    _row,
    _run,
    _statistics_body,
    _url,
)

CUTOFF = 1_704_067_200_000
MEMBER = "urn:li:member:100000099"


def _personal(**fields: Any) -> dict[str, Any]:
    return {
        "id": int(ACCOUNT),
        "type": "PERSONAL",
        "currencyCode": "EUR",
        "reference": MEMBER,
        "createdAt": CUTOFF,
        **fields,
    }


@pytest.mark.parametrize(
    ("record", "refused"),
    [
        (_personal(), True),
        (_personal(createdAt=CUTOFF - 1), False),
        (_personal(type="BUSINESS"), False),
        (_personal(reference="urn:li:organization:100000098"), False),
        (_personal(reference="urn:li:person:example"), False),
        (_personal(reference=None), False),
        (_personal(createdAt=None), False),
    ],
)
def test_only_a_new_member_owned_personal_account_lacks_reporting(record, refused):
    assert _boost_without_reporting(record) is refused


def _record(**fields: Any) -> SimpleNamespace:
    return _response(
        f"campaignManagerAccounts/{ACCOUNT}",
        {"data": _personal(**fields), "included": []},
    )


async def test_an_older_personal_account_is_read_from_its_reporting_table(mock_page):
    browser = _Browser(mock_page)
    url = _url("/campaigns")
    browser.loads[url] = [
        _record(createdAt=CUTOFF - 1),
        _analytics(
            _statistics_body(CURRENT), [_row(f"urn:li:sponsoredCampaign:{CAMPAIGN}", 4)]
        ),
    ]

    result = await _run(mock_page, browser, lambda r: r.get_campaign_analytics(ACCOUNT))

    (row,) = result["analytics"]["rows"]
    assert row["impressions"] == 4
    assert result["url"] == url
