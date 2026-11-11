"""Locale-independent connection-state detection from profile top-card signals.

LinkedIn translates every visible label, but the URLs it links to, the
attributes it sets and the component keys it renders do not depend on UI
language. Detection here uses (measured live 2026-11-11):

* ``/in/USER/edit/intro/`` anchor → self profile
* ``[componentkey^="ConnectButtonstate:"]`` in the top card or its More menu:
  a ``_pending`` key suffix → pending, ``_connect`` → connectable
* ``/edit/forms/recommendation/`` anchors in the More menu → 1st degree
* the ``xmark-outline-medium`` icon on a More-menu item (Ignore) together
  with a direct Message (``/messaging/compose/?profileUrn=``) in the menu and
  none in the top card → incoming request

Per the AGENTS.md Scraping Rules no label value is read for any state. The
component key and the icon id are design-system tokens, not words: the
Pending and Connect items differ in nothing else that is not translated.

A state is only reported on positive evidence. A profile whose top card
or More menu could not be read, or whose signals only half match a state,
is ``unavailable``, never a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ConnectionState = Literal[
    "already_connected",
    "pending",
    "incoming_request",
    "connectable",
    "follow_only",
    "self_profile",
    "unavailable",
]


@dataclass(frozen=True)
class ActionSignals:
    """Structural signals read from the profile's top card and More menu.

    The top card is the innermost ``<section>`` around the profile's
    ``/overlay/contact-info/`` anchor, outside ``main aside``: the sidebar's
    "People you may know" cards carry the same component keys and labeled
    Message anchors, and reading them as this profile's state is how a
    never-invited profile used to report ``pending``. Only rendered
    elements count, because LinkedIn renders the action row twice and the
    sticky-header copy is hidden.
    """

    has_top_card: bool
    """Exactly one top-card section was found. Without it nothing else
    here was read."""

    has_edit_intro_anchor: bool
    """``a[href*="/in/{user}/edit/intro/"]`` exists in ``<main>``. Only
    rendered when viewing your own profile."""

    has_pending_item: bool
    """A ``ConnectButtonstate`` key ending ``_pending`` in the top card or
    the More menu: an invitation from this account is outstanding."""

    has_connect_item: bool
    """A ``ConnectButtonstate`` key ending ``_connect`` in the top card or
    the More menu: LinkedIn offers Connect, and that element is what the
    send path clicks."""

    menu_read: bool
    """The top card's More menu was opened and read. False when the top
    card already answered (pending or connectable there) and when the menu
    could not be opened; the fields below are meaningless without it."""

    has_recommendation_link: bool
    """The More menu links to ``/edit/forms/recommendation/`` (Request a
    recommendation, Recommend), which LinkedIn offers 1st-degree
    connections only."""

    has_ignore_item: bool
    """A More-menu item carries the ``xmark-outline-medium`` icon, which
    is the Ignore action on an incoming invitation (measured on one
    profile; the 1st-degree Remove connection item is
    ``person-xmark-outline-medium`` and does not match)."""

    has_message_item_in_menu: bool
    """The More menu holds a direct Message, ``/messaging/compose/`` with
    ``profileUrn=``, as opposed to the ``body=`` "Send profile in a
    message" every measured menu carries. On the measured incoming request
    Message had moved there from the top card."""

    has_compose_anchor_in_top_card: bool
    """A rendered ``a[href*="/messaging/compose/"]`` in the top card. On
    the measured incoming request Message had moved into the More menu and
    Accept stood in the top card instead."""


def detect_connection_state(signals: ActionSignals) -> ConnectionState:
    """Determine the relationship state for a profile from structural signals.

    Resolution order:

    1. ``self_profile`` — edit-intro anchor (URL).
    2. ``unavailable`` — no top card, so nothing below was read.
    3. ``pending`` — a ``_pending`` connect-state key. Ahead of
       ``connectable`` so that a page somehow carrying both can never
       trigger a write.
    4. ``connectable`` — a ``_connect`` connect-state key.
    5. ``unavailable`` — the More menu could not be read. Absence of a
       state key proves nothing until the menu, where LinkedIn puts it on
       Follow-primary and Message-primary profiles, has been seen.
    6. ``already_connected`` — recommendation links in the menu.
    7. ``incoming_request`` — the Ignore item and a direct Message in the
       menu, and no compose anchor in the top card. One icon id alone is
       too thin for an action that accepts, and "no Message in the top
       card" is true of every Follow-primary profile.
    8. ``unavailable`` — the Ignore item without the rest: something is
       there that no measured state explains.
    9. ``follow_only`` — the menu was read and offers no Connect. The
       caller still treats this as unconfirmed, because a menu can change
       shape without the probe noticing.
    """
    if signals.has_edit_intro_anchor:
        return "self_profile"
    if not signals.has_top_card:
        return "unavailable"
    if signals.has_pending_item:
        return "pending"
    if signals.has_connect_item:
        return "connectable"
    if not signals.menu_read:
        return "unavailable"
    if signals.has_recommendation_link:
        return "already_connected"
    if signals.has_ignore_item:
        if (
            signals.has_message_item_in_menu
            and not signals.has_compose_anchor_in_top_card
        ):
            return "incoming_request"
        return "unavailable"
    return "follow_only"
