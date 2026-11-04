"""send_message line-break safety: partial writes, unsupported breaks, flattening."""

from __future__ import annotations

import pytest

from linkedin_mcp_server.scraping import message_sender
from linkedin_mcp_server.scraping.contracts import normalize_message_line_breaks
from test_send_message_confirmation_dom import (
    NOOP_SEND_JS,
    compose_page,
    dom_page as dom_page_fixture,
    send,
)

dom_page = dom_page_fixture

MULTILINE = "Hello there\nSecond line\nThird line"

# The sender evaluates in an isolated world, so the page cannot replace
# document.execCommand; it can only react to the editing events.
FAIL_SECOND_BREAK_JS = """
  let breaks = 0;
  document.getElementById('composer').addEventListener('input', event => {
    if (event.inputType === 'insertLineBreak' && ++breaks === 2) {
      event.target.contentEditable = 'false';
    }
  });
"""

FLATTEN_BREAKS_JS = """
  document.getElementById('composer').addEventListener('input', event => {
    if (event.inputType !== 'insertLineBreak') return;
    const composer = document.getElementById('composer');
    composer.querySelectorAll('br').forEach(br => br.replaceWith(' '));
  });
"""


@pytest.mark.parametrize(
    ("message", "expected"),
    [("\n\nhi\nthere\n", "hi\nthere"), ("\r\nhi\r\n\r\n\r\n", "hi")],
)
def test_leading_and_trailing_newlines_are_stripped(message, expected):
    assert normalize_message_line_breaks(message) == expected


async def composer_text(page) -> str:
    return await page.evaluate("document.getElementById('composer').innerText")


async def test_partial_write_is_cleaned_up_so_a_retry_is_not_occupied(dom_page):
    result = await send(
        dom_page, compose_page(NOOP_SEND_JS + FAIL_SECOND_BREAK_JS), message=MULTILINE
    )

    assert result["status"] == "compose_interact_failed"
    assert result["retry_safe"] is True
    assert await dom_page.evaluate("document.body.dataset.clicked") is None
    assert (await composer_text(dom_page)).strip() == ""


@pytest.fixture
def no_line_break_support(monkeypatch):
    monkeypatch.setattr(
        message_sender,
        "_MESSAGE_COMPOSER_WRITE_JS",
        message_sender._MESSAGE_COMPOSER_WRITE_JS.replace(
            "!document.queryCommandSupported('insertLineBreak')", "true"
        ),
    )


async def test_missing_line_break_support_refuses_before_typing(
    dom_page, no_line_break_support
):
    result = await send(dom_page, compose_page(NOOP_SEND_JS), message=MULTILINE)

    assert result["status"] == "compose_interact_failed"
    assert result["retry_safe"] is True
    assert "nothing was typed" in result["message"]
    assert (await composer_text(dom_page)).strip() == ""


async def test_single_line_message_does_not_need_line_break_support(
    dom_page, no_line_break_support
):
    result = await send(dom_page, compose_page(NOOP_SEND_JS), message="one line")

    assert result["status"] != "compose_interact_failed"
    assert await dom_page.evaluate("document.body.dataset.clicked") == "true"


async def test_flattened_message_is_never_sent_and_is_cleaned_up(dom_page):
    result = await send(
        dom_page, compose_page(NOOP_SEND_JS + FLATTEN_BREAKS_JS), message=MULTILINE
    )

    assert result["status"] == "compose_interact_failed"
    assert result["retry_safe"] is True
    assert result["sent"] is False
    assert await dom_page.evaluate("document.body.dataset.clicked") is None
    assert (await composer_text(dom_page)).strip() == ""
