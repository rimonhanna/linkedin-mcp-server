"""The saved-search requests against the ones Sales Navigator was measured sending.

``requests.json`` holds the app's own create (lead and account search), its
rename and the Saved searches panel's read-back, captured 2026-11-27 with the
seat and saved-search ids replaced by synthetic ones.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from linkedin_mcp_server.scraping.sales_navigator_list_guard import (
    ListWriteWindow,
    admission_refusal,
)
from linkedin_mcp_server.scraping.sales_navigator_payload import SearchFilter
from linkedin_mcp_server.scraping.sales_navigator_saved_search_payload import (
    create_request,
    created_key,
    find_saved,
    readback_mismatch,
    refuse_invalid_search_name,
    rename_request,
    search_criteria,
)

FIXTURE = json.loads(
    (
        Path(__file__).parents[1]
        / "fixtures"
        / "sales-navigator-saved-search"
        / "requests.json"
    ).read_text()
)
ORIGIN = "https://www.linkedin.com"
SEAT = "urn:li:seat:100000001"
SAVED = "100000777"
NAME = "Example UK leads"
TITLES = ("Head of Sales", "Sales Director")
SEARCH = [
    SearchFilter("CURRENT_TITLE", texts=TITLES),
    SearchFilter("COMPANY_HEADQUARTERS", ids=("101165590",)),
    SearchFilter("INDUSTRY", ids=("4", "96")),
    SearchFilter("COMPANY_HEADCOUNT", ids=("E", "F")),
]
ACCOUNTS = [
    SearchFilter("INDUSTRY", ids=("44",)),
    SearchFilter("REGION", ids=("101165590",)),
    SearchFilter("COMPANY_HEADCOUNT", ids=("E",)),
]


def _refusal(planned, method: str, path: str, body: Any) -> str | None:
    text = json.dumps(body) if body is not None else None
    return admission_refusal(ListWriteWindow(planned), method, ORIGIN + path, text)


def test_the_measured_lead_create_is_admitted_for_its_search():
    sent = FIXTURE["lead_create"]
    assert (
        _refusal(
            create_request("leads", None, SEARCH), "POST", sent["path"], sent["body"]
        )
        is None
    )


def test_the_measured_account_create_is_admitted_for_its_search():
    sent = FIXTURE["account_create"]
    planned = create_request("accounts", "logistics", ACCOUNTS)
    assert _refusal(planned, "POST", sent["path"], sent["body"]) is None


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(lambda b: b.update(useCase="SAVED_ACCOUNT_SEARCH"), id="use_case"),
        pytest.param(
            lambda b: b["searchQuery"]["leadSearchQuery"]["filters"].pop(1),
            id="hq_dropped",
        ),
        pytest.param(
            lambda b: b["searchQuery"]["leadSearchQuery"]["filters"][1].update(
                type="REGION"
            ),
            id="hq_as_lead_location",
        ),
        pytest.param(
            lambda b: b["searchQuery"]["leadSearchQuery"]["filters"][2]["values"].pop(),
            id="an_industry_dropped",
        ),
        pytest.param(
            lambda b: b["searchQuery"]["leadSearchQuery"]["filters"][0]["values"][
                0
            ].update(selectionType="EXCLUDED"),
            id="a_title_excluded",
        ),
        pytest.param(
            lambda b: b["searchQuery"]["leadSearchQuery"]["filters"].append(
                {
                    "type": "FUNCTION",
                    "values": [{"id": "8", "selectionType": "INCLUDED"}],
                }
            ),
            id="an_extra_filter",
        ),
        pytest.param(
            lambda b: b["searchQuery"]["leadSearchQuery"].update(keywords="cto"),
            id="keywords_added",
        ),
    ],
)
def test_a_create_for_another_search_is_refused(tamper):
    body = copy.deepcopy(FIXTURE["lead_create"]["body"])
    tamper(body)
    planned = create_request("leads", None, SEARCH)
    assert (
        _refusal(planned, "POST", FIXTURE["lead_create"]["path"], body)
        == "body differs from the plan"
    )


def test_the_generated_name_seat_and_labels_do_not_decide_admission():
    body = copy.deepcopy(FIXTURE["lead_create"]["body"])
    body["name"] = "anything the app generates"
    body["seat"] = "urn:li:seat:100000009"
    body["searchQuery"]["leadSearchQuery"]["filters"][1]["values"][0]["text"] = "UK"
    planned = create_request("leads", None, SEARCH)
    assert _refusal(planned, "POST", FIXTURE["lead_create"]["path"], body) is None


def test_the_create_answer_names_the_seat_and_the_saved_search():
    assert created_key(FIXTURE["lead_create"]["x-restli-id"]) == (SEAT, SAVED)
    assert created_key(None) is None
    assert created_key("{id=100000777}") is None


def test_the_rename_is_exactly_the_measured_request():
    sent = FIXTURE["rename"]
    planned = rename_request(SEAT, SAVED, NAME)
    assert planned.path == sent["path"]
    assert _refusal(planned, sent["method"], sent["path"], sent["body"]) is None
    assert (
        _refusal(planned, "POST", sent["path"], {"patch": {"$set": {"name": "Other"}}})
        == "body differs from the plan"
    )
    other = sent["path"].replace(SAVED, "100000778")
    assert _refusal(planned, "POST", other, sent["body"]) == "not the planned list"


def test_the_read_back_confirms_the_saved_search():
    element = find_saved(FIXTURE["readback"], SAVED)
    assert element is not None
    assert readback_mismatch(element, NAME, None, SEARCH) is None
    assert find_saved(FIXTURE["readback"], "100000778") is None


@pytest.mark.parametrize(
    ("name", "keywords", "filters", "why"),
    [
        ("Other name", None, SEARCH, "its name is not the one asked"),
        (NAME, "cto", SEARCH, "its keywords are not the ones asked"),
        (NAME, None, SEARCH[:3], "its filters are not the ones asked"),
        (
            NAME,
            None,
            [*SEARCH[:1], SearchFilter("REGION", ids=("101165590",)), *SEARCH[2:]],
            "its filters are not the ones asked",
        ),
    ],
)
def test_a_read_back_of_another_search_is_named(name, keywords, filters, why):
    element = find_saved(FIXTURE["readback"], SAVED)
    assert element is not None
    assert readback_mismatch(element, name, keywords, filters) == why


def test_a_filter_of_the_other_kind_is_refused_naming_it():
    _, why = search_criteria("leads", {"title": ["CTO"], "hq_location": ["UK"]})
    assert (
        why
        == "hq_location is a filter of the accounts search, not of the leads search."
    )
    _, why = search_criteria("accounts", {"size": ["E"], "company_hq_location": ["UK"]})
    assert why == (
        "company_hq_location is a filter of the leads search, not of the "
        "accounts search."
    )
    criteria, why = search_criteria("leads", {"title": ["CTO"], "size": []})
    assert (criteria, why) == ({"title": ["CTO"]}, None)
    assert (
        search_criteria("leads", {"title": None})[1]
        == "Pass keywords or at least one filter."
    )


@pytest.mark.parametrize(
    ("name", "why"),
    [
        ("  ", "name must not be empty."),
        ("a\nb", "name must be one line."),
        ("x" * 257, "name must be at most 256 characters."),
        ("x" * 256, None),
    ],
)
def test_the_name_rules(name, why):
    assert refuse_invalid_search_name(name) == why
