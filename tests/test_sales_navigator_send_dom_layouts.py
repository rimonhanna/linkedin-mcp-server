"""The Sales Navigator composers as a live probe recorded them (synthetic data).

The new-message composer (``/sales/inbox/mobile-compose/<lead>``) shows two
disabled buttons while the box is empty: an icon "copy to clipboard" button
with no ``aria-disabled``, and Send, a text-only button that is the last of
the actions bar and keeps ``aria-disabled="false"`` while disabled. Over the
empty box sit a draft button and a help button, which go away once something
is typed, and Copy enables along with Send. The reply composer shows Send as
its only disabled button. No LinkedIn request is made.
"""

from __future__ import annotations

import pytest
from test_sales_navigator_send_dom import (  # noqa: F401
    SEND_JS,
    _clicks,
    _profile,
    _thread,
    dom,
    reply,
    send,
)

from linkedin_mcp_server.scraping import sales_navigator_sender

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

ICON = '<svg width="16" height="16" viewBox="0 0 16 16"></svg>'

# As the page does on typing: the placeholder buttons go and Copy enables.
NEW_MESSAGE_TYPING_JS = """
  const copy = document.getElementById('copy');
  box.addEventListener('input', () => {
    copy.disabled = !box.value.trim();
    const ghost = document.getElementById('ghost');
    if (ghost && box.value.trim()) ghost.remove();
  });
"""


def new_message_page(*, copy_icon: str = ICON, typing_js: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"></head>
  <body><main>
    <header class="mobile-compose__inmail-header">Free to Open Profile</header>
    <form id="form" data-x-conversation-widget="compose-form"
          class="_form_jrrmou _form--maximized_jrrmou">
      <input type="text" aria-label="Subject (required)">
      <div id="ghost" class="_message-ghost-placeholder__cta-container">
        <button type="button" aria-label="create draft"
                aria-description="Create a draft to Test" aria-labelledby="draft-label"
                data-x--miq data-sales-action
                class="_draft-with-ai-trigger">Create draft</button>
        <button type="button" aria-label="Dismiss"
                data-test-enterprise-teaching-bubble-dismiss-btn
                style="display: none">Dismiss</button>
        <button type="button" aria-label="Help info">{ICON}</button>
      </div>
      <textarea name="message"
                aria-label="Type your message here or create draft"></textarea>
      <fieldset class="_actions-container">
        <section class="_actions-bar">
          <button type="button" id="attach"
                  aria-describedby="msg-add-attachment-trigger">{ICON}</button>
          <button type="button" id="copy" disabled aria-describedby="msg-copy"
                  data-sales-action>{copy_icon}</button>
          <div class="mlA">
            <button type="button" id="send" disabled aria-disabled="false"
                    data-sales-action class="_primary_ps32ck">Send</button>
          </div>
        </section>
      </fieldset>
    </form>
  </main>
  <script>{SEND_JS.replace("BODY", "box.value")}{NEW_MESSAGE_TYPING_JS}{typing_js}</script>
  </body></html>"""


def reply_page(*, fieldset: bool) -> str:
    actions = f"""
      <button type="button" id="maximize">{ICON}</button>
      <button type="button" id="attach">{ICON}</button>
      <button type="button" id="send" disabled aria-disabled="false"
              class="_primary_ps32ck">Send</button>"""
    if fieldset:
        actions = (
            '<fieldset class="_actions-container"><section class="_actions-bar">'
            f"{actions}</section></fieldset>"
        )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"></head>
  <body><main>
    <form id="form" data-x-conversation-widget="compose-form" class="_form_jrrmou">
      <textarea name="message" aria-label="Write a message"></textarea>
      {actions}
    </form>
  </main>
  <script>{SEND_JS.replace("BODY", "box.value")}</script>
  </body></html>"""


async def test_new_message_dry_run_pins_send_beside_the_disabled_copy_button(dom):  # noqa: F811
    result = await send(
        dom,
        new_message_page(),
        [_profile(2, open_link=True)],
        subject="Hello",
        confirm_send=False,
    )

    assert result["status"] == "confirmation_required", result
    assert result["delivery"]["kind"] == "open_profile"
    assert dom.sends == []


async def test_new_message_sends_when_copy_enables_and_the_placeholder_goes(dom):  # noqa: F811
    result = await send(
        dom, new_message_page(), [_profile(2, open_link=True)], subject="Hello"
    )

    assert result["status"] == "sent", result
    assert dom.sends == [
        {"message": {"body": "First line\nSecond line", "subject": "Hello"}}
    ]
    assert await dom.page.evaluate("document.getElementById('ghost')") is None
    assert await dom.page.evaluate("document.getElementById('copy').disabled") is False
    assert await _clicks(dom) == 1


async def test_a_text_button_enabling_beside_send_stops_the_send(dom):  # noqa: F811
    # Only an icon button, like Copy, may enable along with Send.
    html = new_message_page(copy_icon="Copy")

    result = await send(dom, html, [_profile(2, open_link=True)], subject="Hello")

    assert result["status"] == "send_unavailable", result
    assert await _clicks(dom) == 0
    assert dom.sends == []


async def test_a_button_added_to_the_actions_bar_while_typing_stops_the_send(dom):  # noqa: F811
    html = new_message_page(
        typing_js="""
  box.addEventListener('input', () => {
    if (document.getElementById('late')) return;
    const late = document.createElement('button');
    late.type = 'button';
    late.id = 'late';
    late.textContent = 'Late';
    document.getElementById('attach').after(late);
  });
"""
    )

    result = await send(dom, html, [_profile(2, open_link=True)], subject="Hello")

    assert result["status"] == "send_unavailable", result
    assert await _clicks(dom) == 0
    assert dom.sends == []


async def test_copy_enabling_while_send_stays_disabled_sends_nothing(dom, monkeypatch):  # noqa: F811
    monkeypatch.setattr(sales_navigator_sender, "_SUBMIT_READY_TIMEOUT_SECONDS", 0.5)
    html = new_message_page().replace("box.addEventListener('input', sync);", "", 1)

    result = await send(dom, html, [_profile(2, open_link=True)], subject="Hello")

    assert result["status"] == "send_unavailable", result
    assert "did not become available" in result["message"]
    assert await _clicks(dom) == 0
    assert dom.sends == []


@pytest.mark.parametrize("fieldset", [True, False])
async def test_reply_composer_with_send_its_only_disabled_button_sends(dom, fieldset):  # noqa: F811
    result = await reply(dom, reply_page(fieldset=fieldset), [_thread()])

    assert result["status"] == "sent", result
    assert dom.sends == [
        {"message": {"body": "First line\nSecond line", "subject": None}}
    ]
    assert await _clicks(dom) == 1
