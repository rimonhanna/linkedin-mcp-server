"""The page's credits read from the statistics LinkedIn renders into the page.

On a direct load of the dialog URL no credits request is made; the fixture is
a sanitised copy of the datalets such a page carries. Every value is synthetic.
"""

from __future__ import annotations

from pathlib import Path

from patchright.async_api import Error as PlaywrightError

from linkedin_mcp_server.scraping.page_invite_payload import embedded_credits

from .test_page_invites import (
    COMPANY,
    IDS,
    _actions,
    _DialogPage,
    _invite,
    _statuses,
    writes,  # noqa: F401  (fixture)
)
from .test_page_invites_unknown_credits import _NoCreditsPage

HTML = (
    Path(__file__).parent.parent / "fixtures" / "page-invite-embedded-credits.html"
).read_text()
OTHER_PAGE = "100000999"


def test_the_credits_of_the_page_asked_for_are_read():
    assert embedded_credits(HTML, COMPANY) == {
        "remaining": 100,
        "total": 100,
        "nextGrantOn": "2026-12-01",
    }


def test_another_pages_credits_are_never_taken():
    ours = HTML.index('id="datalet-bpr-guid-1003"')
    only_other = HTML[: HTML.rindex("<code", 0, ours)]

    assert embedded_credits(only_other, COMPANY) is None
    other = embedded_credits(HTML, OTHER_PAGE)
    assert other is not None and other["remaining"] == 7
    assert embedded_credits(HTML, "100000123") is None


def test_a_failed_or_unreadable_datalet_gives_no_credits():
    failed = HTML.replace("&quot;status&quot;:200", "&quot;status&quot;:500")
    assert embedded_credits(failed, COMPANY) is None
    unreadable = HTML.replace("&quot;remainingCredit&quot;:100", "&quot;x&quot;:1")
    assert embedded_credits(unreadable, COMPANY) is None


def test_a_page_serialised_with_plain_quotes_is_read_too():
    plain = HTML.replace("&quot;", '"')
    credits = embedded_credits(plain, COMPANY)
    assert credits is not None and credits["remaining"] == 100


class _RenderedCreditsPage(_NoCreditsPage):
    async def content(self) -> str:
        return HTML


async def test_a_send_uses_the_credits_rendered_into_the_page(writes):  # noqa: F811
    page = _RenderedCreditsPage(rows=3)
    result = await _invite(page, [IDS[0]], confirm=True)

    assert result["status"] == "sent"
    assert result["credits"]["remaining"] == 100
    assert "section_errors" not in result
    assert _statuses(result) == {IDS[0]: "invited"}


class _BothCreditsPage(_DialogPage):
    async def content(self) -> str:
        return HTML


async def test_captured_credits_come_before_the_rendered_ones(writes):  # noqa: F811
    result = await _invite(_BothCreditsPage(rows=3, credits=5), [IDS[0]], confirm=False)

    assert result["credits"]["remaining"] == 5


async def test_the_list_uses_the_credits_rendered_into_the_page(writes):  # noqa: F811
    actions, _ = _actions(_RenderedCreditsPage(rows=3))
    result = await actions.list_follow_candidates(COMPANY, limit=20)

    assert result["credits"] == {
        "remaining": 100,
        "total": 100,
        "nextGrantOn": "2026-12-01",
    }
    assert "credits" not in result.get("section_errors", {})


class _UnreadablePage(_NoCreditsPage):
    async def content(self) -> str:
        raise PlaywrightError("page is navigating")


async def test_a_page_that_cannot_be_read_leaves_credits_unknown(writes):  # noqa: F811
    page = _UnreadablePage(rows=3)
    result = await _invite(page, [IDS[0]], confirm=True)

    assert page.sent == []
    assert result["status"] == "credits_unavailable"
    assert result["credits"] is None
    assert result["section_errors"]["credits"]["error_type"] == "unavailable"
