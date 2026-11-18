"""Tests for the shared Rest.li payload helpers (synthetic data)."""

from __future__ import annotations

import pytest

from linkedin_mcp_server.scraping.restli import (
    as_dicts,
    as_int,
    as_mapping,
    as_str,
    iso_from_ms,
    normalized_elements,
    plain_elements,
    restli_list,
)


def test_coercers_accept_only_their_type():
    assert as_mapping({"a": 1}) == {"a": 1}
    assert as_mapping([]) is None
    assert as_dicts([{"a": 1}, "x", 2]) == [{"a": 1}]
    assert as_dicts({"a": 1}) == []
    assert as_str("x") == "x"
    assert as_str("") is None
    assert as_str(1) is None
    assert as_int(7) == 7
    assert as_int(True) is None
    assert as_int("7") is None


def test_iso_from_ms():
    assert iso_from_ms(1_700_000_000_000) == "2023-11-14T22:13:20Z"
    assert iso_from_ms("1700000000000") is None
    assert iso_from_ms(10**20) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("List(a,b)", ["a", "b"]),
        ("List((id:1,text:x),(id:2))", ["(id:1,text:x)", "(id:2)"]),
        ("List()", []),
        ("a,b", []),
        (None, []),
    ],
)
def test_restli_list_splits_top_level_items(value, expected):
    assert restli_list(value) == expected


def test_plain_elements():
    payload = {"elements": [{"id": 100000001}], "paging": {"total": 1500}}
    assert plain_elements(payload) == [{"id": 100000001}]
    assert plain_elements({"elements": []}) == []
    assert plain_elements({"elements": [{"id": 1}, "urn"]}) is None
    assert plain_elements({"data": {}}) is None


def test_normalized_elements_resolve_urn_references():
    payload = {
        "data": {"elements": ["urn:li:example:1", {"id": 2}]},
        "included": [{"entityUrn": "urn:li:example:1", "name": "Example group"}],
    }
    assert normalized_elements(payload) == [
        {"entityUrn": "urn:li:example:1", "name": "Example group"},
        {"id": 2},
    ]
    assert normalized_elements({"data": {"elements": ["urn:li:example:9"]}}) is None
    assert normalized_elements({"elements": []}) is None
