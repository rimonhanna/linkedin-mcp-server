"""Every per-account tool stops at once on a boost account (synthetic data).

Campaign Manager refuses its account route for a ``PERSONAL`` account whose
reference is a member and that was made on or after 2024-01-01 UTC, so a read
says there is nothing to list and a write that nothing was changed, from the
account record the page loads, instead of waiting out a timeout. An older
personal account is read and written as before.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.scraping import campaign_manager_api as api_module

from scraping import test_campaign_manager as reader_tests
from scraping import test_campaign_manager_audiences as audience_tests

CUTOFF = 1_704_067_200_000
MEMBER = "urn:li:member:100000099"


def _personal(account: str, *, created: int) -> SimpleNamespace:
    return reader_tests._response(
        f"campaignManagerAccounts/{account}",
        {
            "data": {
                "id": int(account),
                "name": "Example boost account",
                "status": "ACTIVE",
                "type": "PERSONAL",
                "currencyCode": "EUR",
                "reference": MEMBER,
                "createdAt": created,
            },
            "included": [],
        },
    )


async def test_a_read_on_a_boost_account_says_there_is_nothing_to_list(mock_page):
    browser = reader_tests._Browser(mock_page)
    url = reader_tests._url("/campaigns")
    browser.loads[url] = [_personal(reader_tests.ACCOUNT, created=CUTOFF)]

    with (
        patch.object(api_module, "note_throttle_signal") as signal,
        pytest.raises(InvalidReferenceError) as exc,
    ):
        await reader_tests._run(
            mock_page, browser, lambda r: r.list_campaigns(reader_tests.ACCOUNT)
        )

    assert "personal boost account" in str(exc.value)
    assert "managed with the boosted post" in str(exc.value)
    assert "nothing to list" in str(exc.value)
    signal.assert_not_called()
    assert browser.navigated == [url]


async def test_a_read_that_takes_a_miss_for_none_still_reports_the_boost_account(
    mock_page,
):
    browser = audience_tests._Browser(mock_page)
    browser.loads[audience_tests.MATCHED] = [
        [_personal(audience_tests.ACCOUNT, created=CUTOFF)]
    ]

    with browser.running(), pytest.raises(InvalidReferenceError) as exc:
        await audience_tests._audiences(mock_page).list_audiences(
            audience_tests.ACCOUNT
        )

    assert "nothing to list" in str(exc.value)


async def test_a_write_on_a_boost_account_says_nothing_was_changed(mock_page):
    browser = audience_tests._Browser(mock_page)
    browser.loads[audience_tests.MATCHED] = [
        [_personal(audience_tests.ACCOUNT, created=CUTOFF)]
    ]

    with (
        browser.running(),
        audience_tests._dialog(mock_page) as calls,
        pytest.raises(InvalidReferenceError) as exc,
    ):
        await audience_tests._audiences(mock_page).create_company_list_audience(
            audience_tests.ACCOUNT,
            "Example list",
            audience_tests.COMPANIES,
            confirm_send=True,
        )

    assert "personal boost account" in str(exc.value)
    assert "nothing was changed" in str(exc.value)
    assert calls.submitted == 0


async def test_an_older_personal_account_is_still_listed(mock_page):
    browser = reader_tests._Browser(mock_page)
    url = reader_tests._url("/campaigns")
    browser.loads[url] = [
        _personal(reader_tests.ACCOUNT, created=CUTOFF - 1),
        reader_tests._groups(reader_tests.GROUP),
        *reader_tests._campaign_elements(),
    ]

    result = await reader_tests._run(
        mock_page, browser, lambda r: r.list_campaigns(reader_tests.ACCOUNT)
    )

    assert result["campaigns"][0]["id"] == int(reader_tests.CAMPAIGN)
    assert "section_errors" not in result


async def test_an_older_personal_account_still_opens_a_write(mock_page):
    browser = audience_tests._Browser(mock_page)
    older = _personal(audience_tests.ACCOUNT, created=CUTOFF - 1)
    browser.loads[audience_tests.MATCHED] = [[older, audience_tests._matched()]]

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
