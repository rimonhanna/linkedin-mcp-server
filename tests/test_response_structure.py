"""Tests for the value-free response records (synthetic data)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from linkedin_mcp_server.response_structure import (
    MAX_RECORDS,
    ResponseLog,
    is_field_name,
    response_record,
    skeleton,
)

# Every scalar and id-like key here must stay out of any record.
SECRETS = (
    "Jane Q Example",
    "urn:li:sponsoredAccount:5550123",
    "jane@example.com",
    "987654321",
    "12.75",
    "8675309",
    "person@example.org",
    "urn:li:fs_salesProfile:(ACwAAAB1x,NAME_SEARCH)",
    "3f2a9c1e-77b4-4d1a-9e0f-1b2c3d4e5f60",
    "Acme Widgets",
)

PAYLOAD = {
    "elements": [
        {
            "name": "Jane Q Example",
            "entityUrn": "urn:li:sponsoredAccount:5550123",
            "email": "jane@example.com",
            "count": 987654321,
            "spend": 12.75,
            "active": True,
            "tags": ["Acme Widgets"],
        }
    ],
    "results": {
        "urn:li:fs_salesProfile:(ACwAAAB1x,NAME_SEARCH)": {"title": "Acme Widgets"},
        "8675309": {"title": "Acme Widgets"},
        "person@example.org": {"title": "Acme Widgets"},
        "3f2a9c1e-77b4-4d1a-9e0f-1b2c3d4e5f60": {"title": "Acme Widgets"},
    },
    "paging": {"total": 8675309, "start": 0},
    "metadata": None,
}


def test_no_value_or_id_like_key_survives():
    rendered = json.dumps(skeleton(PAYLOAD))

    leaked = [secret for secret in SECRETS if secret in rendered]
    assert leaked == [], f"values leaked into the skeleton: {leaked}"


def test_keys_are_kept_and_scalars_become_types():
    assert skeleton(PAYLOAD) == {
        "elements": {
            "list[1]": {
                "name": "str",
                "entityUrn": "str",
                "email": "str",
                "count": "int",
                "spend": "float",
                "active": "bool",
                "tags": {"list[1]": "str"},
            }
        },
        "results": {"<id-like key> x4": {"title": "str"}},
        "paging": {"total": "int", "start": "int"},
        "metadata": "null",
    }


def test_id_like_keys_are_not_field_names():
    for key in ("urn:li:x:1", "123456", "a@b.co", "deadbeefcafe0011", "two words"):
        assert not is_field_name(key), key
    for key in ("elements", "$type", "*elements", "com.linkedin.voyager.Foo", "q"):
        assert is_field_name(key), key


def test_a_huge_payload_stays_bounded():
    wide = {f"field{chr(97 + i % 26)}{chr(97 + i // 26)}": i for i in range(500)}
    deep: dict = {"leaf": "x"}
    for _ in range(50):
        deep = {"child": deep, "sibling": [wide] * 1000, "wide": wide}

    rendered = json.dumps(skeleton(deep))

    assert len(rendered) < 6000, len(rendered)


def test_a_record_names_the_call_without_its_id():
    call = SimpleNamespace(
        resource="salesApiCompanies", entity_id="5550123", finder="search"
    )
    response = SimpleNamespace(
        status=200,
        headers={"Content-Type": "application/json; charset=UTF-8"},
        request=SimpleNamespace(
            method="GET",
            headers={"accept": "application/vnd.linkedin.normalized+json+2.1"},
        ),
    )

    record = response_record(call, response, body="json", payload={"id": 5550123})

    assert record == {
        "resource": "salesApiCompanies",
        "finder": "search",
        "entity_id": True,
        "method": "GET",
        "status": 200,
        "content_type": "application/json; charset=UTF-8",
        "accept": "application/vnd.linkedin.normalized+json+2.1",
        "body": "json",
        "skeleton": {"id": "int"},
    }
    assert "5550123" not in json.dumps(record)


def test_the_log_keeps_the_latest_records_and_counts_all():
    log = ResponseLog("Example app")
    for index in range(MAX_RECORDS + 5):
        log.add({"index": index})

    assert log.seen == MAX_RECORDS + 5
    assert len(log.records) == MAX_RECORDS
    assert log.records[0] == {"index": 5}
