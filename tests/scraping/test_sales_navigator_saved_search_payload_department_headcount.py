"""A saved account search's create and read-back carrying a department headcount."""

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

ENGINEERING_50_TO_500 = SearchFilter(
    DEPARTMENT_HEADCOUNT, ids=("8",), minimum=50, maximum=500
)


def _body(department: str = "8", bounds: dict | None = None) -> dict:
    body = copy.deepcopy(FIXTURE["account_create"]["body"])
    body["searchQuery"]["accountSearchQuery"]["filters"] = [
        {
            "type": DEPARTMENT_HEADCOUNT,
            "values": [
                {"id": department, "text": "Engineering", "selectionType": "INCLUDED"}
            ],
            "rangeValue": bounds or {"min": 50, "max": 500},
        }
    ]
    return body


def _planned():
    return create_request("accounts", "logistics", [ENGINEERING_50_TO_500])


def test_a_create_with_the_department_and_range_is_admitted():
    assert (
        _refusal(_planned(), "POST", FIXTURE["account_create"]["path"], _body()) is None
    )


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(_body(department="13"), id="another_department"),
        pytest.param(_body(bounds={"min": 50, "max": 5000}), id="another_range"),
    ],
)
def test_a_create_for_another_department_headcount_is_refused(body):
    assert (
        _refusal(_planned(), "POST", FIXTURE["account_create"]["path"], body)
        == "body differs from the plan"
    )


@pytest.mark.parametrize(("department", "why"), [("8", None), ("13", "its filters")])
def test_the_read_back_checks_the_department(department, why):
    element = {
        "name": "Example",
        "keywords": "logistics",
        "filters": [
            {
                "singleFilterMetadata": {
                    "type": DEPARTMENT_HEADCOUNT,
                    "values": [
                        {
                            "id": department,
                            "displayValue": "Engineering",
                            "selectionType": "INCLUDED",
                        }
                    ],
                    "rangeValue": {"min": 50, "max": 500},
                }
            }
        ],
    }
    mismatch = readback_mismatch(
        element, "Example", "logistics", [ENGINEERING_50_TO_500]
    )
    assert (mismatch is None) if why is None else why in (mismatch or "")
