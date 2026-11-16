"""send_message types line breaks as Shift+Enter and reports flattened delivery."""

from __future__ import annotations

from linkedin_mcp_server.scraping.message_sender import _line_breaks_lost
from test_send_message_confirmation_dom import (
    ID_TRANSITION_SEND_JS,
    NOOP_SEND_JS,
    compose_page,
    dom_page as dom_page_fixture,
    send,
)

dom_page = dom_page_fixture

MULTILINE = "Hello there\n\nSecond paragraph\nThird line"

RECORD_KEYS_JS = """
  document.body.dataset.keys = '';
  document.getElementById('composer').addEventListener('keydown', event => {
    document.body.dataset.keys +=
      (event.shiftKey ? 'Shift+' : '') + event.key + ',';
  });
"""

# The server accepts the message but renders it with the breaks gone.
FLATTENED_DELIVERY_SEND_JS = """
  document.getElementById('send').addEventListener('click', event => {
    event.preventDefault();
    document.body.dataset.clicked = '1';
    const composer = document.getElementById('composer');
    const flat = composer.innerText.replace(/\\s+/g, ' ').trim();
    const entry = messageItem(flat, 'client-opaque-id');
    document.getElementById('thread').appendChild(entry);
    setTimeout(() => {
      entry.setAttribute('data-event-urn', 'server-opaque-id');
    }, 0);
    composer.textContent = '';
  });
"""


class TestShiftEnterTyping:
    async def test_every_line_break_is_shift_enter_and_enter_is_never_bare(
        self, dom_page
    ):
        result = await send(
            dom_page,
            compose_page(RECORD_KEYS_JS + NOOP_SEND_JS),
            message=MULTILINE,
        )

        keys = (await dom_page.evaluate("document.body.dataset.keys")).split(",")
        enters = [key for key in keys if key.endswith("Enter")]
        # Two breaks in the blank line plus one single break make three.
        assert enters == ["Shift+Enter"] * 3
        assert result["status"] != "compose_interact_failed"
        assert await dom_page.evaluate("document.body.dataset.clicked") == "true"

    async def test_single_line_message_presses_no_enter(self, dom_page):
        await send(
            dom_page,
            compose_page(RECORD_KEYS_JS + NOOP_SEND_JS),
            message="one line",
        )

        keys = await dom_page.evaluate("document.body.dataset.keys")
        assert "Enter" not in keys


class TestSentFlattened:
    async def test_intact_delivery_is_plain_sent(self, dom_page):
        result = await send(
            dom_page, compose_page(ID_TRANSITION_SEND_JS), message=MULTILINE
        )

        assert result["status"] == "sent"

    async def test_delivery_with_lost_line_breaks_is_sent_flattened(self, dom_page):
        result = await send(
            dom_page, compose_page(FLATTENED_DELIVERY_SEND_JS), message=MULTILINE
        )

        assert result["status"] == "sent_flattened"
        assert result["sent"] is True
        assert result["retry_safe"] is False
        assert "line breaks" in result["message"]


PARAGRAPH_DELIVERY_SEND_JS = """
  document.getElementById('send').addEventListener('click', event => {
    event.preventDefault();
    document.body.dataset.clicked = '1';
    const composer = document.getElementById('composer');
    const entry = messageItem('', 'client-opaque-id');
    const unit = entry.querySelector('.message-unit');
    unit.replaceChildren(...composer.innerText.split('\\n').filter(Boolean).map(
      line => Object.assign(document.createElement('p'), {textContent: line})));
    document.getElementById('thread').appendChild(entry);
    setTimeout(() => {
      entry.setAttribute('data-event-urn', 'server-opaque-id');
    }, 0);
    composer.textContent = '';
  });
"""

# Focus jumps to the Send button once the first line is in.
FOCUS_LEAVES_JS = """
  document.body.dataset.keys = '';
  const composer = document.getElementById('composer');
  composer.addEventListener('keydown', event => {
    document.body.dataset.keys += event.key + ',';
  });
  composer.addEventListener('input', () => {
    document.getElementById('send').focus();
  });
"""


class TestFocusAndRelease:
    async def test_focus_leaving_the_editor_stops_typing_and_is_not_retry_safe(
        self, dom_page
    ):
        result = await send(
            dom_page, compose_page(FOCUS_LEAVES_JS + NOOP_SEND_JS), message=MULTILINE
        )

        assert result["status"] == "compose_interact_failed"
        assert result["retry_safe"] is False
        assert result["sent"] is False
        assert await dom_page.evaluate("document.body.dataset.clicked") is None
        assert "Enter" not in await dom_page.evaluate("document.body.dataset.keys")
        text = await dom_page.evaluate("document.getElementById('composer').innerText")
        assert text.strip() == ""

    async def test_a_failing_keystroke_releases_shift_and_cleans_up(self, dom_page):
        calls = 0
        original = dom_page.keyboard.insert_text

        async def fail_second(text):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("keystroke failed")
            await original(text)

        dom_page.keyboard.insert_text = fail_second
        result = await send(dom_page, compose_page(NOOP_SEND_JS), message=MULTILINE)

        assert result["status"] == "compose_interact_failed"
        assert result["retry_safe"] is False
        assert await dom_page.evaluate("document.body.dataset.clicked") is None
        text = await dom_page.evaluate("document.getElementById('composer').innerText")
        assert text.strip() == ""
        await dom_page.evaluate(
            "document.addEventListener('keydown', e => {"
            " document.body.dataset.shift = String(e.shiftKey); })"
        )
        await dom_page.keyboard.press("x")
        assert await dom_page.evaluate("document.body.dataset.shift") == "false"


class TestParagraphBubbles:
    async def test_paragraph_elements_with_blank_gaps_are_plain_sent(self, dom_page):
        result = await send(
            dom_page, compose_page(PARAGRAPH_DELIVERY_SEND_JS), message=MULTILINE
        )

        assert result["status"] == "sent"


class TestLineBreaksLost:
    def test_same_text_with_breaks_is_not_lost(self):
        assert not _line_breaks_lost("a\n\nb\nc", "a\n\nb\nc")

    def test_crlf_and_long_runs_are_normalised_like_the_tool_does(self):
        assert not _line_breaks_lost("a\n\nb", "a\r\n\r\n\r\nb")

    def test_same_words_on_one_line_are_lost(self):
        assert _line_breaks_lost("a\nb", "a b")

    def test_a_dropped_blank_line_is_not_a_lost_line(self):
        assert not _line_breaks_lost("a\n\nb", "a\nb")

    def test_extra_blank_lines_between_paragraphs_are_not_lost(self):
        assert not _line_breaks_lost("a\nb\nc", "a\n\nb\n\nc")

    def test_different_words_are_not_a_flattening(self):
        assert not _line_breaks_lost("a\nb", "a c")

    def test_no_delivered_text_is_not_a_finding(self):
        assert not _line_breaks_lost("a\nb", None)
