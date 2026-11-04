"""Line-break handling for send_message: normalisation, typing and confirmation."""

from __future__ import annotations

import pytest

from linkedin_mcp_server.scraping.contracts import (
    normalize_message_line_breaks,
    refuse_an_invalid_message,
)
from test_send_message_confirmation_dom import (
    ID_TRANSITION_SEND_JS,
    compose_page,
    dom_page as dom_page_fixture,
    send,
)

dom_page = dom_page_fixture

MULTILINE = "Hello there\n\nSecond paragraph\nThird line"

RECORD_INPUT_TYPES_JS = """
  document.body.dataset.inputTypes = '';
  document.getElementById('composer').addEventListener('input', event => {
    document.body.dataset.inputTypes += event.inputType + ',';
  });
  document.getElementById('composer').addEventListener('keydown', event => {
    document.body.dataset.keys = (document.body.dataset.keys || '') + event.key + ',';
  });
"""


class TestLineBreakNormalisation:
    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("a\nb", "a\nb"),
            ("a\r\nb", "a\nb"),
            ("a\n\n\n\nb", "a\n\nb"),
            ("a\r\n\r\n\r\nb", "a\n\nb"),
            ("a\n\nb", "a\n\nb"),
        ],
    )
    def test_crlf_becomes_lf_and_long_runs_collapse_to_two(self, message, expected):
        assert normalize_message_line_breaks(message) == expected

    @pytest.mark.parametrize("message", ["a\nb", "a\r\nb", "a\n\n\n\nb"])
    def test_line_breaks_are_accepted(self, message):
        assert refuse_an_invalid_message("alice", message) is None

    @pytest.mark.parametrize(
        "message", ["a\rb", "a\tb", "a\x00b", "a\x1bb", "a\x7fb", "a\n\x0bb"]
    )
    def test_other_control_characters_are_still_refused(self, message):
        refusal = refuse_an_invalid_message("alice", message)
        assert refusal is not None
        assert refusal["status"] == "invalid_message"
        assert refusal["retry_safe"] is True


class TestMultilineSendInBrowser:
    async def test_lines_are_joined_by_line_breaks_never_by_enter(self, dom_page):
        result = await send(
            dom_page,
            compose_page(RECORD_INPUT_TYPES_JS + ID_TRANSITION_SEND_JS),
            message=MULTILINE,
        )

        assert result["status"] == "sent"
        input_types = (
            await dom_page.evaluate("document.body.dataset.inputTypes")
        ).split(",")
        assert input_types.count("insertLineBreak") == 3
        assert "insertParagraph" not in input_types
        assert not await dom_page.evaluate("document.body.dataset.keys")
        assert await dom_page.evaluate("document.body.dataset.clicked") == "1"

    async def test_confirmation_matches_multiline_text_rendered_without_newlines(
        self, dom_page
    ):
        result = await send(
            dom_page, compose_page(ID_TRANSITION_SEND_JS), message=MULTILINE
        )

        assert result["status"] == "sent"
        assert result["sent"] is True
        entries = dom_page.locator("#thread .msg")
        assert await entries.last.get_attribute("data-event-urn") == "server-opaque-id"
        text = await entries.last.locator(".message-unit").inner_text()
        assert text.split() == MULTILINE.split()

    async def test_crlf_message_is_typed_and_confirmed(self, dom_page):
        result = await send(
            dom_page,
            compose_page(ID_TRANSITION_SEND_JS),
            message="One\r\nTwo\r\n\r\n\r\nThree",
        )

        assert result["status"] == "sent"
        assert result["sent"] is True
