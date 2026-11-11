"""Section contracts shared by every scraping workflow."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

import anyio

from linkedin_mcp_server.exceptions import ActionLimitError
from linkedin_mcp_server.scraping.identifiers import (
    messaging_thread_url,
    normalize_person_identifier,
    normalize_thread_id,
    person_profile_url,
)
from linkedin_mcp_server.scraping.link_metadata import Reference

# Returned as section text when a page comes back with its content gone and
# only LinkedIn's own navigation and footer left.
#
# Read carefully: that condition is a *guess* that the page was throttled, not
# an observation of one. It arrived in d8b4c62 with no cited evidence, LinkedIn
# documents no such behaviour, and nobody here has reproduced it deliberately —
# doing so would mean provoking a real throttle on a real account. The log line
# hedges with "likely" for the same reason.
#
# The same empty shell could also be a layout change, a resource this account
# cannot see, or a load that gave up. A session LinkedIn ended is the one
# alternative already ruled out elsewhere: every navigation checks the URL
# against the auth-blocker patterns first, and a redirect to /login, /authwall
# or /checkpoint raises before extraction is reached. That check stays on URLs
# deliberately — body text would be a per-locale guess, and this project's
# rule is that classification never depends on text values.
RATE_LIMITED_SECTION_TEXT = "[Rate limited] LinkedIn blocked this section. Try again later or request fewer sections."

# A submission is in flight from the moment the send is dispatched until the
# whole path has produced a result, cleanup included, and a cancellation in
# that window cannot be reported: it raises `CancelledError` past
# `except Exception`, and a cancelled scope discards whatever is returned from
# inside it. The tool's own deadline no longer lands there, because the send
# stops ahead of it and answers `send_unconfirmed` (upstream #889). What is
# left is cancellation the server does not own, a client that cancels or goes
# away, and for that this line is the only record that a message may already
# have left.
SEND_INTERRUPTED_WARNING = (
    "Message submission was interrupted while in flight. The send outcome is "
    "unknown; check the conversation before retrying, as a retry may deliver "
    "the message twice."
)


def before_the_reply_deadline(
    limit: float = math.inf, *, shield: bool = False
) -> anyio.CancelScope:
    """Bound work that runs while a send's answer waits to leave.

    The scope ends after ``limit`` seconds and never later than halfway to the
    deadline the call runs under, so what follows keeps the other half to hand
    the answer back before that deadline discards it. A shielded scope ignores
    that deadline, so without this bound a slow cleanup outlasts it. Without a
    deadline, or once the call is already cancelled and its answer gone, only
    ``limit`` applies.
    """
    now = anyio.current_time()
    end = now + limit
    deadline = anyio.current_effective_deadline()
    if now < deadline < math.inf:
        end = min(end, now + (deadline - now) / 2)
    return anyio.CancelScope(deadline=end, shield=shield)


def rate_limited_section_error() -> dict[str, str]:
    """The ``section_errors`` entry for a section that came back empty.

    One shape for every caller, because the alternative is what this codebase
    did until now: most call sites dropped the sentinel and returned the
    section as simply absent. An agent reading an empty section with no error
    concludes there was nothing to find and calls again, which is the opposite
    of what a rate limit asks for. Being told is what lets a client back off.

    Note this reports the *heuristic's* verdict, with the caveats on
    ``RATE_LIMITED_SECTION_TEXT`` above, and does not make it more accurate.
    What it changes is that a wrong verdict is now visible and can be argued
    with, where a silently missing section could not be.
    """
    return {
        "error_type": "rate_limit",
        "error_message": RATE_LIMITED_SECTION_TEXT,
    }


def limit_exceeded_section_error(error: ActionLimitError) -> dict[str, Any]:
    """The ``section_errors`` entry for a section a cap refused to load.

    Filed once, for the section the walk stopped at, with the fields a caller
    needs to wait it out; the sections loaded before it are returned as paid
    for. ``ActionLimitError.from_section_error`` reads this back.
    """
    return {
        "error_type": error.error_type,
        "error_message": str(error),
        "kind": error.kind,
        "limit": error.limit,
        "window": error.window,
        "resume_at": error.resume_at.isoformat(),
    }


class _Omitted:
    """A result field the caller did not pass."""


_OMITTED = _Omitted()


def message_action_result(
    url: str,
    status: str,
    message: str,
    *,
    recipient_selected: bool = False,
    sent: bool = False,
    retry_safe: bool = True,
    thread_id: str | None | _Omitted = _OMITTED,
) -> dict[str, Any]:
    """Build a structured response for the send_message tool.

    ``sent`` is true only when the narrowly defined message-list UI transition
    was observed after submission. It does not prove delivery or that the
    recipient read the message. A caller keying a retry on it alone can re-send
    a message that may already have arrived, which is what ``retry_safe`` exists
    to say: it is false from the moment a submission is attempted, and true only
    while nothing can have left the composer.

    ``thread_id`` is present only when the caller passes it, including when
    the value is null. A confirmed send passes the conversation the message
    was observed in, or null when that page stayed on the compose route.
    """
    result: dict[str, Any] = {
        "url": url,
        "status": status,
        "message": message,
        "recipient_selected": recipient_selected,
        "sent": sent,
        "retry_safe": retry_safe,
    }
    if not isinstance(thread_id, _Omitted):
        result["thread_id"] = thread_id
    return result


_NEWLINE_RUN_RE = re.compile(r"\n{3,}")


def normalize_message_line_breaks(message: str) -> str:
    """Return the message with CRLF as LF, 3+ newlines collapsed to 2 and the
    leading and trailing newlines removed."""
    return _NEWLINE_RUN_RE.sub("\n\n", message.replace("\r\n", "\n")).strip("\n")


def reply_action_result(
    url: str,
    status: str,
    message: str,
    *,
    thread_id: str,
    participant: str | None,
    recipient_selected: bool = False,
    sent: bool = False,
    retry_safe: bool = True,
) -> dict[str, Any]:
    """``message_action_result`` for reply_to_conversation, plus its thread.

    ``participant`` is the name the thread's title showed, or None where it
    could not be read; it is reported, never used to decide anything.
    """
    return {
        **message_action_result(
            url,
            status,
            message,
            recipient_selected=recipient_selected,
            sent=sent,
            retry_safe=retry_safe,
        ),
        "thread_id": thread_id,
        "participant": participant,
    }


def _invalid_message_reason(message: str) -> str | None:
    if not message.strip():
        return "Message must contain non-whitespace characters."
    if any(
        (ord(character) < 32 and character != "\n") or ord(character) == 127
        for character in normalize_message_line_breaks(message)
    ):
        # Keep the browser-side insertion contract to plain message text plus
        # line breaks. Reject every other C0 control and DEL (a lone CR
        # included) before a session is acquired so no control input can reach
        # the contenteditable surface.
        return "Message must not contain control characters other than line breaks."
    return None


def refuse_an_invalid_message(
    linkedin_username: str, message: str
) -> dict[str, Any] | None:
    """Return the shared browser-free refusal for an unsafe message."""
    reason = _invalid_message_reason(message)
    if reason is None:
        return None
    return message_action_result(
        person_profile_url(normalize_person_identifier(linkedin_username), "/"),
        "invalid_message",
        reason,
    )


def refuse_an_invalid_reply(thread_id: str, message: str) -> dict[str, Any] | None:
    """The same refusal for a reply, addressed to the thread."""
    reason = _invalid_message_reason(message)
    if reason is None:
        return None
    thread_id = unquote(normalize_thread_id(thread_id))
    return reply_action_result(
        messaging_thread_url(thread_id, "/"),
        "invalid_message",
        reason,
        thread_id=thread_id,
        participant=None,
    )


@dataclass
class ExtractedSection:
    """Text and compact references extracted from a loaded LinkedIn section."""

    text: str
    references: list[Reference]
    error: dict[str, Any] | None = None
    # Where the browser was when the text was read, set only on a page that
    # yielded text. LinkedIn can move a navigation client-side to a different
    # page (the job search to its AI search), and a caller that has to know
    # which page it read cannot tell from the text.
    landed_url: str | None = None


class FilterValidationError(ValueError):
    """Invalid ``search_people`` filter input (network token / URN shape).

    Subclassing ``ValueError`` keeps backward-compatible behaviour for
    direct extractor callers (``pytest.raises(ValueError)`` matches), while
    letting the MCP tool wrapper catch this case precisely and surface the
    actionable message past ``mask_error_details``.
    """
