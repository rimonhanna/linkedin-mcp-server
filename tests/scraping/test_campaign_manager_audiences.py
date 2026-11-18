"""Tests for the Campaign Manager audience workflows (synthetic data only).

The page is the fake the read tests use: page views emit scripted responses,
and the dialog steps are replaced by doubles that push the app's requests
through the real write guard. The dialog steps themselves run against
synthetic markup in ``test_campaign_manager_audiences_dom.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.scraping import campaign_manager_audiences as audiences
from linkedin_mcp_server.scraping import campaign_manager_guard as guard_module
from linkedin_mcp_server.scraping import campaign_manager_write as write_module
from linkedin_mcp_server.scraping.campaign_manager_audiences import CampaignAudiences
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

CM = "https://www.linkedin.com/campaign-manager-api/"
ACCOUNT = "100000001"
CAMPAIGN = "100000301"
APP = f"https://www.linkedin.com/campaignmanager/accounts/{ACCOUNT}"
MATCHED = f"{APP}/audiences/matched"
DETAILS = f"{APP}/campaigns/{CAMPAIGN}/details"
SETUP = f"{APP}/campaigns/new/setup?isNewAdvertiser=true"
TERMS = "https://www.linkedin.com/legal/sas-terms"
NEW_ID = "100000501"
TITLES = "urn:li:adTargetingFacet:titles"
LOCATIONS = "urn:li:adTargetingFacet:locations"
TARGETING = {
    "include": {
        "and": [
            {
                "or": [
                    {
                        "facet": {"urn": LOCATIONS},
                        "segments": [{"urn": "urn:li:geo:100000901"}],
                    }
                ]
            },
            {
                "or": [
                    {
                        "facet": {"urn": TITLES},
                        "segments": [{"urn": "urn:li:title:100000911"}],
                    }
                ]
            },
        ]
    },
    "exclude": {"or": []},
}


def _response(
    path: str,
    payload: Any = None,
    *,
    status: int = 200,
    method: str = "GET",
    body: Any = None,
    restli: str | None = None,
    created_id: str | None = None,
) -> SimpleNamespace:
    headers = {"x-restli-method": restli} if restli else {}
    request = SimpleNamespace(
        method=method,
        url=CM + path,
        headers={},
        post_data=json.dumps(body) if body is not None else None,
        all_headers=AsyncMock(return_value=headers),
    )
    answer = {"x-restli-id": created_id} if created_id else {}
    return SimpleNamespace(
        url=CM + path,
        status=status,
        request=request,
        json=AsyncMock(return_value=payload),
        all_headers=AsyncMock(return_value=answer),
    )


def _emit(page: Any, responses: list[SimpleNamespace]) -> None:
    for response in responses:
        for callback in list(page.listeners.get("response", [])):
            callback(response)


def _matched(*elements: dict, account: str = ACCOUNT, status: int = 200):
    return _response(
        f"campaignManagerMatchedAudiences?q=account&accountId={account}",
        {"elements": list(elements), "paging": {"count": 10, "start": 0}},
        status=status,
    )


def _saved(*elements: dict):
    return _response(
        f"campaignManagerTargetingCriteria?q=member&accountId={ACCOUNT}",
        {"elements": list(elements), "paging": {"count": 10, "start": 0}},
    )


def _list_entry(audience_id: int, name: str, status: str = "BUILDING") -> dict:
    return {
        "id": audience_id,
        "name": name,
        "segmentType": "COMPANY",
        "maSegmentType": "BULK",
        "maSegmentStatus": status,
    }


def _saved_entry(audience_id: int, name: str) -> dict:
    return {
        "type": "USER_AUDIENCE",
        "id": audience_id,
        "name": name,
        "ownership": "ACCOUNT",
        "audienceCount": 4000,
    }


TEMPLATE = {"type": "LINKEDIN_AUDIENCE", "id": -3, "name": "Example template"}


def _campaign(targeting: Any = TARGETING) -> SimpleNamespace:
    return _response(
        f"campaignManagerCampaigns/{CAMPAIGN}",
        {
            "data": {
                "id": int(CAMPAIGN),
                "accountId": int(ACCOUNT),
                "name": "Example ad set",
                "status": "DRAFT",
                "targetingCriteria": targeting,
            },
            "included": [],
        },
    )


class _Browser:
    """Script what each page view and the saved tab click emit."""

    def __init__(self, page: Any) -> None:
        self.page = page
        self.loads: dict[str, list[list[SimpleNamespace]]] = {}
        self.redirects: set[str] = set()
        self.navigated: list[str] = []
        self.tab_waves: list[list[SimpleNamespace]] = []
        self.tab_count = 1
        tab = MagicMock()
        tab.count = AsyncMock(side_effect=lambda: self.tab_count)
        tab.click = AsyncMock(side_effect=self._tab)
        page.locator = MagicMock(return_value=tab)
        self.tab = tab

    async def _tab(self) -> None:
        _emit(self.page, self.tab_waves.pop(0) if self.tab_waves else [])

    async def navigate(self, url: str) -> None:
        self.navigated.append(url)
        self.page.url = SETUP if url in self.redirects else url
        self.page.main_frame = SimpleNamespace(url=self.page.url)
        waves = self.loads.get(url, [])
        _emit(self.page, waves.pop(0) if waves else [])

    @contextmanager
    def running(self) -> Iterator[None]:
        with (
            patch.object(
                PageNavigator, "_navigate_to_page", AsyncMock(side_effect=self.navigate)
            ),
            patch.object(ScrapingSession, "check_rate_limit", new_callable=AsyncMock),
            patch.object(ScrapingSession, "dismiss_modal", new_callable=AsyncMock),
            patch.object(audiences, "_READY_TIMEOUT", 0.2),
            patch.object(audiences, "_UPLOAD_TIMEOUT", 0.3),
            patch.object(audiences, "_SAVE_TIMEOUT", 0.3),
            patch.object(write_module, "_READY_TIMEOUT", 0.2),
        ):
            yield


def _audiences(page: Any) -> CampaignAudiences:
    session = ScrapingSession(page)
    return CampaignAudiences(session, PageNavigator(session))


def _handler(page: Any) -> Any:
    (handler,) = [
        call.args[1]
        for call in page.route.await_args_list
        if call.args[0] == "**/campaign-manager-api/**"
    ]
    return handler


async def _app_sends(page: Any, response: SimpleNamespace) -> bool:
    """The app sends one write: through the guard, answered only if it passed."""
    outcome: list[str] = []
    route = SimpleNamespace(
        request=response.request,
        abort=AsyncMock(side_effect=lambda: outcome.append("abort")),
        fallback=AsyncMock(side_effect=lambda: outcome.append("fallback")),
    )
    await _handler(page)(route)
    if outcome == ["fallback"]:
        _emit(page, [response])
        return True
    return False


def _signed_url(account: str = ACCOUNT, status: int = 201) -> SimpleNamespace:
    return _response(
        "campaignManagerAmbrySignedUrl",
        None,
        method="POST",
        status=status,
        body={
            "owner": f"urn:li:sponsoredAccount:{account}",
            "requestType": "UPLOAD",
            "uploadType": "MATCHED_AUDIENCE",
            "contentType": "csv",
        },
        restli="create",
        created_id="https://www.linkedin.com/dms-uploads/example",
    )


def _segment(name: str, *, status: int = 201) -> SimpleNamespace:
    return _response(
        "campaignManagerDmpSegments",
        None,
        method="POST",
        status=status,
        body={
            "inputFileName": "example.csv",
            "inputFile": "urn:li:media:example",
            "account": f"urn:li:sponsoredAccount:{ACCOUNT}",
            "name": name,
            "sourcePlatform": "LIST_UPLOAD",
            "segmentType": "COMPANY",
        },
        restli="create",
        created_id=NEW_ID if status < 300 else None,
    )


def _saved_create(name: str, targeting: Any = TARGETING) -> SimpleNamespace:
    return _response(
        "campaignManagerTargetingCriteria",
        None,
        method="POST",
        status=201,
        body={
            "name": name,
            "include": targeting["include"],
            "exclude": targeting["exclude"],
            "account": f"urn:li:sponsoredAccount:{ACCOUNT}",
            "ownership": "ACCOUNT",
        },
        restli="create",
        created_id=NEW_ID,
    )


@contextmanager
def _dialog(
    page: Any,
    *,
    sends: list[SimpleNamespace] | None = None,
    opened: Any = "dialog",
    terms: str | None = TERMS,
    filled: bool = True,
) -> Iterator[SimpleNamespace]:
    """Replace the dialog steps; the submit makes the app send ``sends``."""
    calls = SimpleNamespace(submitted=0, closed=0, admitted=[])

    async def submit(_dialog: Any, _button: Any) -> bool:
        calls.submitted += 1
        for response in sends or []:
            calls.admitted.append(await _app_sends(page, response))
        return True

    async def close(_page: Any, _dialog: Any) -> None:
        calls.closed += 1

    with (
        patch.object(audiences, "_open_upload_dialog", AsyncMock(return_value=opened)),
        patch.object(audiences, "_upload_terms_url", AsyncMock(return_value=terms)),
        patch.object(audiences, "_fill_upload_form", AsyncMock(return_value=filled)),
        patch.object(audiences, "_open_save_dialog", AsyncMock(return_value=opened)),
        patch.object(audiences, "_fill_save_dialog", AsyncMock(return_value=filled)),
        patch.object(audiences, "_upload_submit", MagicMock()),
        patch.object(audiences, "_save_submit", MagicMock()),
        patch.object(audiences, "_submit", AsyncMock(side_effect=submit)),
        patch.object(audiences, "_close_dialog", AsyncMock(side_effect=close)),
    ):
        yield calls


COMPANIES = [
    {"name": "Example Co", "linkedin_url": "https://www.linkedin.com/company/example"},
    {"name": "Sample Ltd", "website": "https://sample.example"},
]


# --- list_audiences ------------------------------------------------------------


async def test_list_audiences_reads_both_tabs_latched(mock_page):
    browser = _Browser(mock_page)
    browser.loads[MATCHED] = [[_matched(_list_entry(100000401, "Example list"))]]
    browser.tab_waves = [[_saved(TEMPLATE, _saved_entry(100000402, "Example saved"))]]

    with browser.running():
        result = await _audiences(mock_page).list_audiences(ACCOUNT)

    assert [a["id"] for a in result["matched_audiences"]] == [100000401]
    assert result["matched_audiences"][0]["segmentUrn"] == "urn:li:adSegment:100000401"
    # LinkedIn's own templates are not the account's audiences.
    assert [a["id"] for a in result["saved_audiences"]] == [100000402]
    assert set(result["sections"]) == {"matched_audiences", "saved_audiences"}
    assert "section_errors" not in result
    mock_page.locator.assert_called_with(audiences._SAVED_TAB)
    assert guard_module._GUARDS[mock_page].read_mode is True


async def test_list_audiences_reports_a_missing_tab_and_keeps_the_other(mock_page):
    browser = _Browser(mock_page)
    browser.loads[MATCHED] = [[_matched()]]
    browser.tab_count = 0

    with browser.running():
        result = await _audiences(mock_page).list_audiences(ACCOUNT)

    assert result["matched_audiences"] == []
    assert "saved_audiences" not in result
    assert result["section_errors"]["saved_audiences"]["error_type"] == "unavailable"


async def test_list_audiences_ignores_another_accounts_list(mock_page):
    browser = _Browser(mock_page)
    browser.loads[MATCHED] = [[_matched(_list_entry(1, "Other"), account="100000002")]]

    with browser.running():
        result = await _audiences(mock_page).list_audiences(ACCOUNT)

    assert set(result["section_errors"]) == {"matched_audiences", "saved_audiences"}
    assert "matched_audiences" not in result


async def test_list_audiences_refused_account_is_an_invalid_reference(mock_page):
    browser = _Browser(mock_page)
    browser.loads[MATCHED] = [[_matched(status=403)]]

    with browser.running(), pytest.raises(InvalidReferenceError):
        await _audiences(mock_page).list_audiences(ACCOUNT)


# --- create_company_list_audience ---------------------------------------------


@pytest.mark.parametrize(
    ("name", "companies"),
    [("", COMPANIES), ("x" * 201, COMPANIES), ("Example", []), ("Ex\x01", COMPANIES)],
)
async def test_upload_refuses_invalid_input_before_the_browser(
    mock_page, name, companies
):
    browser = _Browser(mock_page)

    with browser.running():
        result = await _audiences(mock_page).create_company_list_audience(
            ACCOUNT, name, companies, confirm_send=True
        )

    assert result["status"] == "invalid_change"
    assert result["retry_safe"] is True
    assert browser.navigated == []


async def test_upload_dry_run_states_the_agreement_and_never_submits(mock_page):
    browser = _Browser(mock_page)
    browser.loads[MATCHED] = [[_matched()]]

    with browser.running(), _dialog(mock_page) as calls:
        result = await _audiences(mock_page).create_company_list_audience(
            ACCOUNT, "Example list", COMPANIES, confirm_send=False
        )

    assert result["status"] == "confirmation_required"
    assert result["retry_safe"] is True
    assert "LinkedIn Ads Agreement" in result["message"]
    assert TERMS in result["message"]
    assert result["planned"] == {
        "name": "Example list",
        "listType": "COMPANY",
        "companies": 2,
        "fileName": "example-list.csv",
        "termsUrl": TERMS,
        "acceptsAdsAgreement": True,
    }
    assert result["target"]["id"] == int(ACCOUNT)
    assert (calls.submitted, calls.closed) == (0, 1)
    assert guard_module._GUARDS[mock_page].admission is None


async def test_upload_hands_the_built_csv_to_the_form(mock_page):
    browser = _Browser(mock_page)
    browser.loads[MATCHED] = [[_matched()]]

    with browser.running(), _dialog(mock_page):
        await _audiences(mock_page).create_company_list_audience(
            ACCOUNT, "Example list", COMPANIES, confirm_send=False
        )
        fill = cast("AsyncMock", audiences._fill_upload_form)

    call = fill.await_args
    assert call is not None
    _page, _dialog_arg, name, file_name, data = call.args
    assert (name, file_name) == ("Example list", "example-list.csv")
    lines = data.decode("utf-8").split("\r\n")
    assert lines[0].startswith("companyname,companywebsite,")
    assert lines[1].startswith("Example Co,,,https://www.linkedin.com/company/example/")


async def test_upload_creates_and_verifies_by_the_created_id(mock_page):
    browser = _Browser(mock_page)
    browser.loads[MATCHED] = [
        [_matched()],
        [_matched(_list_entry(int(NEW_ID), "Example list"))],
    ]
    sends = [_signed_url(), _segment("Example list")]

    with browser.running(), _dialog(mock_page, sends=sends) as calls:
        result = await _audiences(mock_page).create_company_list_audience(
            ACCOUNT, "Example list", COMPANIES, confirm_send=True
        )

    assert calls.admitted == [True, True]
    assert result["status"] == "audience_created"
    assert result["retry_safe"] is False
    assert result["created_id"] == NEW_ID
    assert result["applied"]["status"] == "BUILDING"
    assert "LinkedIn Ads Agreement" in result["message"]
    assert TERMS in result["message"]
    assert "refused" not in result
    assert browser.navigated == [MATCHED, MATCHED]
    assert guard_module._GUARDS[mock_page].read_mode is True


async def test_upload_off_plan_write_is_blocked_and_safe_to_retry(mock_page):
    browser = _Browser(mock_page)
    browser.loads[MATCHED] = [[_matched()]]
    billing = _response(
        f"campaignManagerAccounts/{ACCOUNT}/billingConfiguration?action=findOrCreate",
        method="POST",
        body={},
    )

    with browser.running(), _dialog(mock_page, sends=[billing]) as calls:
        result = await _audiences(mock_page).create_company_list_audience(
            ACCOUNT, "Example list", COMPANIES, confirm_send=True
        )

    assert calls.admitted == [False]
    assert result["status"] == "change_blocked"
    assert result["retry_safe"] is True
    assert result["refused"][0]["resource"] == "campaignManagerAccounts"


async def test_upload_refuses_a_second_audience_create(mock_page):
    browser = _Browser(mock_page)
    browser.loads[MATCHED] = [
        [_matched()],
        [_matched(_list_entry(int(NEW_ID), "Example list"))],
    ]
    sends = [_signed_url(), _segment("Example list"), _segment("Example list")]

    with browser.running(), _dialog(mock_page, sends=sends) as calls:
        result = await _audiences(mock_page).create_company_list_audience(
            ACCOUNT, "Example list", COMPANIES, confirm_send=True
        )

    assert calls.admitted == [True, True, False]
    assert result["status"] == "audience_created"
    assert [r["reason"] for r in result["refused"]] == [
        "second campaignManagerDmpSegments request"
    ]


async def test_upload_refused_by_linkedin_is_save_failed(mock_page):
    browser = _Browser(mock_page)
    browser.loads[MATCHED] = [[_matched()]]
    sends = [_signed_url(), _segment("Example list", status=400)]

    with browser.running(), _dialog(mock_page, sends=sends):
        result = await _audiences(mock_page).create_company_list_audience(
            ACCOUNT, "Example list", COMPANIES, confirm_send=True
        )

    assert result["status"] == "save_failed"
    assert result["retry_safe"] is True


async def test_upload_failed_signed_url_ends_the_wait(mock_page):
    browser = _Browser(mock_page)
    browser.loads[MATCHED] = [[_matched()]]

    with (
        browser.running(),
        patch.object(audiences, "_UPLOAD_TIMEOUT", 5.0),
        _dialog(mock_page, sends=[_signed_url(status=403)]),
    ):
        result = await _audiences(mock_page).create_company_list_audience(
            ACCOUNT, "Example list", COMPANIES, confirm_send=True
        )

    assert result["status"] == "save_failed"


async def test_upload_created_but_missing_from_the_list_is_unverified(mock_page):
    browser = _Browser(mock_page)
    browser.loads[MATCHED] = [[_matched()], [_matched()]]
    sends = [_signed_url(), _segment("Example list")]

    with browser.running(), _dialog(mock_page, sends=sends):
        result = await _audiences(mock_page).create_company_list_audience(
            ACCOUNT, "Example list", COMPANIES, confirm_send=True
        )

    assert result["status"] == "save_unverified"
    assert result["retry_safe"] is False
    assert result["created_id"] == NEW_ID


async def test_upload_sent_without_an_answer_is_unverified(mock_page):
    browser = _Browser(mock_page)
    browser.loads[MATCHED] = [[_matched()]]

    with browser.running(), _dialog(mock_page, sends=[_signed_url()]):
        result = await _audiences(mock_page).create_company_list_audience(
            ACCOUNT, "Example list", COMPANIES, confirm_send=True
        )

    assert result["status"] == "save_unverified"
    assert result["retry_safe"] is False


async def test_upload_submit_that_sends_nothing_is_control_unavailable(mock_page):
    browser = _Browser(mock_page)
    browser.loads[MATCHED] = [[_matched()]]

    with browser.running(), _dialog(mock_page, sends=[]):
        result = await _audiences(mock_page).create_company_list_audience(
            ACCOUNT, "Example list", COMPANIES, confirm_send=True
        )

    assert result["status"] == "control_unavailable"
    assert result["retry_safe"] is True


@pytest.mark.parametrize(
    ("opened", "terms"),
    [(None, TERMS), ("dialog", None)],
    ids=["no-dialog", "no-terms"],
)
async def test_upload_without_the_full_form_is_unavailable(mock_page, opened, terms):
    browser = _Browser(mock_page)
    browser.loads[MATCHED] = [[_matched()]]

    with browser.running(), _dialog(mock_page, opened=opened, terms=terms) as calls:
        result = await _audiences(mock_page).create_company_list_audience(
            ACCOUNT, "Example list", COMPANIES, confirm_send=True
        )

    assert result["status"] == "upload_form_unavailable"
    assert result["retry_safe"] is True
    assert calls.submitted == 0


async def test_upload_on_a_setup_redirect_is_unavailable(mock_page):
    browser = _Browser(mock_page)
    browser.redirects.add(MATCHED)

    with browser.running(), _dialog(mock_page) as calls:
        result = await _audiences(mock_page).create_company_list_audience(
            ACCOUNT, "Example list", COMPANIES, confirm_send=True
        )

    assert result["status"] == "upload_form_unavailable"
    assert calls.submitted == 0


async def test_upload_refused_account_is_target_not_found(mock_page):
    browser = _Browser(mock_page)
    browser.loads[MATCHED] = [[_matched(status=404)]]

    with browser.running(), _dialog(mock_page) as calls:
        result = await _audiences(mock_page).create_company_list_audience(
            ACCOUNT, "Example list", COMPANIES, confirm_send=True
        )

    assert result["status"] == "target_not_found"
    assert calls.submitted == 0


# --- create_saved_audience ----------------------------------------------------


async def test_saved_audience_name_over_25_characters_is_refused(mock_page):
    browser = _Browser(mock_page)

    with browser.running():
        result = await _audiences(mock_page).create_saved_audience(
            ACCOUNT, CAMPAIGN, "x" * 26, confirm_send=True
        )

    assert result["status"] == "invalid_change"
    assert browser.navigated == []


async def test_saved_audience_dry_run_plans_the_ad_sets_targeting(mock_page):
    browser = _Browser(mock_page)
    browser.loads[DETAILS] = [[_campaign()]]

    with browser.running(), _dialog(mock_page) as calls:
        result = await _audiences(mock_page).create_saved_audience(
            ACCOUNT, CAMPAIGN, "Example saved", confirm_send=False
        )

    assert result["status"] == "confirmation_required"
    assert result["planned"]["targeting"] == [
        {
            "section": "include_locations",
            "facetUrn": LOCATIONS,
            "segmentUrn": "urn:li:geo:100000901",
        },
        {
            "section": "include_or:0",
            "facetUrn": TITLES,
            "segmentUrn": "urn:li:title:100000911",
        },
    ]
    assert result["target"]["id"] == int(CAMPAIGN)
    assert (calls.submitted, calls.closed) == (0, 1)


async def test_saved_audience_disabled_save_is_audience_too_small(mock_page):
    browser = _Browser(mock_page)
    browser.loads[DETAILS] = [[_campaign()]]

    with browser.running(), _dialog(mock_page, opened=False) as calls:
        result = await _audiences(mock_page).create_saved_audience(
            ACCOUNT, CAMPAIGN, "Example saved", confirm_send=True
        )

    assert result["status"] == "audience_too_small"
    assert result["retry_safe"] is True
    assert calls.submitted == 0


async def test_saved_audience_creates_and_verifies_in_the_saved_tab(mock_page):
    browser = _Browser(mock_page)
    browser.loads[DETAILS] = [[_campaign()]]
    browser.loads[MATCHED] = [[_matched()]]
    browser.tab_waves = [[_saved(TEMPLATE, _saved_entry(int(NEW_ID), "Example saved"))]]

    with (
        browser.running(),
        _dialog(mock_page, sends=[_saved_create("Example saved")]) as calls,
    ):
        result = await _audiences(mock_page).create_saved_audience(
            ACCOUNT, CAMPAIGN, "Example saved", confirm_send=True
        )

    assert calls.admitted == [True]
    assert result["status"] == "audience_created"
    assert result["retry_safe"] is False
    assert result["created_id"] == NEW_ID
    assert result["applied"]["name"] == "Example saved"
    assert browser.navigated == [DETAILS, MATCHED]


async def test_saved_audience_with_other_targeting_is_blocked(mock_page):
    browser = _Browser(mock_page)
    browser.loads[DETAILS] = [[_campaign()]]
    other = {
        "include": {
            "and": [
                {
                    "or": [
                        {
                            "facet": {"urn": TITLES},
                            "segments": [{"urn": "urn:li:title:100000912"}],
                        }
                    ]
                }
            ]
        },
        "exclude": {"or": []},
    }

    with (
        browser.running(),
        _dialog(mock_page, sends=[_saved_create("Example saved", other)]) as calls,
    ):
        result = await _audiences(mock_page).create_saved_audience(
            ACCOUNT, CAMPAIGN, "Example saved", confirm_send=True
        )

    assert calls.admitted == [False]
    assert result["status"] == "change_blocked"
    assert result["retry_safe"] is True


async def test_saved_audience_admits_one_create_only(mock_page):
    browser = _Browser(mock_page)
    browser.loads[DETAILS] = [[_campaign()]]
    browser.loads[MATCHED] = [[_matched()]]
    browser.tab_waves = [[_saved(_saved_entry(int(NEW_ID), "Example saved"))]]
    sends = [_saved_create("Example saved"), _saved_create("Example saved")]

    with browser.running(), _dialog(mock_page, sends=sends) as calls:
        result = await _audiences(mock_page).create_saved_audience(
            ACCOUNT, CAMPAIGN, "Example saved", confirm_send=True
        )

    assert calls.admitted == [True, False]
    assert result["status"] == "audience_created"
    assert [r["reason"] for r in result["refused"]] == ["second saved audience create"]


async def test_saved_audience_found_under_another_name_is_unverified(mock_page):
    browser = _Browser(mock_page)
    browser.loads[DETAILS] = [[_campaign()]]
    browser.loads[MATCHED] = [[_matched()]]
    browser.tab_waves = [[_saved(_saved_entry(int(NEW_ID), "Other name"))]]

    with (
        browser.running(),
        _dialog(mock_page, sends=[_saved_create("Example saved")]),
    ):
        result = await _audiences(mock_page).create_saved_audience(
            ACCOUNT, CAMPAIGN, "Example saved", confirm_send=True
        )

    assert result["status"] == "save_unverified"
    assert result["retry_safe"] is False


async def test_saved_audience_of_another_account_is_target_not_found(mock_page):
    browser = _Browser(mock_page)
    record = _campaign()
    record.json.return_value["data"]["accountId"] = 100000002
    browser.loads[DETAILS] = [[record]]

    with browser.running(), _dialog(mock_page) as calls:
        result = await _audiences(mock_page).create_saved_audience(
            ACCOUNT, CAMPAIGN, "Example saved", confirm_send=True
        )

    assert result["status"] == "target_not_found"
    assert calls.submitted == 0


# --- helpers -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("100000501", 100000501),
        ("urn:li:adSegment:100000501", 100000501),
        (None, None),
        ("", None),
        ("12a", None),
        ("urn:li:adSegment:", None),
    ],
)
def test_created_number(value, expected):
    assert audiences._created_number(value) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [("Example list", "example-list.csv"), ("***", "company-list.csv")],
)
def test_file_name(name, expected):
    assert audiences._file_name(name) == expected


def _app_saved_create(name: str, **extra: Any) -> SimpleNamespace:
    """The save audience modal's create as the app bundle builds it."""
    return _response(
        "campaignManagerTargetingCriteria",
        None,
        method="POST",
        status=201,
        body={
            "name": name,
            "description": "",
            "include": TARGETING["include"],
            "exclude": TARGETING["exclude"],
            "account": f"urn:li:sponsoredAccount:{ACCOUNT}",
            **extra,
        },
        created_id=NEW_ID,
    )


async def test_saved_audience_admits_the_apps_create_without_ownership(mock_page):
    browser = _Browser(mock_page)
    browser.loads[DETAILS] = [[_campaign()]]
    browser.loads[MATCHED] = [[_matched()]]
    browser.tab_waves = [[_saved(_saved_entry(int(NEW_ID), "Example saved"))]]

    with (
        browser.running(),
        _dialog(mock_page, sends=[_app_saved_create("Example saved")]) as calls,
    ):
        result = await _audiences(mock_page).create_saved_audience(
            ACCOUNT, CAMPAIGN, "Example saved", confirm_send=True
        )

    assert calls.admitted == [True]
    assert result["status"] == "audience_created"


async def test_blocked_saved_audience_reports_the_refused_structure(mock_page):
    browser = _Browser(mock_page)
    browser.loads[DETAILS] = [[_campaign()]]
    sends = [_app_saved_create("Example saved", audienceCount=100000777)]

    with browser.running(), _dialog(mock_page, sends=sends) as calls:
        result = await _audiences(mock_page).create_saved_audience(
            ACCOUNT, CAMPAIGN, "Example saved", confirm_send=True
        )

    assert calls.admitted == [False]
    assert result["status"] == "change_blocked"
    [structure] = result["refused_structure"]
    assert structure["path"] == "campaign-manager-api/campaignManagerTargetingCriteria"
    assert structure["method"] == "POST"
    assert structure["restli_method"] is None
    assert set(structure["body"]) == {
        "name",
        "description",
        "include",
        "exclude",
        "account",
        "audienceCount",
    }
    assert structure["reason"] == "saved audience carries fields outside the plan"
    shown = json.dumps(structure)
    for value in ("Example saved", ACCOUNT, "100000777", "urn:li:", "100000901"):
        assert value not in shown


LANGUAGE = "urn:li:adTargetingFacet:interfaceLocales"
LANGUAGE_BLOCK = {
    "or": [{"facet": {"urn": LANGUAGE}, "segments": [{"urn": "urn:li:locale:en_US"}]}]
}
WITH_LANGUAGE = {
    "include": {"and": [*TARGETING["include"]["and"], LANGUAGE_BLOCK]},
    "exclude": TARGETING["exclude"],
}


async def test_saved_audience_dry_run_says_the_language_may_be_left_out(mock_page):
    browser = _Browser(mock_page)
    browser.loads[DETAILS] = [[_campaign(WITH_LANGUAGE)]]

    with browser.running(), _dialog(mock_page):
        result = await _audiences(mock_page).create_saved_audience(
            ACCOUNT, CAMPAIGN, "Example saved", confirm_send=False
        )

    assert result["status"] == "confirmation_required"
    assert "without the profile-language targeting" in result["message"]


async def test_saved_audience_dry_run_without_language_says_nothing_of_it(mock_page):
    browser = _Browser(mock_page)
    browser.loads[DETAILS] = [[_campaign()]]

    with browser.running(), _dialog(mock_page):
        result = await _audiences(mock_page).create_saved_audience(
            ACCOUNT, CAMPAIGN, "Example saved", confirm_send=False
        )

    assert "profile-language" not in result["message"]


@pytest.mark.parametrize(
    ("sent", "omitted"),
    [(TARGETING, ["include_language"]), (WITH_LANGUAGE, None)],
)
async def test_saved_audience_result_reports_a_dropped_language(
    mock_page, sent, omitted
):
    browser = _Browser(mock_page)
    browser.loads[DETAILS] = [[_campaign(WITH_LANGUAGE)]]
    browser.loads[MATCHED] = [[_matched()]]
    browser.tab_waves = [[_saved(_saved_entry(int(NEW_ID), "Example saved"))]]

    with (
        browser.running(),
        _dialog(mock_page, sends=[_saved_create("Example saved", sent)]) as calls,
    ):
        result = await _audiences(mock_page).create_saved_audience(
            ACCOUNT, CAMPAIGN, "Example saved", confirm_send=True
        )

    assert calls.admitted == [True]
    assert result["status"] == "audience_created"
    assert result.get("omitted_sections") == omitted
    if omitted:
        assert "without this ad set's profile-language targeting" in result["message"]
        assert "broader audience" in result["message"]
    else:
        assert result["message"].startswith("Saved this ad set's targeting")
