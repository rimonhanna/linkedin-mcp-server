"""Browser-DOM tests for LinkedIn's Press Enter to Send preference (issue #88).

With the preference on, the composer's form has no submit button, only its
send-options toggle. Both send paths must answer ``enter_to_send_enabled``
before typing, dry run included, and never press Enter. The pages are
synthetic: they claim nothing about LinkedIn's markup beyond what the scoping
rules read. No LinkedIn request or write is made.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping.enter_to_send import ENTER_TO_SEND_ENABLED
from linkedin_mcp_server.scraping.message_sender import (
    MessageSender,
    _MESSAGE_COMPOSER_STATE_JS,
    _ProfileMessageTarget,
    _ProfileMessageTargetResolution,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

MESSAGE = "UNDELIVERED SENTINEL"
COMPOSE_URL = "https://www.linkedin.com/messaging/compose/?recipient=ACoAAB"
TARGET = _ProfileMessageTarget(
    profile_path="/in/test-member/",
    profile_urn="ACoAAB",
    compose_url=COMPOSE_URL,
    display_name="Test Member",
)
THREAD_ID = "2-abc=="
THREAD_ROUTE = "https://www.linkedin.com/messaging/thread/2-abc==/"

TOGGLE = '<button id="toggle" type="button" aria-expanded="false">...</button>'
SUBMIT = '<button id="send" type="submit">Send</button>'

# Records every key that reaches the page and every click on a form button, so
# a test can prove nothing was typed, pressed or clicked. Kept in the DOM:
# evaluate runs in an isolated world that cannot see page globals.
RECORDER_JS = """
  document.addEventListener('keydown', event => {
    document.body.dataset.keys = (document.body.dataset.keys || '') + event.key;
  }, true);
  for (const button of document.querySelectorAll('form button')) {
    button.addEventListener('click', event => {
      event.preventDefault();
      document.body.dataset.clicked = button.id;
    });
  }
"""


def compose_page(
    footer: str, *, toggle_outside: bool = False, scope: str = "form"
) -> str:
    outside = TOGGLE if toggle_outside else ""
    return f"""<!DOCTYPE html>
<html lang="en">
  <head><meta charset="utf-8"><title>Messaging</title></head>
  <body>
    <main>
      <section id="conversation" role="dialog">
        {outside}
        <{scope} id="composer-scope" onsubmit="return false">
          <div id="recipient" data-profile-urn="ACoAAB">
            <a href="https://www.linkedin.com/in/test-member/">Test Member</a>
          </div>
          <div id="composer" role="textbox" contenteditable="true"
               style="display:block;width:200px;height:30px"></div>
          {footer}
        </{scope}>
      </section>
    </main>
    <script>{RECORDER_JS}</script>
  </body>
</html>
"""


def thread_page(footer: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
  <head><meta charset="utf-8"><title>Messaging</title></head>
  <body>
    <main>
      <section id="pane">
        <header>
          <h2>Test Member</h2>
          <a href="https://www.linkedin.com/in/test-member/">View profile</a>
        </header>
        <div id="thread">
          <div data-view-name="message-list-item" data-event-urn="message-id">
            <p>Hello there</p>
          </div>
        </div>
        <form id="composer-scope" onsubmit="return false">
          <div id="composer" role="textbox" contenteditable="true"
               style="display:block;width:200px;height:30px"></div>
          {footer}
        </form>
      </section>
    </main>
    <script>{RECORDER_JS}</script>
  </body>
</html>
"""


def with_late_submit(html: str, *, delay_ms: int = 400) -> str:
    """The same page, with its Send button rendered after the editor and toggle."""
    script = (
        "<script>setTimeout(() => document.getElementById('composer-scope')"
        f".insertAdjacentHTML('beforeend', '{SUBMIT}'), {delay_ms});</script>"
    )
    return html.replace("</body>", f"{script}\n  </body>", 1)


@pytest.fixture
async def dom_page():
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                channel="chromium", headless=True
            )
            page = await browser.new_page()
        except Exception as exc:
            if os.environ.get("CI"):
                raise
            pytest.skip(f"chromium unavailable: {exc}")
        page.set_default_timeout(600)
        page.set_default_navigation_timeout(10_000)
        yield page
        await browser.close()


async def _route(page, html: str) -> None:
    async def fulfill(route):
        await route.fulfill(status=200, content_type="text/html", body=html)

    await page.route("https://www.linkedin.com/**", fulfill)


async def send(page, html: str, *, confirm_send: bool) -> dict:
    await _route(page, html)
    await page.goto(COMPOSE_URL)
    session = ScrapingSession(page)
    sender = MessageSender(session, PageNavigator(session))
    with (
        patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
        patch.object(
            sender,
            "_read_profile_message_target",
            new_callable=AsyncMock,
            return_value=_ProfileMessageTargetResolution("resolved", TARGET),
        ),
        patch(
            "linkedin_mcp_server.scraping.message_sender._message_page_url_is_safe",
            return_value=True,
        ),
    ):
        return await sender.send_message(
            "test-member", MESSAGE, confirm_send=confirm_send
        )


async def reply(page, html: str, *, confirm_send: bool) -> dict:
    await _route(page, html)
    await page.goto(THREAD_ROUTE)
    session = ScrapingSession(page)
    sender = MessageSender(session, PageNavigator(session))
    with patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock):
        return await sender.reply_to_conversation(
            THREAD_ID, MESSAGE, confirm_send=confirm_send
        )


async def untouched(page) -> bool:
    return await page.evaluate(
        """() => !document.body.dataset.keys &&
            !document.body.dataset.clicked &&
            document.getElementById('composer').innerText.trim() === ''"""
    )


class TestEnterToSendDetection:
    async def _state(self, page, html: str) -> dict:
        await _route(page, html)
        await page.goto(COMPOSE_URL)
        return await page.evaluate(
            _MESSAGE_COMPOSER_STATE_JS,
            {"profilePath": TARGET.profile_path, "profileUrn": TARGET.profile_urn},
        )

    async def test_toggle_without_submit_is_enter_to_send(self, dom_page):
        state = await self._state(dom_page, compose_page(TOGGLE))

        assert state["status"] == "valid"
        assert state["submitCount"] == 0
        assert state["enterToSend"] is True

    @pytest.mark.parametrize(
        "toggle",
        [
            '<button type="button" aria-haspopup="menu">...</button>',
            '<button type="button" aria-expanded="true">...</button>',
        ],
        ids=["haspopup", "expanded"],
    )
    async def test_either_menu_attribute_marks_the_toggle(self, dom_page, toggle):
        state = await self._state(dom_page, compose_page(toggle))

        assert state["enterToSend"] is True

    async def test_a_submit_button_means_the_preference_is_off(self, dom_page):
        state = await self._state(dom_page, compose_page(SUBMIT + TOGGLE))

        assert state["submitCount"] == 1
        assert state["enterToSend"] is False

    async def test_no_submit_and_no_toggle_is_not_enter_to_send(self, dom_page):
        state = await self._state(dom_page, compose_page(""))

        assert state["submitCount"] == 0
        assert state["enterToSend"] is False

    async def test_hidden_toggle_does_not_count(self, dom_page):
        hidden = TOGGLE.replace("<button ", '<button style="display:none" ', 1)

        state = await self._state(dom_page, compose_page(hidden))

        assert state["enterToSend"] is False

    async def test_a_toggle_outside_the_editors_form_does_not_count(self, dom_page):
        state = await self._state(dom_page, compose_page("", toggle_outside=True))

        assert state["enterToSend"] is False

    async def test_a_plain_button_is_not_a_send_options_toggle(self, dom_page):
        plain = '<button id="plain" type="button">...</button>'

        state = await self._state(dom_page, compose_page(plain))

        assert state["submitCount"] == 0
        assert state["enterToSend"] is False

    async def test_a_dialog_without_a_form_is_not_enter_to_send(self, dom_page):
        state = await self._state(dom_page, compose_page(TOGGLE, scope="div"))

        assert state["status"] == "valid"
        assert state["submitCount"] == 0
        assert state["enterToSend"] is False


class TestSendMessageWithEnterToSend:
    @pytest.mark.parametrize("confirm_send", [False, True], ids=["dry-run", "send"])
    async def test_refuses_before_typing_and_never_presses_enter(
        self, dom_page, confirm_send
    ):
        result = await send(dom_page, compose_page(TOGGLE), confirm_send=confirm_send)

        assert result["status"] == ENTER_TO_SEND_ENABLED
        assert result["recipient_selected"] is True
        assert result["sent"] is False
        assert result["retry_safe"] is True
        assert "Press Enter to Send" in result["message"]
        assert await untouched(dom_page)

    async def test_without_toggle_stays_send_unavailable(self, dom_page):
        result = await send(dom_page, compose_page(""), confirm_send=True)

        assert result["status"] == "send_unavailable"
        assert result["retry_safe"] is True
        assert await untouched(dom_page)

    async def test_a_send_button_is_not_reported_as_enter_to_send(self, dom_page):
        result = await send(dom_page, compose_page(SUBMIT + TOGGLE), confirm_send=False)

        assert result["status"] == "confirmation_required"

    async def test_a_late_send_button_is_not_reported_as_enter_to_send(self, dom_page):
        page = with_late_submit(compose_page(TOGGLE))

        result = await send(dom_page, page, confirm_send=False)

        assert result["status"] == "confirmation_required"
        assert await untouched(dom_page)


class TestReplyWithEnterToSend:
    @pytest.mark.parametrize("confirm_send", [False, True], ids=["dry-run", "send"])
    async def test_refuses_before_typing_and_never_presses_enter(
        self, dom_page, confirm_send
    ):
        result = await reply(dom_page, thread_page(TOGGLE), confirm_send=confirm_send)

        assert result["status"] == ENTER_TO_SEND_ENABLED
        assert result["thread_id"] == THREAD_ID
        assert result["participant"] == "Test Member"
        assert result["recipient_selected"] is True
        assert result["sent"] is False
        assert result["retry_safe"] is True
        assert await untouched(dom_page)

    async def test_a_late_send_button_is_not_reported_as_enter_to_send(self, dom_page):
        page = with_late_submit(thread_page(TOGGLE))

        result = await reply(dom_page, page, confirm_send=False)

        assert result["status"] == "confirmation_required"
        assert await untouched(dom_page)


class TestSubmitRecheck:
    """The preference turned on between the recipient check and text entry."""

    async def test_submit_refuses_before_pinning_the_composer(self):
        page = AsyncMock()
        page.url = COMPOSE_URL
        session = ScrapingSession(page)
        sender = MessageSender(session, PageNavigator(session))
        state = {
            "status": "valid",
            "empty": True,
            "submitCount": 0,
            "enterToSend": True,
        }
        with (
            patch.object(
                sender,
                "_read_message_composer_state",
                new_callable=AsyncMock,
                return_value=state,
            ),
            patch.object(
                sender, "_resolve_message_owner", new_callable=AsyncMock
            ) as resolve,
        ):
            result = await sender._submit_to_verified_composer(
                MESSAGE, target=TARGET, expected_route=COMPOSE_URL
            )

        assert result["status"] == ENTER_TO_SEND_ENABLED
        assert result["retry_safe"] is True
        resolve.assert_not_awaited()
        page.keyboard.press.assert_not_awaited()
