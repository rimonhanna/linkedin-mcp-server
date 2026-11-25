# tests/test_invite_note_control_dom.py
"""Browser-DOM tests for finding the invite dialog's "Add a note" control.

The unit suite mocks ``page.evaluate``, so the note-control finder never runs
there. These cases drive ``_submit_invite_dialog`` against synthetic invite
dialogs in headless chromium, with every network request aborted.

Measured 2026-11-25: the "Add a note to your invitation?" dialog's only
buttons were an icon-only Dismiss and "Send without a note"; "Add a note" was
some other element, so the old pick (second-to-last button) hit Dismiss.
The markup here is a synthetic container for the algorithm, not a copy of
LinkedIn's: every click the page receives is recorded in order, so a case can
say exactly what was pressed and what was not.

Skipped automatically when chromium is not installed, as
``tests/test_action_signals_dom.py`` is.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import pytest
from patchright.async_api import Page, async_playwright

from linkedin_mcp_server.scraping.connection_actions import (
    _MODAL_DIALOG_INDEX_JS,
    ConnectionActions,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

NOTE = "Hello from a test"

_DISMISS = (
    '<button aria-label="Dismiss" onclick="record(\'dismiss\')">'
    '<svg width="16" height="16"></svg></button>'
)
_SEND_WITHOUT = (
    "<button onclick=\"sendWithout('send-without')\">Send without a note</button>"
)


def _page(footer: str, *, body: str = "Add a note to your invitation?") -> str:
    """An open, centred invite dialog whose footer is ``footer``.

    ``addNote()`` swaps the content for a textarea and a Send button, the way
    the note editor mounts; every send closes the dialog, and Escape closes
    it as LinkedIn's does.
    """
    return (
        "<html><head><script>"
        "const clicks = [];"
        "function record(what) { clicks.push(what);"
        " document.body.setAttribute('data-clicks', JSON.stringify(clicks)); }"
        "function addNote() { record('add-note');"
        " document.getElementById('content').innerHTML ="
        " '<textarea id=\"note\"></textarea>'"
        " + '<div><button onclick=\"sendNote()\">Send</button></div>'; }"
        "function sendNote() {"
        " record('send-note:' + document.getElementById('note').value);"
        " document.getElementById('invite').close(); }"
        "function sendWithout(what) { record(what);"
        " document.getElementById('invite').close(); }"
        "document.addEventListener('keydown', (e) => {"
        " if (e.key === 'Escape') document.getElementById('invite').close(); });"
        "</script></head><body>"
        '<dialog id="invite" open style="width:544px">'
        f"<header>{_DISMISS}</header>"
        f'<div id="content"><p>{body}</p><footer>{footer}</footer></div>'
        "</dialog></body></html>"
    )


@pytest.fixture
async def dom_page():
    """Real chromium page with the network cut off, or skip without a browser."""
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            page = await browser.new_page()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            await page.route("**/*", lambda route: route.abort())
            yield page
        finally:
            await browser.close()


def _actions(page) -> ConnectionActions:
    async def unreachable(_username: str) -> dict[str, Any]:
        raise AssertionError("the DOM cases never read a profile")

    session = ScrapingSession(cast(Page, page))
    return ConnectionActions(session, PageNavigator(session), unreachable)


async def _clicks(page) -> list[str]:
    return await page.evaluate(
        "JSON.parse(document.body.getAttribute('data-clicks') || '[]')"
    )


async def _submit(page, html: str, note: str | None):
    await page.set_content(html)
    return await _actions(page)._submit_invite_dialog(note)


_ADD_NOTE_AS = {
    "link": '<a href="#" onclick="event.preventDefault(); addNote()">Add a note</a>',
    "span": '<span style="cursor:pointer" onclick="addNote()">Add a note</span>',
    "label": '<label onclick="addNote()">Add a note</label>',
}


class TestNoteControl:
    @pytest.mark.parametrize("kind", sorted(_ADD_NOTE_AS))
    async def test_a_note_control_that_is_not_a_button_sends_the_note(
        self, dom_page, kind
    ):
        result = await _submit(
            dom_page, _page(_ADD_NOTE_AS[kind] + _SEND_WITHOUT), NOTE
        )

        assert await _clicks(dom_page) == ["add-note", f"send-note:{NOTE}"]
        assert result[:3] == (True, True, None)
        assert result[4] is None

    async def test_the_legacy_button_layout_still_sends_the_note(self, dom_page):
        footer = (
            '<button onclick="addNote()">Add a note</button>'
            "<button onclick=\"sendWithout('send-without')\">Send</button>"
        )

        result = await _submit(dom_page, _page(footer), NOTE)

        assert await _clicks(dom_page) == ["add-note", f"send-note:{NOTE}"]
        assert result[:3] == (True, True, None)

    async def test_another_locale_uses_the_footers_single_control(self, dom_page):
        footer = (
            '<a href="#" onclick="event.preventDefault(); addNote()">'
            "Notiz hinzufügen</a>"
            "<button onclick=\"sendWithout('send-without')\">"
            "Ohne Notiz senden</button>"
        )

        result = await _submit(dom_page, _page(footer), NOTE)

        assert await _clicks(dom_page) == ["add-note", f"send-note:{NOTE}"]
        assert result[:3] == (True, True, None)


class TestNoteControlRefusal:
    async def test_no_note_control_clicks_nothing_and_says_so(self, dom_page):
        result = await _submit(dom_page, _page(_SEND_WITHOUT), NOTE)

        submitted, note_sent, limit, text, missing = result
        assert await _clicks(dom_page) == []
        assert (submitted, note_sent, limit) == (False, False, None)
        assert text is not None and "Send without a note" in text
        assert missing is not None
        assert "no control other than Send and Dismiss" in missing
        assert "'text': 'Send without a note'" in missing
        assert await dom_page.evaluate(_MODAL_DIALOG_INDEX_JS) == -1

    async def test_an_ambiguous_footer_in_another_locale_clicks_nothing(self, dom_page):
        footer = (
            '<a href="#" onclick="event.preventDefault(); record(\'learn\')">'
            "Mehr erfahren</a>"
            '<a href="#" onclick="event.preventDefault(); addNote()">'
            "Notiz hinzufügen</a>"
            "<button onclick=\"sendWithout('send-without')\">"
            "Ohne Notiz senden</button>"
        )

        result = await _submit(dom_page, _page(footer), NOTE)

        assert await _clicks(dom_page) == []
        assert result[0] is False
        assert result[4] is not None and "several controls" in result[4]

    async def test_send_without_a_note_after_the_buttons_is_never_clicked(
        self, dom_page
    ):
        # "Send without a note" as a link after the last button: the last
        # button is then not the Send, and in a locale with no English match
        # the footer's one other control would be the send itself.
        footer = (
            "<button onclick=\"record('add-note')\">Notiz hinzufügen</button>"
            '<a href="#" onclick="event.preventDefault();'
            " sendWithout('send-without')\">Ohne Notiz senden</a>"
        )

        result = await _submit(dom_page, _page(footer), NOTE)

        assert await _clicks(dom_page) == []
        assert result[4] is not None and "not the last control" in result[4]

    @pytest.mark.parametrize(
        "decoy",
        ["Send without a note", "Dismiss", "Close", "Cancel"],
    )
    async def test_send_and_dismiss_are_never_the_note_control(self, dom_page, decoy):
        # The only non-primary control in the footer carries a forbidden name,
        # so the structural fallback must not take it.
        footer = (
            '<a href="#" onclick="event.preventDefault(); record(\'decoy\')">'
            f"{decoy}</a>"
            "<button onclick=\"sendWithout('send')\">Send</button>"
        )

        result = await _submit(dom_page, _page(footer), NOTE)

        assert await _clicks(dom_page) == []
        assert result[4] is not None

    async def test_a_missing_textarea_logs_the_clickable_elements(
        self, dom_page, caplog
    ):
        footer = (
            '<a href="#" onclick="event.preventDefault(); record(\'add-note\')">'
            "Add a note</a>" + _SEND_WITHOUT
        )

        with caplog.at_level(
            logging.INFO, logger="linkedin_mcp_server.scraping.connection_actions"
        ):
            result = await _submit(dom_page, _page(footer), NOTE)

        assert await _clicks(dom_page) == ["add-note"]
        assert result[:2] == (False, False)
        logged = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.INFO and "did not appear" in r.getMessage()
        ]
        assert len(logged) == 1
        assert "'text': 'Send without a note'" in logged[0]


class TestNoNote:
    async def test_no_note_sends_through_the_primary_button(self, dom_page):
        footer = _ADD_NOTE_AS["link"] + _SEND_WITHOUT

        result = await _submit(dom_page, _page(footer), None)

        assert await _clicks(dom_page) == ["send-without"]
        assert result[:3] == (True, False, None)
        assert result[4] is None
