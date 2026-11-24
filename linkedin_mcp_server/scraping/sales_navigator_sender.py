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
Open Profile (free) or is a TeamLink colleague (cost not yet confirmed, so
treated as unknown), and the page's credit
response gives the balance. A send that spends a credit needs
``spend_inmail_credit`` on top of ``confirm_send``. A message into an existing
conversation is free only when the page shows it is: a conversation with a
connection, one with no InMail in it, or an InMail the other side accepted or
answered. Any other one has an unknown cost (``creditCost`` None) and is
treated as one that may spend a credit: it needs ``spend_inmail_credit`` and a
known balance, and without the flag is refused as
``inmail_reply_cost_unknown``.

A send is confirmed by the app's own send request
(``salesApiMessageActions?action=createMessage``) answering 2xx, never by the
page; anything else after the click is ``send_unconfirmed`` and not retry safe.
The read-state guard stays on while typing and sending: the click opens a
window for exactly one ``createMessage``, and a mark-read, an archive or a
second send is still aborted.
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
    close_sales_send,
    allowed_sales_send,
    open_one_sales_send,
    without_read_state_writes,
)
from linkedin_mcp_server.scraping.restli import as_int
from linkedin_mcp_server.scraping.sales_navigator import SalesNavigatorReader, _Refusal
from linkedin_mcp_server.scraping.sales_navigator_payload import (
    sales_profile_id,
)
from linkedin_mcp_server.scraping.sales_navigator_send_contract import (
    INMAIL_CREDIT_REQUIRED,
    INMAIL_CREDITS_UNKNOWN,
    INMAIL_REPLY_COST_UNKNOWN,
    NO_INMAIL_CREDITS,
    SEND_BLOCKED_NO_CREDITS,
    SUBJECT_REQUIRED,
    Delivery,
    DeliveryKind,
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
    merged_profile,
    inbox_refused,
    profile_name,
    profile_of,
    reply_cost,
    sent_body,
    thread_of,
    thread_participant,
    thread_viewer,
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


def _free_at_zero_credits(delivery: Delivery) -> bool:
    """A free InMail kind while the account shows no credits left."""
    return delivery["kind"] == "open_profile" and delivery["creditsRemaining"] == 0


def _cost_text(delivery: Delivery) -> str:
    cost = delivery["creditCost"]
    credits = delivery["creditsRemaining"]
    if delivery["kind"] == "teamlink":
        return (
            f"It is a TeamLink InMail to a colleague. Its cost is not yet "
            f"confirmed, so it may use 1 of {credits} InMail credits."
        )
    if cost is None:
        return (
            f"It may use 1 of {credits} InMail credits: Sales Navigator did not "
            "show whether it does."
        )
    if cost:
        return f"It would use 1 of {credits} InMail credits."
    return "It uses no InMail credit."


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
                profile = merged_profile(capture.responses, lead_id)
                if name := profile_name(profile):
                    recipient["name"] = name
                if (degree := as_int(profile.get("degree"))) is not None:
                    recipient["degree"] = degree
                if inbox_refused(capture.responses):
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
                    if is_inmail or degree != CONNECTION_DEGREE:
                        await self._credits(capture)
                    await capture.settle()
                    # Every profile response the page has by now, merged.
                    profile = merged_profile(capture.responses, lead_id)
                    degree = as_int(profile.get("degree"))
                    if degree is None:
                        recipient.pop("degree", None)
                    else:
                        recipient["degree"] = degree
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
                    kind: DeliveryKind
                    cost: int | None
                    if not is_inmail:
                        # Into an existing conversation with someone who is
                        # not a connection, the page shows no cost at all.
                        kind = "message"
                        cost = 0 if degree == CONNECTION_DEGREE else None
                    elif profile["memberBadges"]["openLink"] is True:
                        kind, cost = "open_profile", 0
                    elif profile["colleague"] is True:
                        # Nothing live proves a colleague InMail is free.
                        kind, cost = "teamlink", None
                    else:
                        kind, cost = "inmail", 1
                    delivery: Delivery = {
                        "kind": kind,
                        "creditCost": cost,
                        "creditsRemaining": inmail_credits(capture.responses),
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
                return await self._submit(
                    message,
                    subject if is_inmail else None,
                    owner=owner,
                    capture=capture,
                    expected_route=expected_route,
                    budget_deadline=budget_deadline,
                    respond=respond,
                    on_sent={"thread_id": existing},
                    delivery=delivery,
                )
            finally:
                await self._dispose(owner)

    async def _credits(self, capture: SendCapture) -> None:
        await self._reader._optional(
            capture,
            lambda response: response.ok and response.call.resource == CREDITS,
        )

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
        # The composer, not the kind, says whether there is a subject field.
        is_inmail = state.get("subject") is True
        if cost is None and not spend_inmail_credit:
            return (
                INMAIL_REPLY_COST_UNKNOWN,
                f"Sales Navigator did not show whether this message to {who} "
                "uses an InMail credit: the conversation is an InMail that was "
                "not accepted or answered, or is with someone who is not a "
                "connection. Nothing was typed or sent. Set "
                "spend_inmail_credit=true with confirm_send=true to send it "
                "anyway; it may use one InMail credit.",
            )
        what = "may use an InMail credit" if cost is None else "is an InMail"
        if cost != 0 and credits is None:
            return (
                INMAIL_CREDITS_UNKNOWN,
                f"This message {what}, and Sales Navigator did not say how "
                "many InMail credits are left, so nothing was typed or sent.",
            )
        if cost != 0 and credits is not None and credits < 1:
            return (
                NO_INMAIL_CREDITS,
                f"This message {what} and no InMail credits are left, so "
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
            return (
                "confirmation_required",
                f"Nothing was sent. The message would go to {who}. "
                f"{_cost_text(delivery)} Set confirm_send=true to send it.",
            )
        return None

    async def reply_to_conversation(
        self,
        thread_id: str,
        message: str,
        *,
        confirm_send: bool,
        spend_inmail_credit: bool = False,
    ) -> dict[str, Any]:
        """Reply inside one Sales Navigator conversation, gated by confirmation.

        The thread id is the authorization: the page must load exactly that
        conversation's own response and stay on its route. A reply composer
        that asks for a subject would start a new InMail and is refused. A
        reply whose cost the conversation does not show (an InMail not yet
        accepted or answered) needs ``spend_inmail_credit``.
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
                viewer = thread_viewer(capture.responses, response.payload)
                participant = thread_participant(response.payload, record, viewer)
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
                    inmail_thread, cost = reply_cost(response.payload, record, viewer)
                    if cost != 0:
                        await self._credits(capture)
                    await capture.settle()
                    delivery: Delivery = {
                        "kind": "inmail" if inmail_thread else "message",
                        "creditCost": cost,
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
                    who = participant or "the participant of this conversation"
                    refused = self._gate(
                        state,
                        delivery,
                        subject=None,
                        spend_inmail_credit=spend_inmail_credit,
                        confirm_send=True,
                        who=who,
                    )
                    if refused is None and not confirm_send:
                        refused = (
                            "confirmation_required",
                            f"Nothing was sent. The message would go to {who} as "
                            f"a reply in this conversation. {_cost_text(delivery)} "
                            "Set confirm_send=true to send it.",
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
                return await self._submit(
                    message,
                    None,
                    owner=owner,
                    capture=capture,
                    expected_route=expected_route,
                    budget_deadline=budget_deadline,
                    respond=respond,
                    on_sent={},
                    delivery=delivery,
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

    async def _wait_for_submit(self, owner: Any, argument: dict[str, Any]) -> str:
        """'ready', 'disabled' (still, at the deadline) or 'invalid'."""
        deadline = time.monotonic() + _SUBMIT_READY_TIMEOUT_SECONDS
        while True:
            state = await owner.evaluate(js.SUBMIT_READY_JS, argument)
            if state != "disabled":
                return "ready" if state == "ready" else "invalid"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "disabled"
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
        delivery: Delivery,
    ) -> dict[str, Any]:
        """Type into the pinned composer, click its Send once, confirm the send.

        ``retry_safe`` turns false from the moment the click is attempted, and
        after any keystroke whose effect nothing proves. ``on_sent`` is added
        to a confirmed send's answer only. The read-state guard stays latched
        throughout; only the click opens it, for one ``createMessage``.
        """
        try:
            return await self._type_and_send(
                message,
                subject,
                owner=owner,
                capture=capture,
                expected_route=expected_route,
                budget_deadline=budget_deadline,
                respond=respond,
                on_sent=on_sent,
                delivery=delivery,
            )
        finally:
            close_sales_send(self._page)

    async def _type_and_send(
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
        delivery: Delivery,
    ) -> dict[str, Any]:
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
            ready = await self._wait_for_submit(owner, argument)
            if ready != "ready":
                if ready == "disabled" and _free_at_zero_credits(delivery):
                    return respond(
                        self._page.url,
                        SEND_BLOCKED_NO_CREDITS,
                        "Sales Navigator did not enable Send for this message "
                        "while the account has no InMail credits left, so "
                        "nothing was sent and the draft was cleared.",
                    )
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
                    open_one_sales_send(self._page)
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
                    zero = (
                        "The account has no InMail credits left. "
                        if _free_at_zero_credits(delivery)
                        else ""
                    )
                    return respond(
                        self._page.url,
                        "send_unconfirmed",
                        f"Send was clicked and Sales Navigator answered HTTP "
                        f"{answer.status}. " + zero + _UNCONFIRMED,
                        retry_safe=False,
                    )
                # An earlier createMessage the guard aborted is not the sent one.
                sent_request = allowed_sales_send(self._page, capture.send_requests)
                delivered = sent_body(sent_request) if sent_request else None
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
