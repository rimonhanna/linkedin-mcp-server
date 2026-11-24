"""Sales Navigator collections in either Rest.li envelope (synthetic data)."""

from __future__ import annotations

from linkedin_mcp_server.scraping.sales_navigator_payload import (
    build_account_card,
    build_elements,
    build_paging,
)

COMPANY_URN = "urn:li:fs_salesCompany:100000001"
PAGING = {"start": 0, "count": 25, "total": 30}


def test_a_normalized_collection_builds_like_a_plain_one():
    card = {"entityUrn": COMPANY_URN, "companyName": "Example Company"}
    plain = {"elements": [card], "paging": PAGING}
    normalized = {"data": {"elements": [card], "paging": PAGING}, "included": []}

    assert build_elements(normalized, build_account_card) == build_elements(
        plain, build_account_card
    )
    assert build_paging(normalized, 1, 0) == build_paging(plain, 1, 0)
    assert build_paging(normalized, 1, 0) is not None


def test_a_normalized_ref_resolves_and_an_unresolved_one_refuses():
    card = {"entityUrn": COMPANY_URN, "companyName": "Example Company"}
    resolved = {"data": {"*elements": [COMPANY_URN]}, "included": [card]}
    unresolved = {"data": {"*elements": [COMPANY_URN]}, "included": []}

    built = build_elements(resolved, build_account_card)
    assert built is not None and built[0]["name"] == "Example Company"
    assert build_elements(unresolved, build_account_card) is None
