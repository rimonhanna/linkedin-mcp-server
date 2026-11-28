"""The lead-search filters Sales Navigator takes by id, and their tables.

A filter is sent as the id Sales Navigator's search URL carries for it, never
as a label: labels are in the member's UI language. Names here are this
server's. The echo check in ``SalesNavigatorReader._search`` makes a filter
the page drops or rewrites an error; it cannot show that the server applied
the filter or that an id names what was meant.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Literal, cast

from linkedin_mcp_server.scraping.contracts import FilterValidationError

RELATIONSHIP = "RELATIONSHIP"
FUNCTION = "FUNCTION"
YEARS_IN_CURRENT_POSITION = "YEARS_IN_CURRENT_POSITION"
YEARS_AT_CURRENT_COMPANY = "YEARS_AT_CURRENT_COMPANY"
LEAD_LIST = "LEAD_LIST"
ACCOUNT_LIST = "ACCOUNT_LIST"
# "Company headquarters location": the current company's HQ, by geo id, as
# the filter panel sends it (measured 2026-11-27). ``REGION`` is the lead's own
# location.
COMPANY_HEADQUARTERS = "COMPANY_HEADQUARTERS"
# Account search's "Department headcount": a headcount rangeValue with the
# department (a function id) as the filter's selectedSubFilter, never as a
# value, which LinkedIn ignores. The shape of a captured request for its
# sibling DEPARTMENT_HEADCOUNT_GROWTH; not yet measured for this filter.
DEPARTMENT_HEADCOUNT = "DEPARTMENT_HEADCOUNT"

FUNCTION_IDS: Mapping[str, str] = {
    "accounting": "1",
    "administrative": "2",
    "arts_and_design": "3",
    "business_development": "4",
    "community_and_social_services": "5",
    "consulting": "6",
    "education": "7",
    "engineering": "8",
    "entrepreneurship": "9",
    "finance": "10",
    "healthcare_services": "11",
    "human_resources": "12",
    "information_technology": "13",
    "legal": "14",
    "marketing": "15",
    "media_and_communication": "16",
    "military_and_protective_services": "17",
    "operations": "18",
    "product_management": "19",
    "program_and_project_management": "20",
    "purchasing": "21",
    "quality_assurance": "22",
    "real_estate": "23",
    "research": "24",
    "sales": "25",
    "customer_success_and_support": "26",
}

TenureBucket = Literal[
    "less_than_1",
    "1_to_2",
    "3_to_5",
    "6_to_10",
    "more_than_10",
]
TENURE_IDS: Mapping[TenureBucket, str] = {
    "less_than_1": "1",
    "1_to_2": "2",
    "3_to_5": "3",
    "6_to_10": "4",
    "more_than_10": "5",
}


def function_ids(values: Sequence[str]) -> list[str]:
    """Function ids from ids in the table or names; anything else raises."""
    known = set(FUNCTION_IDS.values())
    ids: list[str] = []
    for raw in values:
        token = raw.strip()
        if token in known:
            ids.append(token)
            continue
        name = re.sub(r"[\s-]+", "_", token.casefold().replace("&", "and"))
        mapped = FUNCTION_IDS.get(name)
        if mapped is None:
            raise FilterValidationError(
                f"Unknown function {raw!r}; expected one of {list(FUNCTION_IDS)!r} "
                "or its id (1 to 26)."
            )
        ids.append(mapped)
    return ids


def department_headcount(
    value: Mapping[str, object],
) -> tuple[str, int | None, int | None]:
    """The function id and headcount bounds of a department headcount filter."""
    department = value.get("department")
    if not isinstance(department, str) or not department.strip():
        raise FilterValidationError(
            "department_headcount needs a department, a function name or id."
        )
    (department_id,) = function_ids([department])
    bounds: list[int | None] = []
    for name in ("min", "max"):
        bound = value.get(name)
        if bound is not None and (
            isinstance(bound, bool) or not isinstance(bound, int) or bound < 0
        ):
            raise FilterValidationError(
                f"department_headcount {name} must be a whole number of at least 0."
            )
        bounds.append(bound)
    minimum, maximum = bounds
    if minimum is None and maximum is None:
        raise FilterValidationError(
            "department_headcount needs a min or a max headcount."
        )
    if minimum is not None and maximum is not None and minimum > maximum:
        raise FilterValidationError(
            "department_headcount min must not be greater than its max."
        )
    return department_id, minimum, maximum


def tenure_ids(values: Sequence[str], field: str) -> list[str]:
    """Tenure bucket ids; an unknown bucket raises, naming the field."""
    unknown = [value for value in values if value not in TENURE_IDS]
    if unknown:
        raise FilterValidationError(
            f"Unknown {field} {unknown!r}; expected one of {list(TENURE_IDS)!r}."
        )
    return [TENURE_IDS[cast(TenureBucket, value)] for value in values]
