"""A boost account's record refuses only that account (synthetic data).

A business account's page can load another account's record; a post-2024
personal boost account seen under a different id must not stop a read or a
write of the account the tool was asked about.
"""

from __future__ import annotations

from scraping import test_campaign_manager as reader_tests
from scraping import test_campaign_manager_audiences as audience_tests
from scraping.test_campaign_manager_boost_account_tools import CUTOFF, _personal

OTHER = "100000077"


async def test_a_business_account_lists_beside_another_ids_boost_record(mock_page):
    browser = reader_tests._Browser(mock_page)
    url = reader_tests._url("/campaigns")
    browser.loads[url] = [
        _personal(OTHER, created=CUTOFF),
        reader_tests._account_record(),
        reader_tests._groups(reader_tests.GROUP),
        *reader_tests._campaign_elements(),
    ]

    result = await reader_tests._run(
        mock_page, browser, lambda r: r.list_campaigns(reader_tests.ACCOUNT)
    )

    assert result["campaigns"][0]["id"] == int(reader_tests.CAMPAIGN)
    assert result["ad_accounts"][0]["id"] == int(reader_tests.ACCOUNT)
    assert "section_errors" not in result


async def test_a_business_account_write_proceeds_beside_another_ids_boost_record(
    mock_page,
):
    browser = audience_tests._Browser(mock_page)
    browser.loads[audience_tests.MATCHED] = [
        [_personal(OTHER, created=CUTOFF), audience_tests._matched()]
    ]

    with browser.running(), audience_tests._dialog(mock_page) as calls:
        result = await audience_tests._audiences(
            mock_page
        ).create_company_list_audience(
            audience_tests.ACCOUNT,
            "Example list",
            audience_tests.COMPANIES,
            confirm_send=False,
        )

    assert result["status"] == "confirmation_required"
    assert (calls.submitted, calls.closed) == (0, 1)
