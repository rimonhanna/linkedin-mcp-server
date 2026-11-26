# tests/test_invite_note_editor_dom.py
"""Browser-DOM tests for the invite dialog's contenteditable note editor.

Measured live 2026-11-26 with a read-only probe (nothing sent, nothing typed):
"Add a note" is an ``<a>`` whose ``href`` is the member's own profile, with no
``target``. LinkedIn's app handles the click in place: no document load, the
URL stays the same, and the same ``<dialog>`` swaps its ``CustomInvite`` screen
for ``CustomInviteCompose``. About 0.5-1.5 s later that screen holds a
tiptap/ProseMirror ``div[contenteditable=true][role=textbox]``, a 0/300
counter, Cancel and a disabled Send that is enabled once the editor holds text.
The page has no ``<textarea>`` at all, so the old wait for one timed out and
the call returned ``connect_unavailable``.

The markup below is that structure with the obfuscated class names dropped and
the member replaced by a placeholder. The swap is scripted (the network is cut
off), and every click the page receives is recorded in order.

Skipped automatically when chromium is not installed, as
``tests/test_invite_note_control_dom.py`` is.
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

NOTE = "Hello from a test, glad to connect."

_GATE = """
<div data-sdui-screen="com.linkedin.sdui.flagshipnav.mynetwork.invitations.CustomInvite">
 <div data-display-contents="true"><div>
  <p>Add a note to your invitation?</p><hr role="presentation">
  <div><div><p>Personalize your invitation to<span> </span><strong>Test Member</strong>
   <span> </span>by adding a note. LinkedIn members are more likely to accept
   invitations that include a note.</p></div></div>
  <hr role="presentation">
  <div>
   <a aria-disabled="false" href="https://www.linkedin.com/in/test-member/"
      onclick="event.preventDefault(); openCompose()"><span><span>Add a note</span></span></a>
   <div data-display-contents="true"><button type="button"
      onclick="sendWithout()"><span><span>Send without a note</span></span></button></div>
  </div>
 </div></div>
</div>
"""

_COMPOSE = """
<div data-sdui-screen="com.linkedin.sdui.flagshipnav.mynetwork.invitations.CustomInviteCompose">
 <div data-display-contents="true"><div>
  <p>Add a note to your invitation</p><hr role="presentation">
  <div><svg width="16" height="16"></svg><h2>Premium</h2></div>
  <div><p>LinkedIn members are more likely to accept invitations that include a
   personal note.</p>
   <div><div data-testid="ui-core-tiptap-text-editor-wrapper"><div>
    <div id="editor" contenteditable="true" role="textbox" dir="ltr" tabindex="0"
         aria-placeholder="Ex: We know each other from..." translate="no"
         style="min-height: 3lh; max-height: 4lh;"><p><br></p></div>
   </div><div data-testid="ui-core-tiptap-text-editor-cursor"></div></div>
   <div id="customInviteNoteCounterRef"><div><p><span id="counter">0/300</span></p></div></div>
   </div>
   <div id="customInviteNoteErrorRef"><div></div></div>
  </div>
  <hr role="presentation">
  <div>
   <button type="button" onclick="record('cancel'); closeInvite()"><span><span>Cancel</span></span></button>
   <div data-display-contents="true"><button id="send" disabled type="button"
      onclick="sendNote()"><span><span>Send</span></span></button></div>
  </div>
 </div></div>
</div>
"""


def _page(
    *, mount_ms: int = 900, keep: int | None = None, send_enables: bool = True
) -> str:
    """The invite dialog as measured, swapping to the editor after ``mount_ms``.

    ``keep`` makes the editor keep only that many characters of what it is
    given while still enabling Send, as an editor that cuts a note short
    would; ``None`` keeps everything. ``send_enables=False`` leaves Send
    disabled whatever the editor holds, as when LinkedIn's own editor state
    never saw the text.
    """
    keep_js = "null" if keep is None else str(keep)
    return (
        "<html><head><script>"
        "const clicks = [];"
        "function record(what) { clicks.push(what);"
        " document.body.setAttribute('data-clicks', JSON.stringify(clicks)); }"
        "function closeInvite() { document.getElementById('invite').close(); }"
        "function sendWithout() { record('send-without'); closeInvite(); }"
        "function editorText() {"
        " return document.getElementById('editor').innerText.replace(/\\s+/g, ' ').trim(); }"
        "function sendNote() { record('send-note:' + editorText()); closeInvite(); }"
        f"const KEEP = {keep_js};"
        f"const SEND_ENABLES = {'true' if send_enables else 'false'};"
        "function wireEditor() {"
        " const ed = document.getElementById('editor');"
        " ed.addEventListener('input', () => {"
        "  if (KEEP !== null && ed.innerText.length > KEEP) {"
        "   ed.textContent = ed.innerText.slice(0, KEEP); }"
        "  const n = ed.innerText.trim().length;"
        "  document.getElementById('counter').textContent = n + '/300';"
        "  document.getElementById('send').disabled = !SEND_ENABLES || n === 0; }); }"
        "function openCompose() { record('add-note');"
        " const screen = document.getElementById('screen');"
        " screen.innerHTML = '<p>Loading</p>';"
        f" setTimeout(() => {{ screen.innerHTML = COMPOSE; wireEditor(); }}, {mount_ms}); }}"
        f"const COMPOSE = {_js_string(_COMPOSE)};"
        "document.addEventListener('keydown', (e) => {"
        " if (e.key === 'Escape') closeInvite(); });"
        "</script></head><body>"
        '<dialog id="invite" data-testid="dialog" open style="width:544px">'
        '<button type="button" data-dialog-dismiss="" aria-label="Dismiss"'
        " onclick=\"record('dismiss'); closeInvite()\">"
        '<span><svg width="16" height="16"></svg></span></button>'
        f'<div data-testid="dialog-content"><div id="screen">{_GATE}</div></div>'
        '<section><h2 data-testid="toasts-title">0 notifications</h2></section>'
        "</dialog></body></html>"
    )


def _js_string(text: str) -> str:
    import json

    return json.dumps(" ".join(text.split()))


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


class TestContentEditableNoteEditor:
    async def test_the_note_is_typed_into_the_editor_and_sent(self, dom_page):
        result = await _submit(dom_page, _page(), NOTE)

        assert await _clicks(dom_page) == ["add-note", f"send-note:{NOTE}"]
        assert result[:3] == (True, True, None)
        assert result[4] is None

    async def test_a_note_on_two_lines_is_sent_whole(self, dom_page):
        note = "First line of the note.\nSecond line of the note."

        result = await _submit(dom_page, _page(), note)

        assert await _clicks(dom_page) == [
            "add-note",
            "send-note:First line of the note. Second line of the note.",
        ]
        assert result[:3] == (True, True, None)

    async def test_an_editor_that_keeps_part_of_the_note_sends_nothing(
        self, dom_page, caplog
    ):
        # Send is enabled by what the editor kept, so only reading the editor
        # back stops a shortened note from going out.
        with caplog.at_level(
            logging.INFO, logger="linkedin_mcp_server.scraping.connection_actions"
        ):
            result = await _submit(dom_page, _page(keep=10), NOTE)

        assert await _clicks(dom_page) == ["add-note"]
        assert result[:3] == (False, False, None)
        assert any(
            f"holds 10 characters, not the {len(NOTE)}" in r.getMessage()
            for r in caplog.records
        )
        assert await dom_page.evaluate(_MODAL_DIALOG_INDEX_JS) == -1

    async def test_a_send_that_stays_disabled_sends_nothing(self, dom_page, caplog):
        # The editor reads the note back, but Send never enables.
        with caplog.at_level(
            logging.INFO, logger="linkedin_mcp_server.scraping.connection_actions"
        ):
            result = await _submit(dom_page, _page(send_enables=False), NOTE)

        assert await _clicks(dom_page) == ["add-note"]
        assert result[:3] == (False, False, None)
        assert await dom_page.evaluate(_MODAL_DIALOG_INDEX_JS) == -1
        assert any(
            r.levelno == logging.INFO
            and "note is in the editor but Send stayed disabled" in r.getMessage()
            for r in caplog.records
        )

    async def test_no_note_sends_without_opening_the_editor(self, dom_page):
        result = await _submit(dom_page, _page(), None)

        assert await _clicks(dom_page) == ["send-without"]
        assert result[:3] == (True, False, None)


class TestQuotaProbeWithEditor:
    async def test_the_probe_sees_the_editor_and_never_sends(self, dom_page, caplog):
        await dom_page.set_content(_page())

        with caplog.at_level(
            logging.INFO, logger="linkedin_mcp_server.scraping.connection_actions"
        ):
            message = await _actions(dom_page)._probe_invite_note_limit()

        assert await _clicks(dom_page) == ["add-note"]
        assert message is None
        assert not [r for r in caplog.records if "did not appear" in r.getMessage()]
        assert await dom_page.evaluate(_MODAL_DIALOG_INDEX_JS) == -1
