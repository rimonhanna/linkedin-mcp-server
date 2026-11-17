"""One reader for both Rest.li envelopes LinkedIn answers in (synthetic data)."""

from __future__ import annotations

import pytest

from linkedin_mcp_server.scraping.restli import (
    entity_id,
    envelope_elements,
    envelope_paging,
    envelope_record,
    envelope_total,
    same_id,
)

PAGING = {"count": 10, "links": [], "start": 0, "total": 3}


def test_plain_collection():
    payload = {"elements": [{"id": 1}, {"id": 2}], "paging": PAGING}
    assert envelope_elements(payload) == [{"id": 1}, {"id": 2}]
    assert envelope_total(payload) == 3
    assert envelope_elements({"elements": []}) == []


def test_normalized_collection_with_inline_objects():
    # The shape campaignManagerReportingAccounts?q=findAll answers with.
    element = {"$type": "com.example.Account", "id": 100000001, "name": "Example"}
    payload = {
        "data": {
            "$type": "com.example.Collection",
            "elements": [element],
            "entityUrn": "urn:li:collection:example",
            "paging": PAGING,
        },
        "included": [],
    }
    assert envelope_elements(payload) == [element]
    assert envelope_total(payload) == 3
    assert envelope_paging(payload) == PAGING


@pytest.mark.parametrize("key", ["elements", "*elements"])
def test_normalized_collection_resolves_urn_refs_through_included(key):
    urn = "urn:li:example:100000201"
    entity = {"entityUrn": urn, "id": 100000201, "name": "Example group"}
    payload = {"data": {key: [urn], "paging": PAGING}, "included": [entity]}
    assert envelope_elements(payload) == [entity]


def test_an_unresolved_ref_refuses_the_whole_collection():
    payload = {
        "data": {"elements": ["urn:li:example:1", "urn:li:example:2"]},
        "included": [{"entityUrn": "urn:li:example:1", "id": 1}],
    }
    assert envelope_elements(payload) is None


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "x",
        [],
        {"elements": [{"id": 1}, 2]},
        {"elements": "x"},
        {"data": {"paging": PAGING}},
        {"data": "urn:li:example:1"},
        {"paging": PAGING},
    ],
)
def test_wrong_collection_shapes(payload):
    assert envelope_elements(payload) is None


def test_record_in_either_envelope():
    record = {"id": 100000301, "accountId": 100000001}
    assert envelope_record({"data": record, "included": []}) == record
    assert envelope_record(record) == record
    urn = "urn:li:example:100000301"
    ref = {"data": urn, "included": [{"entityUrn": urn, **record}]}
    assert envelope_record(ref) == {"entityUrn": urn, **record}


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"elements": [{"id": 1}]},
        {"data": {"elements": [{"id": 1}]}, "included": []},
        {"data": {"*elements": []}, "included": []},
        {"data": "urn:li:example:9", "included": []},
        {"data": []},
    ],
)
def test_a_collection_or_unresolved_ref_is_not_a_record(payload):
    assert envelope_record(payload) is None


def test_ids_compare_as_numbers_whether_sent_as_int_or_digits():
    assert entity_id(100000301) == 100000301
    assert entity_id("100000301") == 100000301
    assert entity_id(True) is None
    assert entity_id("urn:li:sponsoredCampaign:100000301") is None
    assert same_id(100000301, "100000301")
    assert same_id("100000301", "100000301")
    assert not same_id(100000302, "100000301")
    assert not same_id(None, "100000301")
    assert not same_id(100000301, "urn:li:sponsoredCampaign:100000301")
