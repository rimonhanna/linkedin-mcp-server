"""declaration_note: a declaration is recorded only if it was sent and reads back."""

from __future__ import annotations

from types import SimpleNamespace

from linkedin_mcp_server.scraping.campaign_manager_political import declaration_note


def _save(intent: str | None) -> SimpleNamespace:
    fields = {"dailyBudget": {}} if intent is None else {"politicalIntent": intent}
    return SimpleNamespace(body={"patch": {"$set": fields}})


def test_no_declaration_adds_nothing():
    assert declaration_note(None, [_save("POLITICAL")], {}) == ""


def test_sent_and_read_back_is_recorded():
    note = declaration_note(
        True, [_save("POLITICAL")], {"politicalIntent": "POLITICAL"}
    )

    assert "was recorded" in note


def test_held_already_but_never_sent_is_not_called_recorded():
    note = declaration_note(False, [_save(None)], {"politicalIntent": "NOT_POLITICAL"})

    assert "was recorded" not in note
    assert "not sent" in note
    assert "already holds" in note


def test_sent_but_not_read_back_is_unconfirmed():
    note = declaration_note(True, [_save("POLITICAL")], None)

    assert "was recorded" not in note
    assert "not confirmed" in note


def test_never_sent_is_not_recorded():
    note = declaration_note(True, [_save(None)], {"politicalIntent": "NOT_DECLARED"})

    assert "not recorded" in note
