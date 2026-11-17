"""Which ``politicalIntent`` the app writes on its own is let through.

Pure rules, synthetic values only: the classic Save and exit resets the
declaration to ``NOT_DECLARED`` for targeting without an EU location, and the
newer editor fills ``NOT_POLITICAL`` itself.
"""

from __future__ import annotations

import pytest

from linkedin_mcp_server.scraping.campaign_manager_political import (
    CLEARED_BY_LINKEDIN,
    CLEARS_DECLARATION,
    MISMATCH,
    NOT_OFFERED,
    UNDECLARED_FILL,
    declaration_stop,
    refuse_intent,
    sent_intent,
)


@pytest.mark.parametrize(
    ("sent", "current", "political_ad", "newer", "expected"),
    [
        # The reset erases nothing: admitted without a declaration.
        ("NOT_DECLARED", None, None, False, None),
        ("NOT_DECLARED", "NOT_DECLARED", None, False, None),
        # The reset would erase a declaration.
        ("NOT_DECLARED", "NOT_POLITICAL", None, False, CLEARS_DECLARATION),
        ("NOT_DECLARED", "POLITICAL", None, False, CLEARS_DECLARATION),
        # A declaration was given but the app saves "not declared".
        ("NOT_DECLARED", "POLITICAL", True, False, CLEARED_BY_LINKEDIN),
        ("NOT_DECLARED", None, False, False, CLEARED_BY_LINKEDIN),
        # The newer editor's silent fill.
        ("NOT_POLITICAL", "NOT_DECLARED", False, True, None),
        ("NOT_POLITICAL", "NOT_DECLARED", None, True, UNDECLARED_FILL),
        ("NOT_POLITICAL", None, None, True, UNDECLARED_FILL),
        ("NOT_POLITICAL", "NOT_DECLARED", True, True, NOT_OFFERED),
        # The classic dialog answered with the other value.
        ("NOT_POLITICAL", None, True, False, MISMATCH),
        ("POLITICAL", None, True, False, None),
        # Re-sending the record's own declaration declares nothing.
        ("NOT_POLITICAL", "NOT_POLITICAL", None, True, None),
    ],
)
def test_refuse_intent(sent, current, political_ad, newer, expected):
    assert refuse_intent(sent, current, political_ad, newer_editor=newer) == expected


def test_sent_intent_reads_only_a_set_string():
    assert sent_intent({"patch": {"$set": {"politicalIntent": "POLITICAL"}}}) == (
        "POLITICAL"
    )
    assert sent_intent({"patch": {"$set": {"name": "x"}}}) is None
    assert sent_intent({"patch": {"$set": {"politicalIntent": 1}}}) is None
    assert sent_intent(None) is None


@pytest.mark.parametrize(
    ("reason", "status"),
    [
        (CLEARS_DECLARATION, "declaration_would_clear"),
        (CLEARED_BY_LINKEDIN, "declaration_cleared_by_linkedin"),
        (UNDECLARED_FILL, "declaration_required"),
        (NOT_OFFERED, "declaration_not_offered"),
    ],
)
def test_each_refusal_names_its_stop(reason, status):
    stop = declaration_stop(
        [(object(), "unplanned change to name"), (object(), reason)]
    )

    assert stop is not None
    assert stop[0] == status
    assert "nothing was" in stop[1]


def test_other_refusals_name_no_declaration_stop():
    assert declaration_stop([(object(), MISMATCH)]) is None
