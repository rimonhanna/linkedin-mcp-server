"""Sales Navigator sends: a new message to a lead, a reply in a conversation.

A new message opens the composer route that takes the lead's URN
(``/sales/inbox/mobile-compose/<urn>``), a Sales Navigator page load and not
a lead page view; a reply opens the conversation. Everything up to the
decision to type runs with the read-state guard on, so a dry run or a refusal
leaves a conversation unread and nothing can be sent. The lead is proven by
the page's own profile response, the conversation by its own thread
response, and the route is pinned through every later step.

What a send costs is read from the page, never inferred: the composer shows a
subject field only for an InMail, the profile says whether the lead has an
Open Profile (free), and the page's credit response gives the balance. A send
that spends a credit needs ``spend_inmail_credit`` on top of ``confirm_send``.

A send is confirmed by the app's own send request
(``salesApiMessageActions?action=createMessage``) answering 2xx, never by the
page; anything else after the click is ``send_unconfirmed`` and not retry safe.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
import logging
import time
from typing import Any
from urllib.parse import quote, unquote, urlparse

import anyio

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
import linkedin_mcp_server.scraping.contracts as contracts
import linkedin_mcp_server.scraping.sales_navigator_composer_js as js
from linkedin_mcp_server.scraping.identifiers import (
    SALES_INBOX_URL,
    normalize_sales_lead_id,
    normalize_sales_thread_id,
    sales_inbox_thread_url,
)
from linkedin_mcp_server.scraping.messaging_read_state import (
    allow_explicit_message_write,
    without_read_state_writes,
)
from linkedin_mcp_server.scraping.restli import (
    as_int,
    as_mapping,
)
from linkedin_mcp_server.scraping.sales_navigator import SalesNavigatorReader, _Refusal
from linkedin_mcp_server.scraping.sales_navigator_payload import (
    sales_profile_id,
)
from linkedin_mcp_server.scraping.sales_navigator_send_contract import (
    INMAIL_CREDIT_REQUIRED,
    INMAIL_CREDITS_UNKNOWN,
    NO_INMAIL_CREDITS,
    SUBJECT_REQUIRED,
    Delivery,
    Recipient,
    compose_route_urn,
    refuse_an_invalid_sales_message,
    refuse_an_invalid_sales_reply,
    sales_compose_url,
    sales_message_result,
    sales_profile_urn,
    sales_reply_result,
)
from linkedin_mcp_server.scraping.sales_navigator_send_page import (
    CONNECTION_DEGREE,
    CREDITS,
    THREADS,
    SendCapture,
    existing_thread,
    inmail_credits,
    is_profile,
    is_send,
    no_inbox_access,
    profile_name,
    profile_of,
    sent_body,
    thread_of,
    thread_participant,
)
from linkedin_mcp_server.scraping.sales_navigator_api import SnResponse
from linkedin_mcp_server.scraping.session import ScrapingSession

logger = logging.getLogger(__name__)

_INBOX_PATH = urlparse(SALES_INBOX_URL).path
# The composer renders after the data it is built from.
_COMPOSER_TIMEOUT_SECONDS = 10.0
_SUBMIT_READY_TIMEOUT_SECONDS = 1.0
_CLEANUP_TIMEOUT_SECONDS = 1.0
# Longest wait for the send response once clicked, inside the send budget.
_SEND_RESPONSE_SECONDS = 30.0

_UNCONFIRMED = (
    "Check the conversation in Sales Navigator before retrying; retrying may "
    "deliver the message twice."
)


def _route_path(url: str) -> str:
    return urlparse(url).path.rstrip("/")


class SalesNavigatorMessageSender:
    """Own the two Sales Navigator send workflows."""

    def __init__(
        self,
        session: ScrapingSession,
        reader: SalesNavigatorReader,
    ):
        self._session = session
        self._reader = reader

    @property
    def _page(self) -> Any:
        return self._session.page

    async def _pin_composer(self) -> tuple[Any | None, dict[str, Any]]:
        """The one composer form as a handle and its state, waiting for it to render."""
        deadline = time.monotonic() + _COMPOSER_TIMEOUT_SECONDS
        while True:
            handle = await self._page.evaluate_handle(js.PIN_JS)
            owner = handle.as_element()
            if owner is not None:
                state = await owner.evaluate(js.STATE_JS)
                if isinstance(state, dict) and state.get("status") == "valid":
                    return owner, state
                await self._dispose(owner)
            else:
                await handle.dispose()
            if time.monotonic() >= deadline:
                return None, {"status": "none"}
            await asyncio.sleep(0.25)

    @staticmethod
    async def _dispose(owner: Any) -> None:
        with contracts.before_the_reply_deadline(_CLEANUP_TIMEOUT_SECONDS, shield=True):
            try:
                await owner.evaluate(js.DISPOSE_JS)
            except Exception:
                logger.debug("Could not release the composer", exc_info=True)
            try:
                await owner.dispose()
            except Exception:
                logger.debug("Could not release the composer handle", exc_info=True)

    async def send_message(
        self,
        lead_id: str,
        message: str,
        *,
        confirm_send: bool,
        subject: str | None = None,
        spend_inmail_credit: bool = False,
    ) -> dict[str, Any]:
        """Send a new message to one lead, gated by confirmation and credit.

        Nothing is typed unless the lead, the composer and the cost are all
        verified, ``confirm_send`` is true and, for a send that uses an InMail
        credit, ``spend_inmail_credit`` is true.
        """
        refusal = refuse_an_invalid_sales_message(lead_id, message, subject)
        if refusal is not None:
            return refusal
        message = contracts.normalize_message_line_breaks(message)
        profile_urn = sales_profile_urn(lead_id)
        lead_id = normalize_sales_lead_id(lead_id)
        url = sales_compose_url(profile_urn)
        recipient: Recipient = {"leadId": lead_id}
        respond = partial(sales_message_result, recipient=recipient)
        budget_deadline = contracts.send_budget_deadline()

        capture = SendCapture(self._page)
        async with capture:
            async with without_read_state_writes(self._page):
                # Any profile answers the page load, so a lead the page does
                # not show ends in the lookup below, which is no throttle
                # evidence, rather than in a data wait that is.
                await self._reader._open(
                    capture,
                    url,
                    expected_path=_INBOX_PATH,
                    ready=is_profile,
                    required=False,
                )

                def is_lead(response: SnResponse) -> bool:
                    return profile_of(response, lead_id) is not None

                profile_response = next(
                    iter(capture.matching(is_lead)), None
                ) or await self._reader._optional(capture, is_lead)
                expected_route = self._page.url
                route_urn = compose_route_urn(expected_route)
                if (
                    route_urn is None
                    or sales_profile_id(route_urn) != lead_id
                    or profile_response is None
                ):
                    return respond(
                        expected_route,
                        "recipient_resolution_failed",
                        "Sales Navigator did not open a message to this lead. "
                        "Check the lead_id against search_sales_navigator_leads.",
                    )
                profile = profile_of(profile_response, lead_id) or {}
                if name := profile_name(profile):
                    recipient["name"] = name
                if (degree := as_int(profile.get("degree"))) is not None:
                    recipient["degree"] = degree
                if no_inbox_access(capture.responses):
                    return respond(
                        expected_route,
                        "composer_unavailable",
                        "This Sales Navigator contract has no messaging, so "
                        "nothing was typed or sent.",
                    )

                owner, state = await self._pin_composer()
                if self._page.url != expected_route:
                    if owner is not None:
                        await self._dispose(owner)
                    return respond(
                        self._page.url,
                        "recipient_resolution_failed",
                        "The page moved away from this lead's composer.",
                    )
                if owner is None:
                    return respond(
                        expected_route,
                        "composer_unavailable",
                        "Sales Navigator showed no single message box for this lead, "
                        "so nothing was typed.",
                    )
                proceed = False
                try:
                    is_inmail = state.get("subject") is True
                    if is_inmail:
                        await self._reader._optional(
                            capture,
                            lambda response: (
                                response.ok and response.call.resource == CREDITS
                            ),
                        )
                    await capture.settle()
                    existing = existing_thread(capture.responses)
                    if (
                        not is_inmail
                        and degree != CONNECTION_DEGREE
                        and existing is None
                    ):
                        return respond(
                            expected_route,
                            "composer_unavailable",
                            "Sales Navigator did not show whether this message "
                            "would use an InMail credit, so nothing was typed.",
                            recipient_selected=True,
                        )
                    open_profile = (as_mapping(profile.get("memberBadges")) or {}).get(
                        "openLink"
                    ) is True
                    cost = 1 if is_inmail and not open_profile else 0
                    credits = inmail_credits(capture.responses)
                    delivery: Delivery = {
                        "kind": (
                            "message"
                            if not is_inmail
                            else "open_profile"
                            if open_profile
                            else "inmail"
                        ),
                        "creditCost": cost,
                        "creditsRemaining": credits,
                    }
                    respond = partial(respond, delivery=delivery)
                    refused = self._gate(
                        state,
                        delivery,
                        subject=subject,
                        spend_inmail_credit=spend_inmail_credit,
                        confirm_send=confirm_send,
                        who=recipient.get("name") or "this lead",
                    )
                    if refused is not None:
                        status, text = refused
                        return respond(
                            expected_route, status, text, recipient_selected=True
                        )
                    proceed = True
                finally:
                    if not proceed:
                        await self._dispose(owner)

            try:
                # Only a verified, confirmed send releases the latch, and only
                # once the guarded read above has exited.
                allow_explicit_message_write(self._page)
                return await self._submit(
                    message,
                    subject if is_inmail else None,
                    owner=owner,
                    capture=capture,
                    expected_route=expected_route,
                    budget_deadline=budget_deadline,
                    respond=respond,
                    on_sent={"thread_id": existing},
                )
            finally:
                await self._dispose(owner)

    @staticmethod
    def _gate(
        state: dict[str, Any],
        delivery: Delivery,
        *,
        subject: str | None,
        spend_inmail_credit: bool,
        confirm_send: bool,
        who: str,
    ) -> tuple[str, str] | None:
        """The status that stops this send before anything is typed, if any."""
        if state.get("empty") is not True:
            return (
                "composer_occupied",
                "The message box already holds a draft that would be sent along "
                "with the message. The draft was left untouched.",
            )
        if state.get("sendCount") != 1:
            return (
                "send_unavailable",
                "Sales Navigator showed no single Send button for this message box, "
                "so nothing was typed.",
            )
        cost = delivery["creditCost"]
        credits = delivery["creditsRemaining"]
        is_inmail = delivery["kind"] != "message"
        if cost and credits is None:
            return (
                INMAIL_CREDITS_UNKNOWN,
                "This message is an InMail, and Sales Navigator did not say how "
                "many InMail credits are left, so nothing was typed or sent.",
            )
        if cost and credits is not None and credits < 1:
            return (
                NO_INMAIL_CREDITS,
                "This message is an InMail and no InMail credits are left, so "
                "nothing was typed or sent. Sales Navigator adds credits each month.",
            )
        if subject is not None and not is_inmail:
            return (
                "invalid_message",
                f"A message to {who} has no subject line, so nothing was typed. "
                "Call again without a subject.",
            )
        if is_inmail and subject is None:
            return (
                SUBJECT_REQUIRED,
                f"A message to {who} is an InMail and needs a subject. Nothing was "
                "typed; call again with a one-line subject.",
            )
        if cost and not spend_inmail_credit:
            return (
                INMAIL_CREDIT_REQUIRED,
                f"Sending this InMail to {who} uses 1 of {credits} InMail credits. "
                "Nothing was typed or sent. Set spend_inmail_credit=true with "
                "confirm_send=true to spend it.",
            )
        if not confirm_send:
            cost_text = (
                f"It would use 1 of {credits} InMail credits."
                if cost
                else "It uses no InMail credit."
            )
            return (
                "confirmation_required",
                f"Nothing was sent. The message would go to {who}. {cost_text} "
                "Set confirm_send=true to send it.",
            )
        return None

    async def reply_to_conversation(
        self,
        thread_id: str,
        message: str,
        *,
        confirm_send: bool,
    ) -> dict[str, Any]:
        """Reply inside one Sales Navigator conversation, gated by confirmation.

        The thread id is the authorization: the page must load exactly that
        conversation's own response and stay on its route. A reply composer
        that asks for a subject would start a new InMail and is refused.
        """
        refusal = refuse_an_invalid_sales_reply(thread_id, message)
        if refusal is not None:
            return refusal
        message = contracts.normalize_message_line_breaks(message)
        thread_id = normalize_sales_thread_id(thread_id)
        url = sales_inbox_thread_url(thread_id)
        respond = partial(sales_reply_result, thread_id=thread_id, participant=None)
        budget_deadline = contracts.send_budget_deadline()
        thread_path = _route_path(url)

        capture = SendCapture(self._page)
        async with capture:
            async with without_read_state_writes(self._page):
                response = await self._reader._open(
                    capture,
                    url,
                    expected_path=_INBOX_PATH,
                    ready=lambda item: thread_of(item, thread_id) is not None,
                    refused=_Refusal(
                        "thread_id", frozenset({THREADS}), quote(thread_id, safe="")
                    ),
                )
                expected_route = self._page.url
                record = (
                    thread_of(response, thread_id) if response is not None else None
                )
                if (
                    unquote(_route_path(expected_route)) != unquote(thread_path)
                    or record is None
                    or response is None
                ):
                    return respond(
                        expected_route,
                        "recipient_resolution_failed",
                        "Sales Navigator did not open the requested conversation. "
                        "Check the thread_id against get_sales_navigator_inbox.",
                    )
                participant = thread_participant(response.payload, record)
                respond = partial(
                    sales_reply_result, thread_id=thread_id, participant=participant
                )

                owner, state = await self._pin_composer()
                if self._page.url != expected_route:
                    if owner is not None:
                        await self._dispose(owner)
                    return respond(
                        self._page.url,
                        "recipient_resolution_failed",
                        "The page moved away from the requested conversation.",
                    )
                if owner is None:
                    return respond(
                        expected_route,
                        "composer_unavailable",
                        "The conversation showed no single reply box, so nothing "
                        "was typed.",
                    )
                proceed = False
                try:
                    await capture.settle()
                    delivery: Delivery = {
                        "kind": "message",
                        "creditCost": 0,
                        "creditsRemaining": inmail_credits(capture.responses),
                    }
                    if state.get("subject") is True:
                        return respond(
                            expected_route,
                            "composer_unavailable",
                            "Sales Navigator asks for a subject here, so this "
                            "reply would be a new InMail. Nothing was typed; use "
                            "send_sales_navigator_message with a subject instead.",
                            recipient_selected=True,
                        )
                    respond = partial(respond, delivery=delivery)
                    refused = self._gate(
                        state,
                        delivery,
                        subject=None,
                        spend_inmail_credit=False,
                        confirm_send=True,
                        who=participant or "the participant of this conversation",
                    )
                    if refused is None and not confirm_send:
                        refused = (
                            "confirmation_required",
                            "Nothing was sent. The message would go to "
                            f"{participant or 'the participant of this conversation'}"
                            " as a reply in this conversation and uses no InMail "
                            "credit. Set confirm_send=true to send it.",
                        )
                    if refused is not None:
                        status, text = refused
                        return respond(
                            expected_route, status, text, recipient_selected=True
                        )
                    proceed = True
                finally:
                    if not proceed:
                        await self._dispose(owner)

            try:
                allow_explicit_message_write(self._page)
                return await self._submit(
                    message,
                    None,
                    owner=owner,
                    capture=capture,
                    expected_route=expected_route,
                    budget_deadline=budget_deadline,
                    respond=respond,
                    on_sent={},
                )
            finally:
                await self._dispose(owner)

    async def _type(self, owner: Any, field: str, text: str) -> str:
        """Type ``text`` into the pinned field with real key input.

        Each line break is Shift+Enter pressed on the field element itself; a
        plain Enter is never pressed. Focus is checked before every keystroke.
        """
        argument = {"field": field, "text": text}
        prepared = await owner.evaluate(js.PREPARE_WRITE_JS, argument)
        if prepared != "ready":
            return "occupied" if prepared == "occupied" else "invalid"
        keyboard = self._page.keyboard
        outcome = "typed"
        keys_sent = False
        handle = None

        async def focused() -> bool:
            return (await owner.evaluate(js.FOCUS_CHECK_JS, argument)) is True

        try:
            for index, line in enumerate(text.split("\n")):
                if index > 0:
                    if not await focused():
                        outcome = "focus_lost"
                        break
                    if handle is None:
                        handle = await owner.evaluate_handle(js.FIELD_JS, argument)
                    keys_sent = True
                    await handle.press("Shift+Enter")
                if line:
                    if not await focused():
                        outcome = "focus_lost"
                        break
                    keys_sent = True
                    await keyboard.insert_text(line)
        except BaseException as error:
            # Record what was typed so cleanup can remove it.
            with contracts.before_the_reply_deadline(
                _CLEANUP_TIMEOUT_SECONDS, shield=True
            ):
                try:
                    await owner.evaluate(js.SETTLE_WRITE_JS, argument)
                except Exception:
                    logger.debug("Could not record a partial write", exc_info=True)
            if isinstance(error, Exception):
                logger.debug("Typing stopped partway", exc_info=True)
                return "keys_interrupted"
            raise
        finally:
            if handle is not None:
                try:
                    await handle.dispose()
                except Exception:
                    logger.debug("Could not release the field handle", exc_info=True)
        result = await owner.evaluate(js.SETTLE_WRITE_JS, argument)
        if outcome == "focus_lost":
            return "focus_lost"
        if result in {"written", "flattened"}:
            return result
        return "invalid_after_keys" if keys_sent else "invalid"

    async def _wait_for_submit(self, owner: Any, argument: dict[str, Any]) -> bool:
        deadline = time.monotonic() + _SUBMIT_READY_TIMEOUT_SECONDS
        while True:
            state = await owner.evaluate(js.SUBMIT_READY_JS, argument)
            if state == "ready":
                return True
            if state != "disabled":
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.05, remaining))

    async def _cleanup(self, owner: Any) -> None:
        with contracts.before_the_reply_deadline(_CLEANUP_TIMEOUT_SECONDS, shield=True):
            try:
                await owner.evaluate(js.CLEANUP_JS)
            except Exception:
                logger.debug("Could not clear the typed draft", exc_info=True)

    async def _submit(
        self,
        message: str,
        subject: str | None,
        *,
        owner: Any,
        capture: SendCapture,
        expected_route: str,
        budget_deadline: float,
        respond: Callable[..., dict[str, Any]],
        on_sent: dict[str, Any],
    ) -> dict[str, Any]:
        """Type into the pinned composer, click its Send once, confirm the send.

        ``retry_safe`` turns false from the moment the click is attempted, and
        after any keystroke whose effect nothing proves. ``on_sent`` is added
        to a confirmed send's answer only.
        """
        respond = partial(respond, recipient_selected=True)
        if self._page.url != expected_route:
            return respond(
                self._page.url,
                "recipient_resolution_failed",
                "The page moved before text entry.",
            )
        state = await owner.evaluate(js.STATE_JS)
        if not isinstance(state, dict) or state.get("status") != "valid":
            return respond(
                self._page.url,
                "compose_interact_failed",
                "The message box changed before text entry.",
            )
        if state.get("empty") is not True:
            return respond(
                self._page.url,
                "composer_occupied",
                "The message box already holds a draft that would be sent along "
                "with the message. The draft was left untouched.",
            )

        may_have_submitted = False
        try:
            fields = ([("subject", subject)] if subject is not None else []) + [
                ("message", message)
            ]
            for field, text in fields:
                written = await self._type(owner, field, text)
                after_keys = (
                    {"retry_safe": False}
                    if written
                    in {"focus_lost", "keys_interrupted", "invalid_after_keys"}
                    else {}
                )
                if self._page.url != expected_route:
                    return respond(
                        self._page.url,
                        "recipient_resolution_failed",
                        "The page moved during text entry.",
                        **after_keys,
                    )
                if written == "occupied":
                    return respond(
                        self._page.url,
                        "composer_occupied",
                        "The message box already holds a draft that would be sent "
                        "along with the message. The draft was left untouched.",
                    )
                if written in {"focus_lost", "keys_interrupted"}:
                    return respond(
                        self._page.url,
                        "compose_interact_failed",
                        "Focus left the message box or typing was interrupted, so "
                        "the message was not sent and the draft was cleared. Some "
                        "keystrokes may have reached another part of the page; "
                        "check the conversation before retrying.",
                        retry_safe=False,
                    )
                if written == "flattened":
                    return respond(
                        self._page.url,
                        "compose_interact_failed",
                        "The message box dropped the line breaks, so the message "
                        "was not sent and the draft was cleared. Retry with a "
                        "single-line message.",
                    )
                if written != "written":
                    return respond(
                        self._page.url,
                        "compose_interact_failed",
                        "The message box could not take the message.",
                        **after_keys,
                    )

            argument = {"message": message, "subject": subject}
            if not await self._wait_for_submit(owner, argument):
                return respond(
                    self._page.url,
                    "send_unavailable",
                    "The Send button did not become available without the message "
                    "box changing.",
                )
            if anyio.current_time() >= budget_deadline:
                # Too little time is left to confirm a send, and nothing has
                # been submitted: end like any timeout before dispatch.
                await anyio.sleep_forever()

            sent_before = len(capture.responses)
            with anyio.CancelScope(deadline=budget_deadline):
                try:
                    # A click can dispatch before the evaluate call reports an
                    # error, so an exception from this round trip is ambiguous.
                    may_have_submitted = True
                    clicked = await owner.evaluate(js.SUBMIT_JS, argument)
                except Exception:
                    logger.debug("Send click did not complete", exc_info=True)
                    return respond(
                        self._page.url,
                        "send_unconfirmed",
                        "The click on Send was interrupted and Sales Navigator did "
                        "not confirm the send. " + _UNCONFIRMED,
                        retry_safe=False,
                    )
                if clicked != "clicked":
                    may_have_submitted = False
                    return respond(
                        self._page.url,
                        "send_unavailable",
                        "The Send button was missing, disabled or changed.",
                    )
                try:
                    answer = await capture.wait_for(
                        lambda response: is_send(response.call),
                        after=sent_before,
                        timeout=max(
                            0.0,
                            min(
                                _SEND_RESPONSE_SECONDS,
                                budget_deadline - anyio.current_time(),
                            ),
                        ),
                        throttle=False,
                    )
                except LinkedInScraperException:
                    return respond(
                        self._page.url,
                        "send_unconfirmed",
                        "Send was clicked but Sales Navigator's send request did "
                        "not answer in time. " + _UNCONFIRMED,
                        retry_safe=False,
                    )
                if not answer.ok:
                    return respond(
                        self._page.url,
                        "send_unconfirmed",
                        f"Send was clicked and Sales Navigator answered HTTP "
                        f"{answer.status}. " + _UNCONFIRMED,
                        retry_safe=False,
                    )
                delivered = (
                    sent_body(capture.send_requests[-1])
                    if capture.send_requests
                    else None
                )
                if contracts.line_breaks_lost(message, delivered):
                    return respond(
                        self._page.url,
                        "sent_flattened",
                        "Message sent and confirmed by Sales Navigator, but it left "
                        "without its line breaks. It is already sent; do not send "
                        "it again.",
                        sent=True,
                        retry_safe=False,
                        **on_sent,
                    )
                return respond(
                    self._page.url,
                    "sent",
                    "Message sent and confirmed by Sales Navigator's send response.",
                    sent=True,
                    retry_safe=False,
                    **on_sent,
                )
            # Reached only when the budget ran out.
            return respond(
                self._page.url,
                "send_unconfirmed",
                "The tool deadline arrived before Sales Navigator confirmed the "
                "send, and the message may already have left. " + _UNCONFIRMED,
                retry_safe=False,
            )
        except Exception:
            if not may_have_submitted:
                raise
            logger.debug("Send failed after a possible submission", exc_info=True)
            return respond(
                self._page.url,
                "send_unconfirmed",
                "The message may already have been sent when the send failed. "
                + _UNCONFIRMED,
                retry_safe=False,
            )
        except BaseException:
            if may_have_submitted:
                logger.warning(contracts.SEND_INTERRUPTED_WARNING)
            raise
        finally:
            if not may_have_submitted:
                await self._cleanup(owner)
