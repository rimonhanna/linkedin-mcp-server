"""The invite workflow when the dialog never shows the page's credits.

A dry run still reports each person; a confirmed send refuses and writes
nothing. Every value is synthetic.
"""

from __future__ import annotations

from typing import Any

from linkedin_mcp_server.scraping import page_invites as pi

from .test_page_invites import (
    IDS,
    _DialogPage,
    _invite,
    _statuses,
    writes,  # noqa: F401  (fixture)
)


class _NoCreditsPage(_DialogPage):
    """The dialog with its inviter statistics response never arriving."""

    def _emit(self, path: str, payload: Any, **kwargs: Any) -> None:
        if path.startswith("voyagerRelationshipsDashCommunityInviterStatistics"):
            return
        super()._emit(path, payload, **kwargs)

    async def content(self) -> str:
        return "<html><body></body></html>"


async def test_a_dry_run_without_credits_still_reports_each_person(writes):  # noqa: F811
    page = _NoCreditsPage(rows=3, invitable={IDS[2]: False})
    result = await _invite(
        page, [IDS[0], IDS[1], IDS[2], "ACoAAmissing"], confirm=False
    )

    assert page.events == []
    assert page.sent == []
    assert writes == []
    assert result["status"] == "dry_run"
    assert result["retry_safe"] is True
    assert _statuses(result) == {
        IDS[0]: "would_invite",
        IDS[1]: "would_invite",
        IDS[2]: "not_invitable",
        "ACoAAmissing": "not_invitable",
    }
    assert result["credits"] is None
    assert result["section_errors"]["credits"]["error_type"] == "unavailable"


async def test_a_send_without_credits_refuses_and_writes_nothing(writes):  # noqa: F811
    page = _NoCreditsPage(rows=3)
    result = await _invite(page, [IDS[0], IDS[1]], confirm=True)

    assert page.events == []
    assert page.sent == []
    assert writes == []
    assert result["status"] == "credits_unavailable"
    assert result["retry_safe"] is True
    assert result["credits"] is None
    assert result["section_errors"]["credits"]["error_type"] == "unavailable"
    assert _statuses(result) == {IDS[0]: "not_sent", IDS[1]: "not_sent"}


async def test_a_dry_run_without_credits_still_holds_people_to_pacing(
    writes,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setattr(pi, "_pacing_headroom", lambda _now: (1, None))
    result = await _invite(_NoCreditsPage(rows=3), [IDS[0], IDS[1]], confirm=False)

    assert result["status"] == "dry_run"
    assert result["credits"] is None
    assert _statuses(result) == {IDS[0]: "would_invite", IDS[1]: "pacing_cap"}
