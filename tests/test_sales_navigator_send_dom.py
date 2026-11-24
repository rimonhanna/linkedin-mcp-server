"""Browser-DOM tests for the Sales Navigator send and reply safety contract.

The production composer scripts run in headless Chromium against synthetic
markup shaped like Sales Navigator's composer (a form with
``textarea[name=message]``, an optional subject input and a Send
``button[type=button]`` disabled while empty). Its Send button POSTs to the
send endpoint, which a route answers, so confirmation is read from a real
response. The page's data responses are handed to the sender as the capture
would have seen them. Synthetic data only; no LinkedIn request is made.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping import sales_navigator_sender
from linkedin_mcp_server.scraping.api_capture import CapturedResponse
from linkedin_mcp_server.scraping.messaging_read_state import (
    register_read_state_routes,
)
from linkedin_mcp_server.scraping.sales_navigator_payload import parse_sn_call
from linkedin_mcp_server.scraping.sales_navigator_sender import (
    SalesNavigatorMessageSender,
)
from linkedin_mcp_server.scraping.session import ScrapingSession

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

LEAD = "ACwAAA1000001"
LEAD_URN = f"urn:li:fs_salesProfile:({LEAD},NAME_SEARCH,abcd)"
VIEWER_URN = "urn:li:fs_salesProfile:(ACwAAA1000099,NAME_SEARCH,zzzz)"
THREAD = "2-MTAwMDAwMDAxXzEwMDAwMDAwMg=="
EXISTING_THREAD = "2-MTAwMDAwMDAzXzEwMDAwMDAwNA=="
API = "https://www.linkedin.com/sales-api"
TWO_LINES = "First line\nSecond line"


def _response(url: str, payload: Any, status: int = 200) -> CapturedResponse:
    call = parse_sn_call(url, "GET")
    assert call is not None
    return CapturedResponse(call, status, payload)


def _profile(
    degree: int | None,
    *,
    open_link: bool = False,
    colleague: bool | None = None,
    urn: str = LEAD_URN,
) -> CapturedResponse:
    record: dict[str, Any] = {
        "entityUrn": urn,
        "fullName": "Test Lead",
        "memberBadges": {"openLink": open_link},
    }
    if degree is not None:
        record["degree"] = degree
    if colleague is not None:
        record["colleague"] = colleague
    return _response(
        f"{API}/salesApiProfiles/(profileId:{LEAD},authType:NAME_SEARCH,authToken:abcd)",
        {"data": record, "included": []},
    )


def _credits(value: int) -> CapturedResponse:
    return _response(
        f"{API}/salesApiCredits?q=findCreditGrant&creditGrantType=LSS_INMAIL",
        {"elements": [{"type": "LSS_INMAIL", "value": value, "id": 1}]},
    )


def _existing() -> CapturedResponse:
    return _response(
        f"{API}/salesApiMessagingThreads?q=recipients&recipients=List(x)",
        {"data": {"elements": [{"id": EXISTING_THREAD}]}, "included": []},
    )


def _message(kind: str, author: str, index: int = 1) -> dict[str, Any]:
    return {
        "id": f"m{index}",
        "type": kind,
        "author": author,
        "body": "Hello",
        "deliveredAt": 1_790_000_000_000 + index,
    }


REGULAR = [_message("MESSAGE", LEAD_URN)]
# An InMail the signed-in member sent, which the lead has not taken up.
UNANSWERED = [_message("INMAIL", VIEWER_URN)]


def _thread(messages: list[dict[str, Any]] = REGULAR) -> CapturedResponse:
    return _response(
        f"{API}/salesApiMessagingThreads/{THREAD.replace('=', '%3D')}?count=1",
        {
            "data": {
                "id": THREAD,
                "participants": [VIEWER_URN, LEAD_URN],
                "messages": messages,
            },
            "included": [
                {"entityUrn": VIEWER_URN, "degree": 0, "fullName": "Signed In"},
                {"entityUrn": LEAD_URN, "degree": 1, "fullName": "Test Lead"},
            ],
        },
    )


# The Send button POSTs what the box holds, as the app's createMessage does.
SEND_JS = """
  const form = document.getElementById('form');
  const box = form.querySelector('textarea');
  const subject = form.querySelector('input[type=text]');
  const send = document.getElementById('send');
  const sync = () => {
    send.disabled = !box.value.trim() || (subject && !subject.value.trim());
  };
  box.addEventListener('input', sync);
  if (subject) subject.addEventListener('input', sync);
  sync();
  // Page globals are not visible to the tests' isolated world; the DOM is.
  const record = (name, value) => { document.body.dataset[name] = value; };
  const keys = [];
  record('keys', '[]');
  document.addEventListener('keydown', e => {
    keys.push([e.key, e.shiftKey]);
    record('keys', JSON.stringify(keys));
  }, true);
  let clicks = 0;
  send.addEventListener('click', () => {
    record('clicks', String(++clicks));
    fetch('/sales-api/salesApiMessageActions?action=createMessage', {
      method: 'POST',
      body: JSON.stringify({
        message: {body: BODY, subject: subject ? subject.value : null}
      }),
    }).catch(() => record('sendFailed', 'true'));
  });
"""


def composer_page(
    *,
    subject: bool = False,
    draft: str = "",
    editors: int = 1,
    quiet_buttons: int = 1,
    body_js: str = "box.value",
    mark_read: bool = False,
) -> str:
    extra_editors = "".join(
        '<form><textarea name="message"></textarea></form>' for _ in range(editors - 1)
    )
    subject_html = '<input type="text" aria-label="Subject">' if subject else ""
    # Extra buttons the form shows: one that is never disabled, and as many
    # more disabled ones as asked, to make the Send control ambiguous.
    others = '<button type="button" id="attach">Attach</button>' + "".join(
        f'<button type="button" disabled id="other{index}">X</button>'
        for index in range(quiet_buttons - 1)
    )
    send = (
        '<button type="button" id="send">Send</button>'
        if quiet_buttons
        else '<span id="send"></span>'
    )
    mark = (
        "fetch('/sales-api/salesApiMessagingThreads/x?action=markAsRead',"
        "{method:'POST'}).then(() => { document.body.dataset.marked = 'sent'; },"
        " () => { document.body.dataset.marked = 'blocked'; });"
        if mark_read
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"></head>
  <body><main>
    <form id="form">
      {subject_html}
      <textarea name="message">{draft}</textarea>
      {others}{send}
    </form>
    {extra_editors}
  </main>
  <script>{SEND_JS.replace("BODY", body_js) if quiet_buttons else ""}{mark}</script>
  </body></html>"""


class _Reader:
    """Stands in for SalesNavigatorReader: loads the page, hands back its data."""

    def __init__(self, page: Any, html: str, responses: list[CapturedResponse]):
        self._page = page
        self._html = html
        self._responses = responses

    async def _open(self, capture, url, *, ready, **_kwargs):
        await self._page.goto(url)
        await self._page.set_content(self._html)
        capture.responses.extend(self._responses)
        return next((item for item in self._responses if ready(item)), None)

    @staticmethod
    async def _optional(capture, predicate):
        return next((item for item in capture.responses if predicate(item)), None)


@dataclass
class _Dom:
    """The page, and what its send endpoint received and answers."""

    page: Any
    sends: list[dict[str, Any]] = field(default_factory=list)
    send_status: int = 201


@pytest.fixture
async def dom():
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
        page.set_default_timeout(2_000)
        page.set_default_navigation_timeout(10_000)
        state = _Dom(page)

        async def fulfill(route):
            request = route.request
            if "salesApiMessageActions" in request.url:
                state.sends.append(json.loads(request.post_data or "{}"))
                await route.fulfill(
                    status=state.send_status,
                    content_type="application/json",
                    body='{"value": {"id": "1000000001"}}',
                )
                return
            if "/sales-api/" in request.url:
                await route.fulfill(status=200, body="{}")
                return
            await route.fulfill(
                status=200,
                content_type="text/html",
                body='<!DOCTYPE html><html><head><meta charset="utf-8"></head></html>',
            )

        await page.route("https://www.linkedin.com/**", fulfill)
        # As the browser does at page creation: a route added mid-session
        # can hang (messaging_read_state.py).
        await register_read_state_routes(page)
        try:
            yield state
        finally:
            await browser.close()


def _sender(page, html: str, responses: list[CapturedResponse]):
    sender = SalesNavigatorMessageSender(
        ScrapingSession(page), cast(Any, _Reader(page, html, responses))
    )
    return sender


async def send(dom: _Dom, html, responses, **kwargs) -> dict[str, Any]:
    kwargs.setdefault("confirm_send", True)
    message = kwargs.pop("message", TWO_LINES)
    return await _sender(dom.page, html, responses).send_message(
        LEAD, message, **kwargs
    )


async def reply(dom: _Dom, html, responses, **kwargs) -> dict[str, Any]:
    kwargs.setdefault("confirm_send", True)
    message = kwargs.pop("message", TWO_LINES)
    return await _sender(dom.page, html, responses).reply_to_conversation(
        THREAD, message, **kwargs
    )


async def _box(dom: _Dom) -> str:
    return await dom.page.evaluate("document.querySelector('#form textarea').value")


async def _clicks(dom: _Dom) -> int:
    return int(await dom.page.evaluate("document.body.dataset.clicks || '0'"))


# --- New message to a connection -------------------------------------------


async def test_dry_run_types_nothing_and_reports_a_free_message(dom):
    result = await send(dom, composer_page(), [_profile(1)], confirm_send=False)

    assert result["status"] == "confirmation_required"
    assert result["recipient_selected"] is True
    assert result["sent"] is False and result["retry_safe"] is True
    assert result["delivery"] == {
        "kind": "message",
        "creditCost": 0,
        "creditsRemaining": None,
    }
    assert result["recipient"] == {"leadId": LEAD, "name": "Test Lead", "degree": 1}
    assert "thread_id" not in result
    assert await _box(dom) == ""
    assert json.loads(await dom.page.evaluate("document.body.dataset.keys")) == []
    assert dom.sends == []


async def test_confirmed_send_types_breaks_as_shift_enter_and_confirms_from_the_response(
    dom,
):
    result = await send(dom, composer_page(), [_profile(1), _existing()])

    assert result["status"] == "sent", result
    assert result["sent"] is True and result["retry_safe"] is False
    assert result["thread_id"] == EXISTING_THREAD
    assert dom.sends == [{"message": {"body": TWO_LINES, "subject": None}}]
    keys = json.loads(await dom.page.evaluate("document.body.dataset.keys"))
    assert ["Enter", True] in keys, "a line break must be typed as Shift+Enter"
    assert ["Enter", False] not in keys, "a plain Enter must never be pressed"
    assert await _clicks(dom) == 1


async def test_send_refused_by_the_server_is_unconfirmed_and_not_retry_safe(
    dom,
):
    dom.send_status = 500

    result = await send(dom, composer_page(), [_profile(1)])

    assert result["status"] == "send_unconfirmed"
    assert result["sent"] is False and result["retry_safe"] is False
    assert "500" in result["message"]
    assert "thread_id" not in result


async def test_a_click_that_sends_nothing_is_unconfirmed(dom, monkeypatch):
    monkeypatch.setattr(sales_navigator_sender, "_SEND_RESPONSE_SECONDS", 1.0)
    html = composer_page().replace("fetch('/sales-api/", "void('/sales-api/")

    result = await send(dom, html, [_profile(1)])

    assert result["status"] == "send_unconfirmed"
    assert result["retry_safe"] is False
    assert await _clicks(dom) == 1


async def test_lost_line_breaks_in_the_sent_body_report_sent_flattened(dom):
    html = composer_page(body_js="box.value.replace(/\\n/g, ' ')")

    result = await send(dom, html, [_profile(1)])

    assert result["status"] == "sent_flattened"
    assert result["sent"] is True and result["retry_safe"] is False


async def test_a_draft_is_left_untouched_and_nothing_is_sent(dom):
    result = await send(dom, composer_page(draft="Their own draft"), [_profile(1)])

    assert result["status"] == "composer_occupied"
    assert result["retry_safe"] is True
    assert await _box(dom) == "Their own draft"
    assert dom.sends == []


async def test_two_message_boxes_are_never_typed_into(dom, monkeypatch):
    monkeypatch.setattr(sales_navigator_sender, "_COMPOSER_TIMEOUT_SECONDS", 0.5)

    result = await send(dom, composer_page(editors=2), [_profile(1)])

    assert result["status"] == "composer_unavailable"
    assert result["retry_safe"] is True
    assert await _box(dom) == ""


@pytest.mark.parametrize("quiet_buttons", [0, 2])
async def test_no_single_send_button_is_send_unavailable(dom, quiet_buttons):
    result = await send(dom, composer_page(quiet_buttons=quiet_buttons), [_profile(1)])

    assert result["status"] == "send_unavailable"
    assert result["retry_safe"] is True
    assert await _box(dom) == ""
    assert dom.sends == []


async def test_a_profile_for_another_lead_is_never_written_to(dom):
    other = _profile(1, urn="urn:li:fs_salesProfile:(ACwAAA1000002,NAME_SEARCH,x)")

    result = await send(dom, composer_page(), [other])

    assert result["status"] == "recipient_resolution_failed"
    assert result["recipient_selected"] is False
    assert dom.sends == []


async def test_a_non_connection_with_no_subject_field_and_no_thread_is_refused(
    dom,
):
    result = await send(dom, composer_page(), [_profile(2)])

    assert result["status"] == "composer_unavailable"
    assert dom.sends == []


async def test_a_dry_run_leaves_the_thread_unread(dom):
    result = await send(
        dom, composer_page(mark_read=True), [_profile(1)], confirm_send=False
    )
    await dom.page.wait_for_function("document.body.dataset.marked !== undefined")

    assert result["status"] == "confirmation_required"
    assert await dom.page.evaluate("document.body.dataset.marked") == "blocked"


# --- InMail ----------------------------------------------------------------


async def test_inmail_without_the_spend_flag_types_nothing(dom):
    result = await send(
        dom,
        composer_page(subject=True),
        [_profile(2), _credits(7)],
        subject="Hello",
    )

    assert result["status"] == "inmail_credit_required"
    assert result["retry_safe"] is True
    assert result["delivery"] == {
        "kind": "inmail",
        "creditCost": 1,
        "creditsRemaining": 7,
    }
    assert await _box(dom) == ""
    assert dom.sends == []


async def test_inmail_dry_run_reports_cost_and_credits(dom):
    result = await send(
        dom,
        composer_page(subject=True),
        [_profile(2), _credits(7)],
        subject="Hello",
        spend_inmail_credit=True,
        confirm_send=False,
    )

    assert result["status"] == "confirmation_required"
    assert result["delivery"]["creditCost"] == 1
    assert result["delivery"]["creditsRemaining"] == 7
    assert "1 of 7" in result["message"]
    assert dom.sends == []


async def test_inmail_with_no_credits_left_is_refused_even_with_the_flag(dom):
    result = await send(
        dom,
        composer_page(subject=True),
        [_profile(2), _credits(0)],
        subject="Hello",
        spend_inmail_credit=True,
    )

    assert result["status"] == "no_inmail_credits"
    assert dom.sends == []


async def test_inmail_with_an_unknown_balance_is_refused(dom):
    result = await send(
        dom,
        composer_page(subject=True),
        [_profile(2)],
        subject="Hello",
        spend_inmail_credit=True,
    )

    assert result["status"] == "inmail_credits_unknown"
    assert result["delivery"]["creditsRemaining"] is None
    assert dom.sends == []


async def test_inmail_needs_a_subject(dom):
    result = await send(
        dom,
        composer_page(subject=True),
        [_profile(2), _credits(7)],
        spend_inmail_credit=True,
    )

    assert result["status"] == "subject_required"
    assert dom.sends == []


async def test_a_subject_for_a_connection_is_refused(dom):
    result = await send(dom, composer_page(), [_profile(1)], subject="Hello")

    assert result["status"] == "invalid_message"
    assert dom.sends == []


async def test_open_profile_inmail_is_free(dom):
    result = await send(
        dom,
        composer_page(subject=True),
        [_profile(2, open_link=True)],
        subject="Hello",
        confirm_send=False,
    )

    assert result["status"] == "confirmation_required"
    assert result["delivery"]["kind"] == "open_profile"
    assert result["delivery"]["creditCost"] == 0


async def test_confirmed_inmail_types_the_subject_and_sends_once(dom):
    result = await send(
        dom,
        composer_page(subject=True),
        [_profile(2), _credits(7)],
        subject="Hello",
        spend_inmail_credit=True,
    )

    assert result["status"] == "sent", result
    assert dom.sends == [{"message": {"body": TWO_LINES, "subject": "Hello"}}]
    assert await _clicks(dom) == 1


# --- Reply -----------------------------------------------------------------


async def test_reply_dry_run_names_the_participant_and_types_nothing(dom):
    result = await reply(
        dom, composer_page(mark_read=True), [_thread()], confirm_send=False
    )
    await dom.page.wait_for_function("document.body.dataset.marked !== undefined")

    assert result["status"] == "confirmation_required"
    assert result["participant"] == "Test Lead"
    assert result["thread_id"] == THREAD
    assert result["delivery"]["creditCost"] == 0
    assert await _box(dom) == ""
    assert await dom.page.evaluate("document.body.dataset.marked") == "blocked"
    assert dom.sends == []


async def test_confirmed_reply_is_sent_and_confirmed(dom):
    result = await reply(dom, composer_page(), [_thread()])

    assert result["status"] == "sent", result
    assert result["sent"] is True and result["retry_safe"] is False
    assert dom.sends == [{"message": {"body": TWO_LINES, "subject": None}}]


async def test_reply_box_asking_for_a_subject_is_refused(dom):
    result = await reply(dom, composer_page(subject=True), [_thread()])

    assert result["status"] == "composer_unavailable"
    assert dom.sends == []


async def test_reply_without_the_thread_response_is_refused(dom):
    result = await reply(dom, composer_page(), [_profile(1)])

    assert result["status"] == "recipient_resolution_failed"
    assert dom.sends == []


# --- Cost the page does not show ------------------------------------------


async def test_reply_in_an_unanswered_inmail_is_refused_without_the_flag(dom):
    result = await reply(dom, composer_page(), [_thread(UNANSWERED), _credits(7)])

    assert result["status"] == "inmail_reply_cost_unknown"
    assert result["retry_safe"] is True
    assert result["delivery"] == {
        "kind": "inmail",
        "creditCost": None,
        "creditsRemaining": 7,
    }
    assert await _box(dom) == ""
    assert dom.sends == []


async def test_reply_in_an_unanswered_inmail_needs_a_known_balance(dom):
    result = await reply(
        dom, composer_page(), [_thread(UNANSWERED)], spend_inmail_credit=True
    )

    assert result["status"] == "inmail_credits_unknown"
    assert dom.sends == []


async def test_reply_in_an_unanswered_inmail_with_the_flag_reports_no_cost_claim(dom):
    result = await reply(
        dom,
        composer_page(),
        [_thread(UNANSWERED), _credits(7)],
        spend_inmail_credit=True,
        confirm_send=False,
    )

    assert result["status"] == "confirmation_required"
    assert result["delivery"]["creditCost"] is None
    assert "may use 1 of 7" in result["message"]
    assert dom.sends == []


@pytest.mark.parametrize("answer", ["INMAIL_ACCEPT", "INMAIL_REPLY"])
async def test_reply_in_an_inmail_the_lead_answered_is_free(dom, answer):
    messages = [*UNANSWERED, _message(answer, LEAD_URN, 2)]

    result = await reply(dom, composer_page(), [_thread(messages)], confirm_send=False)

    assert result["status"] == "confirmation_required"
    assert result["delivery"]["kind"] == "inmail"
    assert result["delivery"]["creditCost"] == 0


async def test_an_answer_from_the_signed_in_member_does_not_make_a_reply_free(dom):
    messages = [*UNANSWERED, _message("INMAIL_REPLY", VIEWER_URN, 2)]

    result = await reply(dom, composer_page(), [_thread(messages), _credits(7)])

    assert result["status"] == "inmail_reply_cost_unknown"
    assert dom.sends == []


async def test_new_message_into_a_thread_with_a_non_connection_has_no_cost_claim(dom):
    result = await send(dom, composer_page(), [_profile(2), _existing(), _credits(7)])

    assert result["status"] == "inmail_reply_cost_unknown"
    assert result["delivery"] == {
        "kind": "message",
        "creditCost": None,
        "creditsRemaining": 7,
    }
    assert await _box(dom) == ""
    assert dom.sends == []


async def test_new_message_into_a_thread_with_a_non_connection_sends_with_the_flag(
    dom,
):
    result = await send(
        dom,
        composer_page(),
        [_profile(2), _existing(), _credits(7)],
        spend_inmail_credit=True,
    )

    assert result["status"] == "sent", result
    assert result["delivery"]["creditCost"] is None
    assert len(dom.sends) == 1


# --- Every profile response counts -----------------------------------------


async def test_any_profile_response_with_the_open_profile_badge_counts(dom):
    responses = [_profile(None, open_link=True), _profile(2), _credits(7)]

    result = await send(
        dom,
        composer_page(subject=True),
        responses,
        subject="Hello",
        confirm_send=False,
    )

    assert result["status"] == "confirmation_required"
    assert result["delivery"]["kind"] == "open_profile"
    assert result["recipient"]["degree"] == 2


async def test_profile_responses_that_disagree_on_degree_name_no_connection(dom):
    result = await send(dom, composer_page(), [_profile(1), _profile(2), _existing()])

    assert result["status"] == "inmail_reply_cost_unknown"
    assert "degree" not in result["recipient"]
    assert dom.sends == []


# --- TeamLink --------------------------------------------------------------


async def test_teamlink_cost_is_unknown_and_needs_the_spend_flag(dom):
    responses = [_profile(3, colleague=True), _profile(None, colleague=False)]

    refused = await send(
        dom, composer_page(subject=True), [*responses, _credits(7)], subject="Hello"
    )

    assert refused["status"] == "inmail_reply_cost_unknown", refused
    assert refused["delivery"] == {
        "kind": "teamlink",
        "creditCost": None,
        "creditsRemaining": 7,
    }
    assert dom.sends == []


async def test_teamlink_send_with_the_spend_flag_goes_out(dom):
    responses = [_profile(3, colleague=True), _profile(None, colleague=False)]

    result = await send(
        dom,
        composer_page(subject=True),
        [*responses, _credits(7)],
        subject="Hello",
        spend_inmail_credit=True,
    )

    assert result["status"] == "sent", result
    assert result["delivery"]["kind"] == "teamlink"
    assert result["delivery"]["creditCost"] is None
    assert dom.sends == [{"message": {"body": TWO_LINES, "subject": "Hello"}}]


async def test_teamlink_at_zero_credits_is_refused_before_typing(dom):
    result = await send(
        dom,
        composer_page(subject=True),
        [_profile(3, colleague=True), _credits(0)],
        subject="Hello",
        spend_inmail_credit=True,
    )

    assert result["status"] == "no_inmail_credits"
    assert result["delivery"]["kind"] == "teamlink"
    assert await _box(dom) == ""
    assert dom.sends == []


async def test_teamlink_without_a_known_balance_is_refused(dom):
    result = await send(
        dom,
        composer_page(subject=True),
        [_profile(3, colleague=True)],
        subject="Hello",
        spend_inmail_credit=True,
    )

    assert result["status"] == "inmail_credits_unknown"
    assert dom.sends == []


# --- The guard while sending -----------------------------------------------


async def test_a_mark_read_while_typing_is_aborted(dom):
    html = composer_page().replace(
        "box.addEventListener('input', sync);",
        "box.addEventListener('input', sync);"
        "box.addEventListener('input', () => {"
        " if (document.body.dataset.marked) return;"
        " document.body.dataset.marked = 'pending';"
        " fetch('/sales-api/salesApiMessagingThreads/x?action=markAsRead',"
        " {method: 'POST'}).then(() => { document.body.dataset.marked = 'sent'; },"
        " () => { document.body.dataset.marked = 'blocked'; }); }, {once: true});",
    )

    result = await reply(dom, html, [_thread()])
    await dom.page.wait_for_function("document.body.dataset.marked !== 'pending'")

    assert result["status"] == "sent", result
    assert await dom.page.evaluate("document.body.dataset.marked") == "blocked"


async def test_a_second_send_request_from_one_click_is_aborted(dom):
    html = composer_page().replace(
        "send.addEventListener('click', () => {",
        "send.addEventListener('click', () => {"
        " setTimeout(() => fetch('/sales-api/salesApiMessageActions?action=createMessage',"
        " {method: 'POST', body: JSON.stringify({second: true})})"
        ".then(() => { document.body.dataset.second = 'sent'; },"
        " () => { document.body.dataset.second = 'blocked'; }), 0);",
    )

    result = await reply(dom, html, [_thread()])
    await dom.page.wait_for_function("document.body.dataset.second !== undefined")

    assert await dom.page.evaluate("document.body.dataset.second") == "blocked"
    assert result["status"] == "sent", result
    assert dom.sends == [{"message": {"body": TWO_LINES, "subject": None}}]


async def test_an_aborted_earlier_create_message_is_not_the_sent_one(dom):
    # A createMessage made before the click is aborted by the guard; the body
    # checked for flattening must be the one that actually left.
    html = composer_page().replace(
        "box.addEventListener('input', sync);",
        "box.addEventListener('input', sync);"
        "box.addEventListener('input', () => {"
        " fetch('/sales-api/salesApiMessageActions?action=createMessage',"
        " {method: 'POST', body: JSON.stringify({message: {body: 'First line Second line'}})})"
        ".catch(() => {}); });",
    )

    result = await reply(dom, html, [_thread()])

    assert result["status"] == "sent", result
    assert dom.sends == [{"message": {"body": TWO_LINES, "subject": None}}]


# --- The Send button -------------------------------------------------------


async def test_a_disabled_submit_type_button_is_never_the_send_button(dom):
    html = composer_page().replace(
        '<button type="button" id="send">', '<button id="send">'
    )

    result = await send(dom, html, [_profile(1)])

    assert result["status"] == "send_unavailable"
    assert result["retry_safe"] is True
    assert dom.sends == []


async def test_another_button_changing_while_typing_stops_the_send(dom):
    html = composer_page().replace(
        "box.addEventListener('input', sync);",
        "box.addEventListener('input', sync);"
        "box.addEventListener('input', () => {"
        " document.getElementById('attach').disabled = true; });",
    )

    result = await send(dom, html, [_profile(1)])

    assert result["status"] == "send_unavailable"
    assert result["retry_safe"] is True
    assert await _clicks(dom) == 0
    assert dom.sends == []


async def test_a_send_window_the_click_left_unused_is_closed(dom, monkeypatch):
    monkeypatch.setattr(sales_navigator_sender, "_SEND_RESPONSE_SECONDS", 1.0)
    html = composer_page().replace("fetch('/sales-api/", "void('/sales-api/")

    result = await send(dom, html, [_profile(1)])
    late = await dom.page.evaluate(
        "fetch('/sales-api/salesApiMessageActions?action=createMessage',"
        " {method: 'POST', body: '{}'}).then(() => 'sent', () => 'blocked')"
    )

    assert result["status"] == "send_unconfirmed"
    assert late == "blocked"
    assert dom.sends == []


# --- The inbox reads' access and viewer checks ------------------------------


def _chrome(viewer: str) -> CapturedResponse:
    return _response(
        f"{API}/salesApiNavChrome?decoration=x", {"data": {"viewerSalesUrn": viewer}}
    )


async def test_a_contract_without_an_inbox_types_nothing(dom):
    access = _response(
        f"{API}/salesApiAccess", {"data": {"hasContractInboxAccess": False}}
    )

    result = await send(dom, composer_page(), [_profile(1), access])

    assert result["status"] == "composer_unavailable"
    assert await _box(dom) == ""
    assert dom.sends == []


async def test_a_chrome_naming_the_lead_as_you_leaves_the_reply_cost_unknown(dom):
    messages = [*UNANSWERED, _message("INMAIL_ACCEPT", LEAD_URN, 2)]

    result = await reply(
        dom, composer_page(), [_chrome(LEAD_URN), _thread(messages), _credits(7)]
    )

    assert result["status"] == "inmail_reply_cost_unknown"
    assert result["participant"] is None
    assert dom.sends == []


async def test_a_chrome_naming_you_keeps_the_answered_inmail_free(dom):
    messages = [*UNANSWERED, _message("INMAIL_ACCEPT", LEAD_URN, 2)]

    result = await reply(
        dom,
        composer_page(),
        [_chrome(VIEWER_URN), _thread(messages)],
        confirm_send=False,
    )

    assert result["status"] == "confirmation_required"
    assert result["participant"] == "Test Lead"
    assert result["delivery"]["creditCost"] == 0
