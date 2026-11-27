"""Who ``connect_with_person`` is asked to invite, read before any page load.

A caller names a member by public identifier, or by one of the ids the other
tools return: an ``ACoA``/``ACwA`` profile id (``search_sales_navigator_leads``
returns the ``ACwA`` one as ``id`` and as ``profileUrl`` ``/in/<id>``), a
Sales Navigator lead URN or URL, or a ``urn:li:fsd_profile:`` URN. The forms
are read by :func:`page_invite_payload.invitee_ref`, the reader
``invite_to_follow_page`` uses, so the two tools agree on what names whom.

An id is not where the invite write looks. It confirms the invitation against
the member's ``/in/<public identifier>/`` address, so an id is resolved the way
LinkedIn does it: ``/in/<id>/`` lands on the member's public address. Measured
2026-11-27 for one Sales Navigator lead: ``/in/ACwAA.../`` loaded the member's
``/in/<vanity>/`` page, whose top card carried an ``ACoAA`` profile URN that
decodes to the same member number as the ``ACwAA`` id. The redirect is not the
proof, though. A profile id is resolved only when the page that loaded carries
a profile URN that is the member asked for (:meth:`ConnectTarget.confirms`),
and the write path checks that again on the page it acts on. Anything else is
refused with nothing sent, because this write acts as the user.

A ``urn:li:member:<n>`` URN is read but refused: no address that opens a
profile by member number has been measured, and the member number alone
cannot be turned into a profile id (the ids carry bytes of their own after
it). The lead row that carries the URN carries its ``id`` too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.scraping.identifiers import normalize_person_identifier
from linkedin_mcp_server.scraping.page_invite_payload import invitee_ref, member_id_of

__all__ = ["ConnectTarget", "connect_target", "is_opaque_member_id"]

# A member id LinkedIn mints, not a public identifier: ``ACoAA...`` (the
# member URN's id) or ``ACwAA...`` (a Sales Navigator profile id). LinkedIn's
# are 39 characters, 34 after the prefix; this asks for 30 or more, and is
# case-sensitive, so a vanity name that starts with "aco" or "ACo" stays one.
_OPAQUE_MEMBER_ID = re.compile(r"^AC[ow]AA[A-Za-z0-9_-]{30,}$")


def is_opaque_member_id(value: str) -> bool:
    """Whether ``value`` is an id LinkedIn mints rather than a public name."""
    return bool(_OPAQUE_MEMBER_ID.fullmatch(value))


@dataclass(frozen=True, slots=True)
class ConnectTarget:
    """A member named by profile id, to be resolved to a public identifier.

    ``profile_id`` is the ``ACoA``/``ACwA`` id ``/in/<id>/`` opens. A page is
    this member only when its profile URN is ``exact`` (an ``ACoA`` id the
    caller gave) or decodes to ``member_id``.
    """

    given: str
    profile_id: str
    member_id: str | None
    exact: str | None

    def confirms(self, profile_urn: object) -> bool:
        """Whether a page's profile URN is the member asked for."""
        if not isinstance(profile_urn, str) or not profile_urn:
            return False
        if self.exact is not None and profile_urn == self.exact:
            return True
        return (
            self.member_id is not None and member_id_of(profile_urn) == self.member_id
        )


def connect_target(value: str) -> str | ConnectTarget:
    """The public identifier, or the id to resolve, that ``value`` names.

    Raises:
        InvalidReferenceError: when ``value`` names nobody, or names a member
            this tool cannot confirm on a page (a ``urn:li:member`` URN, an id
            that neither is an ``ACoA`` id nor decodes to a member number).
            Nothing was sent.
    """
    ref = invitee_ref(value)
    encoded = ref.encoded if ref is not None else None
    if ref is not None and ref.kind == "member" and encoded is None:
        raise InvalidReferenceError(
            "connect_with_person cannot open a profile from a urn:li:member "
            "URN. Pass the lead's id (ACw...) or profileUrl that came with it, "
            "or the member's public identifier. Nothing was sent."
        )
    if ref is None or encoded is None or not _OPAQUE_MEMBER_ID.fullmatch(encoded):
        # A public identifier, or nothing an id reader recognises: the strict
        # reader decides, and explains a refusal in its own words. It reads
        # some links the id reader does not (a site-relative ``/in/<id>/``),
        # so an id it hands back is read again as one, never invited by.
        username = normalize_person_identifier(value)
        if username != value and _OPAQUE_MEMBER_ID.fullmatch(username):
            return connect_target(username)
        return username
    member = ref.key if ref.kind == "member" else None
    if member is None and ref.exact is None:
        raise InvalidReferenceError(
            f"connect_with_person cannot confirm who {encoded} is: it is not a "
            "member id this tool can match on the profile page. Pass the "
            "member's public identifier instead. Nothing was sent."
        )
    return ConnectTarget(value, encoded, member, ref.exact)
