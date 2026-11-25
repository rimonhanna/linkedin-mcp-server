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

    async def test_the_old_gating_dialog_with_two_buttons_sends_the_note(
        self, dom_page
    ):
        # Issue #455's layout: "Add a note" and "Send without a note" both as
        # buttons, beside the icon-only Dismiss in the header.
        footer = '<button onclick="addNote()">Add a note</button>' + _SEND_WITHOUT

        result = await _submit(dom_page, _page(footer), NOTE)

        assert await _clicks(dom_page) == ["add-note", f"send-note:{NOTE}"]
        assert result[:3] == (True, True, None)


# Layouts where the last button is "Add a note" or the send-without control
# is not the last button. The control beside the primary would send without
# the note, so no click at all is the only safe answer.
_MIRRORED = {
    "fr-buttons": (
        "<button onclick=\"sendWithout('send-without')\">Envoyer sans note</button>"
        '<button onclick="addNote()">Ajouter une note</button>'
    ),
    "en-connect-without": (
        "<button onclick=\"sendWithout('send-without')\">"
        "Connect without a note</button>"
        '<button onclick="addNote()">Add a note</button>'
    ),
    "de-link": (
        '<a href="#" onclick="event.preventDefault();'
        " sendWithout('send-without')\">Ohne Notiz senden</a>"
        '<button onclick="addNote()">Notiz hinzufügen</button>'
    ),
}


class TestNoteControlRefusal:
    async def test_no_note_control_clicks_nothing_and_says_so(self, dom_page):
        result = await _submit(dom_page, _page(_SEND_WITHOUT), NOTE)

        submitted, note_sent, limit, text, missing = result
        assert await _clicks(dom_page) == []
        assert (submitted, note_sent, limit) == (False, False, None)
        assert text is not None and "Send without a note" in text
        assert missing is not None
        assert 'no control reads "Add a note"' in missing
        assert "'text': 'Send without a note'" in missing
        assert await dom_page.evaluate(_MODAL_DIALOG_INDEX_JS) == -1

    async def test_another_locale_clicks_nothing(self, dom_page):
        footer = (
            '<a href="#" onclick="event.preventDefault(); addNote()">'
            "Notiz hinzufügen</a>"
            "<button onclick=\"sendWithout('send-without')\">"
            "Ohne Notiz senden</button>"
        )

        result = await _submit(dom_page, _page(footer), NOTE)

        assert await _clicks(dom_page) == []
        assert result[4] is not None and "matched in English only" in result[4]

    @pytest.mark.parametrize("layout", sorted(_MIRRORED))
    async def test_a_mirrored_layout_clicks_nothing(self, dom_page, layout):
        result = await _submit(dom_page, _page(_MIRRORED[layout]), NOTE)

        assert await _clicks(dom_page) == []
        assert result[:3] == (False, False, None)
        assert result[4] is not None
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
        assert result[4] is not None and "matched in English only" in result[4]

    async def test_send_without_a_note_after_the_buttons_is_never_clicked(
        self, dom_page
    ):
        # "Send without a note" as a link after the last button: the last
        # button is then not the Send, and the control beside it is the send.
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
        # The only non-primary control in the footer carries a forbidden name.
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

    async def test_the_send_is_the_last_shown_button_outside_live_regions(
        self, dom_page
    ):
        # A notifications counter inside the dialog with a button of its own,
        # and a hidden button, both after the Send in document order.
        trailing = (
            '<div aria-live="polite">0 notifications'
            "<button onclick=\"record('live')\">x</button></div>"
            '<button style="display:none" onclick="record(\'hidden\')">'
            "Done</button>"
        )
        footer = _ADD_NOTE_AS["link"] + _SEND_WITHOUT

        result = await _submit(dom_page, _page(footer + trailing), None)

        assert await _clicks(dom_page) == ["send-without"]
        assert result[:3] == (True, False, None)


class TestQuotaProbe:
    async def test_the_probe_opens_a_note_control_that_is_not_a_button(self, dom_page):
        # The probe runs on profiles the write gate refused, so it may open
        # the note editor but must never send. Here the editor is replaced by
        # LinkedIn's Premium upsell, whose text is what the probe returns.
        upsell = (
            '<template id="upsell"><p>You are out of free personalized'
            ' invitations. <a href="/premium/products/">Try Premium</a></p>'
            "</template>"
        )
        footer = (
            '<a href="#" onclick="event.preventDefault(); record(\'add-note\');'
            " document.getElementById('content').replaceChildren("
            "document.getElementById('upsell').content.cloneNode(true));\">"
            "Add a note</a>" + _SEND_WITHOUT
        )
        await dom_page.set_content(_page(footer + upsell))

        message = await _actions(dom_page)._probe_invite_note_limit()

        assert await _clicks(dom_page) == ["add-note"]
        assert message is not None and "out of free personalized" in message
        assert await dom_page.evaluate(_MODAL_DIALOG_INDEX_JS) == -1

    async def test_the_quota_probe_never_clicks_the_primary_button(self, dom_page):
        # The legacy three-button dialog: Dismiss, "Add a note", Send. The
        # probe runs only on profiles the write gate refused, so Send here
        # would send the invitation this workflow decided not to send.
        footer = (
            '<button onclick="addNote()">Add a note</button>'
            "<button onclick=\"sendWithout('send')\">Send</button>"
        )
        await dom_page.set_content(_page(footer))

        message = await _actions(dom_page)._probe_invite_note_limit()

        assert await _clicks(dom_page) == ["add-note"]
        assert message is None
        assert await dom_page.evaluate(_MODAL_DIALOG_INDEX_JS) == -1

    @pytest.mark.parametrize("layout", sorted(_MIRRORED))
    async def test_the_probe_clicks_nothing_in_a_mirrored_layout(
        self, dom_page, layout
    ):
        await dom_page.set_content(_page(_MIRRORED[layout]))

        message = await _actions(dom_page)._probe_invite_note_limit()

        assert await _clicks(dom_page) == []
        assert message is None
        assert await dom_page.evaluate(_MODAL_DIALOG_INDEX_JS) == -1
