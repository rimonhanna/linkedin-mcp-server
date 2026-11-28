"""Rows and dates of the connections page, read without a browser."""

from __future__ import annotations

from datetime import date

import pytest

from linkedin_mcp_server.scraping.recent_connections_parse import (
    connected_on,
    connection_from_row,
    connection_line,
)

PID = "ACoAAFAKE00000000000000000000000000000001"


def _raw(**overrides):
    raw = {
        "publicIdentifier": "jane-doe-fake",
        "profileIds": [PID],
        "label": "Jane Doe\n\nFake headline",
        "loose": ["Connected on November 26, 2026"],
    }
    raw.update(overrides)
    return raw


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Connected on November 26, 2026", date(2026, 11, 26)),
        ("Connected on May 1, 2025", date(2025, 5, 1)),
        ("  Connected on March 9,  2024 ", date(2024, 3, 9)),
    ],
)
def test_an_en_us_connected_line_reads_as_its_day(text, expected):
    assert connected_on(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "Verbonden op 26 november 2026",
        "Connected on Smarch 26, 2026",
        "Connected on February 30, 2026",
        "Connected 1 day ago",
        "Message",
    ],
)
def test_any_other_text_has_no_date(text):
    assert connected_on(text) is None


def test_a_row_carries_the_id_invite_to_follow_page_takes_and_its_day():
    assert connection_from_row(_raw()) == {
        "name": "Jane Doe",
        "headline": "Fake headline",
        "profileId": PID,
        "publicIdentifier": "jane-doe-fake",
        "profileUrl": "https://www.linkedin.com/in/jane-doe-fake/",
        "connectedText": "Connected on November 26, 2026",
        "connectedOn": "2026-11-26",
        "connectedOnPrecision": "day",
    }


def test_another_locale_keeps_the_raw_text_and_no_date():
    row = connection_from_row(_raw(loose=["Verbonden op 26 november 2026"]))

    assert row is not None
    assert row["connectedText"] == "Verbonden op 26 november 2026"
    assert row["connectedOn"] is None


def test_a_row_naming_two_profile_ids_gives_neither():
    other = "ACoAAFAKE00000000000000000000000000000002"
    row = connection_from_row(_raw(profileIds=[PID, other]))

    assert row is not None
    assert row["profileId"] is None


def test_a_row_without_a_headline_or_a_readable_id():
    row = connection_from_row(_raw(label="Jane Doe", profileIds=["not-an-id"]))

    assert row is not None
    assert row["headline"] is None
    assert row["profileId"] is None
    assert connection_line(row) == "Jane Doe | Connected on November 26, 2026"


@pytest.mark.parametrize("public", [None, "", "a/b", "has space"])
def test_a_row_without_a_usable_public_id_is_dropped(public):
    assert connection_from_row(_raw(publicIdentifier=public)) is None
