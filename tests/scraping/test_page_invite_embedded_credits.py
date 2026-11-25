"""The page's credits read from the statistics LinkedIn renders into the page.

On a direct load of the dialog URL no credits request is made; the fixture is
a sanitised copy of the datalets such a page carries. Every value is synthetic.
"""

from __future__ import annotations

from pathlib import Path

from linkedin_mcp_server.scraping.page_invite_payload import embedded_credits

from .test_page_invites import (
    COMPANY,
    IDS,
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
