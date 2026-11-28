"""The department headcount filter as Sales Navigator really sends and reads it.

Measured 2026-11-28 on the account search page: the create carries the range's
bounds as strings, ``{"min": "20", "max": "500"}``, with the department as
``selectedSubFilter`` ``"8"``; the saved search reads back with the range as
a label only, ``{"displayValue": "Engineering 20 – 500"}``. Seat and
generated name are synthetic here.
"""

from __future__ import annotations

import copy

import pytest

from linkedin_mcp_server.scraping.sales_navigator_lead_filters import (
    DEPARTMENT_HEADCOUNT,
)
from linkedin_mcp_server.scraping.sales_navigator_payload import SearchFilter
from linkedin_mcp_server.scraping.sales_navigator_saved_search_payload import (
    create_request,
    readback_mismatch,
)

from .test_sales_navigator_saved_search_payload import FIXTURE, _refusal

PATH = FIXTURE["account_create"]["path"]
ENGINEERING_20_TO_500 = SearchFilter(
    DEPARTMENT_HEADCOUNT, sub_filter="8", minimum=20, maximum=500
)
UK = SearchFilter("REGION", ids=("101165590",))


def _measured(**department: object) -> dict:
    body = copy.deepcopy(FIXTURE["account_create"]["body"])
    inner = body["searchQuery"]["accountSearchQuery"]
    del inner["keywords"], inner["spellCorrectionEnabled"]
    inner["filters"] = [
        {
            "type": "REGION",
            "values": [
                {
                    "id": "101165590",
                    "text": "United Kingdom",
                    "selectionType": "INCLUDED",
                }
            ],
        },
        {
            "type": DEPARTMENT_HEADCOUNT,
            "rangeValue": {"min": "20", "max": "500"},
            "selectedSubFilter": "8",
            **department,
        },
    ]
    return body


def _planned():
    return create_request("accounts", None, [UK, ENGINEERING_20_TO_500])


def test_the_measured_create_with_string_bounds_is_admitted():
    assert _refusal(_planned(), "POST", PATH, _measured()) is None


@pytest.mark.parametrize(
    ("department", "why"),
    [
        pytest.param(
            {"selectedSubFilter": "13"},
            "the DEPARTMENT_HEADCOUNT filter's selectedSubFilter differs",
            id="another_department",
        ),
        pytest.param(
            {"rangeValue": {"min": "20", "max": "5000"}},
            "the DEPARTMENT_HEADCOUNT filter's rangeValue differs",
            id="another_max",
        ),
        pytest.param(
            {"rangeValue": {"min": "20"}},
            "the DEPARTMENT_HEADCOUNT filter's rangeValue differs",
            id="max_dropped",
        ),
        pytest.param(
            {"rangeValue": {"min": True, "max": "500"}},
            "the DEPARTMENT_HEADCOUNT filter's rangeValue differs",
            id="a_bound_not_a_number",
        ),
    ],
)
def test_a_create_for_another_department_or_range_is_refused_naming_the_field(
    department, why
):
    refusal = _refusal(_planned(), "POST", PATH, _measured(**department))
    assert refusal == f"body differs from the plan: {why}"


def _read_back(range_value: dict) -> dict:
    return {
        "name": "Example",
        "filters": [
            {
                "singleFilterMetadata": {
                    "type": "REGION",
                    "values": [
                        {
                            "displayValue": "United Kingdom",
                            "selectionType": "INCLUDED",
                            "id": "101165590",
                        }
                    ],
                }
            },
            {
                "singleFilterMetadata": {
                    "searchFilterConfig": {
                        "title": "Department headcount",
                        "presentationType": "RANGE_TEXT",
                    },
                    "type": DEPARTMENT_HEADCOUNT,
                    "rangeValue": range_value,
                }
            },
        ],
    }


@pytest.mark.parametrize(
    ("label", "why"),
    [
        pytest.param("Engineering 20 \u2013 500", None, id="measured"),
        pytest.param(
            "Engineering 20 \u2013 5,000",
            "its filters are not the ones asked (the DEPARTMENT_HEADCOUNT "
            "filter's rangeValue differs)",
            id="another_max",
        ),
        pytest.param(
            "Engineering 20+",
            "its filters are not the ones asked (the DEPARTMENT_HEADCOUNT "
            "filter's rangeValue differs)",
            id="max_dropped",
        ),
    ],
)
def test_the_measured_read_back_is_checked_by_the_bounds_its_label_shows(label, why):
    element = _read_back({"displayValue": label})
    mismatch = readback_mismatch(element, "Example", None, [UK, ENGINEERING_20_TO_500])
    assert mismatch == why


def test_bounds_with_thousands_separators_read_back_as_the_bounds():
    element = _read_back({"displayValue": "Engineering 1,001 \u2013 5,000"})
    wide = SearchFilter(
        DEPARTMENT_HEADCOUNT, sub_filter="8", minimum=1001, maximum=5000
    )
    assert readback_mismatch(element, "Example", None, [UK, wide]) is None
