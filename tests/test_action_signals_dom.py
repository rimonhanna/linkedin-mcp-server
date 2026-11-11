# tests/test_action_signals_dom.py
"""Browser-DOM tests for the locale independence of the action-area reads.

The unit suite mocks ``page.evaluate``, so the programs in
``scraping/connection_actions.py`` never execute there. These tests run the
real ones against synthetic HTML in headless chromium, with every network
request aborted: nothing here can reach LinkedIn.

Every fixture is built from one set of templates, three sets of words and
one set whose ARIA values are empty: English, German, opaque tokens carrying
no verb, and present-but-empty attributes. The structure is identical across
all four, and each case asserts the *same* answer for all of them in one
assertion that names the locales. A decision that differs between two of
them is a decision that read a word, which the AGENTS.md Scraping Rules
forbid; the opaque set is the control, because a label with no verb in it
cannot be matched by one.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``. CI installs it, so these
run there, but a skip is still the right answer for a missing browser here:
this file checks extraction JS rather than the browser, and
``test_browser_identity.py`` is the one that has to fail instead.

The profile fixtures are sanitized copies of the top cards and More menus
measured live on 2026-11-11 (never invited and Follow-primary,
Connect-primary, pending, 1st degree, incoming request): names, vanity names, member ids and
component-key uuids are synthetic, while the element structure, the
attributes and the shape of every ``componentkey`` are kept. Each page also
carries what made the old probe wrong: the "People you may know" sidebar in
``main aside`` with labeled Message anchors and other members' connect-state
keys, the sticky-header copy of the action row (hidden at the top of the
page, and in some cases shown, the way it is once the page has scrolled),
and a hidden video subtitles ``[role=menu]`` ahead of the profile's own menu
in DOM order. The More menu is portal-mounted outside ``<main>`` and renders
150 ms after the click, as the live one does.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields
from typing import Any, cast

import pytest
from patchright.async_api import Page, async_playwright

from linkedin_mcp_server.scraping.connection import (
    ConnectionState,
    detect_connection_state,
)
from linkedin_mcp_server.scraping.connection_actions import (
    _MODAL_DIALOG_INDEX_JS,
    _SENT_LIST_HAS_USER_JS,
    CLICK_CONNECT_JS,
    CLICK_INCOMING_ACCEPT_JS,
    ConnectionActions,
    _InviteDialog,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

#: CI uses ``--dist loadgroup``. Keep every test that launches Chromium on one
#: worker so browser startups cannot compete with the DOM cases' wall-clock
#: timers.
#: Without that distribution mode the group mark is inert.
pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

USER = "testuser"
#: The target's member id, inside its connect-state key.
MEMBER = "100000001"
PROFILE_URN = "ACoAAAtestuser0000000000000000000000"


@dataclass(frozen=True, slots=True)
class Labels:
    """Every visible string and aria-label value one locale contributes.

    Only these values change between the four fixture sets, so nothing else
    can explain a decision that changes with them.
    """

    locale: str
    more: str
    message: str
    connect: str
    pending: str
    follow: str
    following: str
    save_lead: str
    accept: str
    ignore: str
    remove: str
    recommend: str
    share: str
    report: str
    notify: str
    edit: str
    play: str
    settings: str
    subtitles: str
    comment: str


ENGLISH = Labels(
    locale="en",
    more="More",
    message="Message",
    connect="Invite Avery Quill to connect",
    pending="Pending, click to withdraw invitation sent to Avery Quill",
    follow="Follow Avery Quill",
    following="Following, click to unfollow Avery Quill",
    save_lead="Save Avery’s as a lead in Sales Navigator",
    accept="Accept Avery Quill’s request to connect",
    ignore="Ignore Avery Quill’s request to connect",
    remove="Remove connection",
    recommend="Request a recommendation",
    share="Send profile in a message",
    report="Report / Block",
    notify="Manage notifications about Avery Quill",
    edit="Edit intro",
    play="Play",
    settings="Settings",
    subtitles="Subtitles",
    comment="Comment",
)

GERMAN = Labels(
    locale="de",
    more="Mehr",
    message="Nachricht",
    connect="Avery Quill als Kontakt einladen",
    pending="Ausstehend, klicken zum Zurückziehen der Einladung an Avery Quill",
    follow="Avery Quill folgen",
    following="Gefolgt, klicken um Avery Quill nicht mehr zu folgen",
    save_lead="Avery als Lead im Sales Navigator speichern",
    accept="Kontaktanfrage von Avery Quill annehmen",
    ignore="Kontaktanfrage von Avery Quill ignorieren",
    remove="Kontakt entfernen",
    recommend="Empfehlung anfordern",
    share="Profil in einer Nachricht senden",
    report="Melden/Blockieren",
    notify="Benachrichtigungen zu Avery Quill verwalten",
    edit="Intro bearbeiten",
    play="Abspielen",
    settings="Einstellungen",
    subtitles="Untertitel",
    comment="Kommentieren",
)

# No verb anywhere, in any language: these labels are identifiers. Whatever
# still classifies correctly here is reading structure, and this set is the
# one a locale table could not rescue.
OPAQUE = Labels(
    locale="opaque",
    more="c8a0b5",
    message="d1f6e2",
    connect="e4b7a9",
    pending="a9e2f7",
    follow="f0c3d8",
    following="b1d9e4",
    save_lead="c3e7f0",
    accept="a7f3c1",
    ignore="b2e9d4",
    remove="d5a1c8",
    recommend="e2c6b9",
    share="f8b3a5",
    report="a4d2e7",
    notify="b6f1c3",
    edit="b5d8c0",
    play="c2f4a6",
    settings="a0b4e6",
    subtitles="e9a5b8",
    comment="d4e8b7",
)

# Attribute presence and attribute truthiness are different contracts. This
# set keeps every aria-label attribute in the markup while making its value
# empty, so `hasAttribute` survives and `getAttribute(...)` truthiness does not.
EMPTY_ARIA = Labels("empty-aria", *("" for _ in fields(Labels)[1:]))

LOCALES = (ENGLISH, GERMAN, OPAQUE, EMPTY_ARIA)

Build = Callable[[Labels], str]


# --- page scaffolding -------------------------------------------------------

# The More button toggles the portal-mounted menu after a delay, Escape
# closes it, and every control records its clicks on <body>. Patchright
# evaluates in an isolated world, so page-world variables are invisible to
# the programs, but the DOM is shared.
_PAGE_SCRIPT = r"""
<style>
  [data-display-contents="true"] { display: contents; }
  [role="menuitem"] { display: block; }
</style>
<script>
  function record(what) {
    const before = document.body.getAttribute('data-clicked');
    document.body.setAttribute('data-clicked', before ? before + ' ' + what : what);
  }
  function toggleProfileMenu(button) {
    record('more');
    const menu = document.getElementById('profile-menu');
    if (button.getAttribute('aria-expanded') === 'true') {
      menu.hidden = true;
      button.setAttribute('aria-expanded', 'false');
      return;
    }
    setTimeout(() => {
      menu.hidden = false;
      button.setAttribute('aria-expanded', 'true');
      // A menu that renders in two batches: the rest arrives later.
      const late = document.getElementById('late-items');
      if (late && !menu.dataset.grown) {
        menu.dataset.grown = 'yes';
        setTimeout(() => menu.append(late.content.cloneNode(true)), 1200);
      }
    }, 150);
  }
  // Once the page scrolls, the sticky copy shows and the in-flow row hides.
  window.addEventListener('scroll', () => {
    const scrolled = window.scrollY > 0;
    for (const el of document.querySelectorAll('[data-sticky="on-scroll"]')) {
      el.style.display = scrolled ? 'block' : 'none';
    }
    for (const el of document.querySelectorAll('[data-live-row="on-scroll"]')) {
      el.style.visibility = scrolled ? 'hidden' : 'visible';
    }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    const menu = document.getElementById('profile-menu');
    if (menu) menu.hidden = true;
    for (const b of document.querySelectorAll('button[aria-expanded="true"]')) {
      b.setAttribute('aria-expanded', 'false');
    }
  });
</script>
"""


def _onclick(what: str) -> str:
    return f"record('{what}'); event.preventDefault();"


def _page_html(main: str, menu: str = "", late: str = "") -> str:
    portal = (
        f'<div tabindex="-1"><div id="profile-menu" role="menu" hidden>{menu}</div></div>'
        if menu
        else ""
    )
    template = f'<template id="late-items">{late}</template>' if late else ""
    return (
        f"<html><head>{_PAGE_SCRIPT}</head><body>"
        f'<header><a href="https://www.linkedin.com/in/{USER}/">Me</a></header>'
        f"<main>{main}</main>{portal}{template}</body></html>"
    )


@dataclass(frozen=True, slots=True)
class Profile:
    """One profile page: the action-row controls and the More-menu items.

    ``row`` builds the controls between the name block and More, ``menu``
    the items of the portal-mounted menu, and ``menu_late`` items that
    arrive 1.2 s after the rest. ``card_extra`` lands in the top card ahead
    of everything else (a cover video, an edit anchor). ``sticky`` is the
    sticky-header copy of the row: ``hidden`` (top of the page, as
    measured), ``shown`` or ``shown-first`` (rendered, after or before the
    in-flow row in DOM order), ``invisible`` (laid out but
    ``visibility: hidden``), or ``on-scroll`` (shown, with the in-flow row
    hidden, whenever the page is scrolled).
    """

    row: Build
    menu: Build | None
    card_extra: Build = lambda labels: ""
    more_opens_menu: bool = True
    contact_info: bool = True
    sticky: str = "hidden"
    menu_late: Build | None = None

    def html(self, labels: Labels) -> str:
        return _page_html(
            _column(self, labels) + _sidebar(labels),
            self.menu(labels) if self.menu else "",
            self.menu_late(labels) if self.menu_late else "",
        )


def _icon(icon_id: str) -> str:
    return f'<svg id="{icon_id}" aria-hidden="true" width="16" height="16"></svg>'


def _more(labels: Labels, *, live: bool, key: str) -> str:
    handler = "toggleProfileMenu(this)" if live else "record('sticky-more')"
    return (
        '<div data-display-contents="true">'
        f'<button type="button" componentkey="{key}" aria-label="{labels.more}"'
        f' aria-expanded="false" onclick="{handler}">'
        f"<span>{_icon('ellipses-horizontal-fill-small')}</span></button></div>"
    )


_STICKY_HEADER = "position:fixed; top:0; left:0; right:0; background:#fff"


def _action_row(profile: Profile, labels: Labels, *, live: bool) -> str:
    """One copy of the action row: the in-flow one or the sticky header's."""
    prefix = "" if live else "sticky-"
    controls = profile.row(labels).replace("record('", f"record('{prefix}")
    more = _more(
        labels,
        live=live and profile.more_opens_menu,
        key="5d8e62c6-0000-4000-8000-00000000000" + ("1" if live else "2"),
    )
    row = f"<div><div><div>{controls}{more}</div></div></div>"
    if live:
        if profile.sticky == "on-scroll":
            return f'<div data-live-row="on-scroll">{row}</div>'
        return row
    if profile.sticky == "hidden":
        return f'<div style="display:none">{row}</div>'
    if profile.sticky == "invisible":
        return f'<div style="visibility:hidden">{row}</div>'
    if profile.sticky == "on-scroll":
        return (
            f'<div data-sticky="on-scroll" style="{_STICKY_HEADER}; display:none">'
            f"{row}</div>"
        )
    return f'<div style="{_STICKY_HEADER}">{row}</div>'


def _top_card(profile: Profile, labels: Labels) -> str:
    contact = (
        f'<p><a href="https://www.linkedin.com/in/{USER}/overlay/contact-info/">'
        "Contact info</a></p>"
        if profile.contact_info
        else ""
    )
    return f"""
<section componentkey="2cb22c94-0000-4000-8000-000000000001">
  {profile.card_extra(labels)}
  <div>
    <a tabindex="0" href="https://www.linkedin.com/in/{USER}/"
      componentkey="topcard-logo-image-referencekey">
      <div componentkey="topcard-logo-image-referencekey" aria-label="Profile photo">
        {_icon("person-accent-4")}</div></a>
    <a aria-disabled="false" href="https://www.linkedin.com/in/{USER}/"
      componentkey="8fda384f-0000-4000-8000-000000000001"
      aria-label="{labels.notify}"><span>{_icon("bell-outline-medium")}</span></a>
  </div>
  <div>
    <div data-display-contents="true">
      <a tabindex="0" href="https://www.linkedin.com/in/{USER}/"
        componentkey="ProfileVerificationTriggerRef-{USER}"
        aria-haspopup="dialog" aria-expanded="false">
        <div componentkey="ProfileVerificationTriggerRef-{USER}">
          <h2>Avery Quill</h2></div></a>
    </div>
    <p>· 2nd</p>
    <p>Founder, Example Labs</p>
    {contact}
  </div>
  <a tabindex="0"
    href="https://www.linkedin.com/search/results/people/?origin=MEMBER_PROFILE_CANNED_SEARCH&amp;connectionOf=%5B%22{PROFILE_URN}%22%5D"
    componentkey="2acb4a41-0000-4000-8000-000000000001">
    <div><p><span>Sam, Kim and 12 other mutual connections</span></p></div></a>
  <div data-display-contents="true">
    <div>
      {_rows(profile, labels)}
    </div>
  </div>
  <hr role="presentation">
  <a tabindex="0" href="https://www.linkedin.com/in/{USER}/"
    componentkey="e215e7cd-0000-4000-8000-000000000001">
    <div>{_icon("premium-chip-fill-color-small")}<p>Profile enhanced with Premium</p></div></a>
</section>
"""


def _rows(profile: Profile, labels: Labels) -> str:
    rows = [
        _action_row(profile, labels, live=True),
        _action_row(profile, labels, live=False),
    ]
    return "".join(reversed(rows) if profile.sticky == "shown-first" else rows)


def _activity(labels: Labels) -> str:
    """The rest of the profile column: a post with a video and its menus."""
    return f"""
<section componentkey="activity">
  <div class="video-player">
    <button type="button" aria-label="{labels.play}">&#9654;</button>
    <button type="button" aria-label="{labels.settings}" aria-expanded="false">&#9881;</button>
    <div role="menu" style="display:none">
      <div role="menuitem" tabindex="0">{labels.subtitles}</div>
    </div>
  </div>
  <a href="https://www.linkedin.com/feed/update/urn:li:activity:1/"
    aria-label="{labels.comment}">{labels.comment}</a>
  <div style="height:4000px"></div>
</section>
"""


def _column(profile: Profile, labels: Labels) -> str:
    return (
        '<div><div><section componentkey="profile-column"><div><div>'
        + _top_card(profile, labels)
        + _activity(labels)
        + "</div></div></section>"
    )


def _sidebar(labels: Labels) -> str:
    """ "People you may know": what the old action-root walk latched onto.

    Labeled Message anchors, and connect-state keys for *other* members,
    one of them pending: read from here, a never-invited profile is
    pending.
    """
    cards = []
    for index, state in ((2, "connect"), (3, "pending"), (4, "connect")):
        cards.append(f"""
  <div componentkey="sidebar-card-{index}">
    <a href="https://www.linkedin.com/in/other-member-{index}/">Other {index}</a>
    <a href="/messaging/compose/?profileUrn=urn%3Ali%3Afsd_profile%3AACoAAAother{index}"
      aria-label="{labels.message} Other {index}">{labels.message}</a>
    <button type="button"
      componentkey="ConnectButtonstate:invitation:urn:li:member:20000000{index}_{state}"
      aria-label="{labels.connect if state == "connect" else labels.pending}"
      onclick="record('sidebar-{state}')">{_icon("person-plus-outline-medium")}</button>
  </div>""")
    return (
        "<aside><section><h2>People you may know</h2>"
        + "".join(cards)
        + "</section></aside></div></div>"
    )


# --- action-row controls ----------------------------------------------------


def _message_anchor(labels: Labels) -> str:
    return (
        '<div data-display-contents="true">'
        '<a aria-disabled="false"'
        f' href="/messaging/compose/?profileUrn=urn%3Ali%3Afsd_profile%3A{PROFILE_URN}'
        f'&amp;recipient={PROFILE_URN}"'
        ' componentkey="d28d3e78-0000-4000-8000-000000000001"'
        f' onclick="{_onclick("message")}">'
        f"<span>{_icon('paper-plane-diagonal-outline-small')}"
        f"<span>{labels.message}</span></span></a></div>"
    )


def _following_button(labels: Labels) -> str:
    return (
        '<div componentkey="d1e729b9-0000-4000-8000-000000000001">'
        '<div data-display-contents="true"><button type="button"'
        ' componentkey="auto-component-f9aeb7a4-0000-4000-8000-000000000001"'
        f' aria-label="{labels.following}" onclick="record(\'following\')">'
        f"<span>{_icon('check-outline-small')}<span>{labels.following}</span>"
        "</span></button></div></div>"
    )


def _save_button(labels: Labels) -> str:
    return (
        '<div><div data-display-contents="true"><button type="button"'
        ' componentkey="5cc4e05d-0000-4000-8000-000000000001"'
        f' aria-label="{labels.save_lead}" onclick="record(\'save\')">'
        f"<span><span>{labels.save_lead}</span></span></button></div></div>"
    )


def _accept_button(labels: Labels) -> str:
    return (
        '<div componentkey="625a266b-0000-4000-8000-000000000001">'
        '<div data-display-contents="true"><button type="button"'
        ' componentkey="6f77e438-0000-4000-8000-000000000001"'
        f' aria-label="{labels.accept}" onclick="record(\'accept\')">'
        f"<span><span>{labels.accept}</span></span></button></div></div>"
    )


def _newsletter_anchor(labels: Labels) -> str:
    return (
        '<div data-display-contents="true"><a aria-disabled="false"'
        ' href="https://www.linkedin.com/newsletters/example-1/" target="_blank"'
        ' componentkey="3a0d0804-0000-4000-8000-000000000001"'
        f' onclick="{_onclick("newsletter")}"><span><span>Newsletter</span>'
        "</span></a></div>"
    )


def _card_connect_anchor(labels: Labels) -> str:
    """Connect-primary: a labeled anchor to the profile carrying the key."""
    key = f"ConnectButtonstate:invitation:urn:li:member:{MEMBER}_connect"
    return (
        '<div><div data-display-contents="true">'
        f'<a href="https://www.linkedin.com/in/{USER}/" componentkey="{key}"'
        f' aria-label="{labels.connect}" onclick="{_onclick("card-connect")}">'
        f"<span>{_icon('person-plus-outline-small')}"
        f"<span>{labels.connect}</span></span></a></div></div>"
    )


# --- More-menu items --------------------------------------------------------


def _menu_head(labels: Labels) -> str:
    return f"""
<div data-display-contents="true">
  <a role="menuitem" tabindex="0" target="_blank"
    href="https://www.linkedin.com/sales/people/{PROFILE_URN},name"
    componentkey="7923be65-0000-4000-8000-000000000001" onclick="{_onclick("sales")}">
    <div>{_icon("logo-sales-navigator-outline-medium")}<p>Sales Navigator</p></div></a>
</div>
<div data-display-contents="true">
  <a role="menuitem" tabindex="0"
    href="/messaging/compose/?screenContext=NON_SELF_PROFILE_VIEW&amp;body=https%3A%2F%2Fwww.linkedin.com%2Fin%2F{USER}"
    componentkey="b695bfcc-0000-4000-8000-000000000001" onclick="{_onclick("share")}">
    <div>{_icon("paper-plane-diagonal-outline-medium")}<p>{labels.share}</p></div></a>
</div>
<div role="menuitem" tabindex="0">
  <div>{_icon("arrow-tray-down-outline-medium")}<p>PDF</p></div>
</div>
"""


def _menu_tail(labels: Labels) -> str:
    items = (
        ("bbcb312b", "flag-outline-medium", labels.report),
        ("44f0f2d4", "block-outline-medium", labels.report),
        ("e2950c0e", "square-info-outline-medium", labels.report),
    )
    return "".join(
        f'<a role="menuitem" tabindex="0" href="https://www.linkedin.com/in/{USER}/"'
        f' componentkey="{uuid}-0000-4000-8000-000000000001"'
        f' onclick="{_onclick("report")}"><div>{_icon(icon)}<p>{text}</p></div></a>'
        for uuid, icon, text in items
    )


def _connect_state_item(labels: Labels, state: str) -> str:
    key = f"ConnectButtonstate:invitation:urn:li:member:{MEMBER}_{state}"
    label, icon = (
        (labels.connect, "person-plus-outline-medium")
        if state == "connect"
        else (labels.pending, "clock-outline-medium")
    )
    return f"""
<div data-display-contents="true">
  <div componentkey="6615acba-0000-4000-8000-000000000001">
    <div data-display-contents="true">
      <a role="menuitem" tabindex="0" href="https://www.linkedin.com/in/{USER}/"
        componentkey="{key}" onclick="{_onclick("menu-" + state)}">
        <div componentkey="{key}" aria-label="{label}">
          {_icon(icon)}<div><p>{label}</p></div></div></a>
    </div>
  </div>
</div>
"""


def _follow_item(labels: Labels, *, following: bool) -> str:
    label = labels.following if following else labels.follow
    icon = "check-outline-small" if following else "plus-outline-small"
    return f"""
<div data-display-contents="true">
  <div componentkey="9d7636cf-0000-4000-8000-000000000001">
    <div data-display-contents="true">
      <div role="menuitem" tabindex="0"
        componentkey="auto-component-13d1b75c-0000-4000-8000-000000000001"
        onclick="record('menu-follow')">
        <div componentkey="auto-component-13d1b75c-0000-4000-8000-000000000001"
          aria-label="{label}">{_icon(icon)}<div><p>{label}</p></div></div>
      </div>
    </div>
  </div>
</div>
"""


def _recommendation_items(labels: Labels) -> str:
    return "".join(
        f'<a role="menuitem" tabindex="0"'
        f' href="https://www.linkedin.com/in/{USER}/edit/forms/recommendation/{kind}/"'
        f' componentkey="695cd4fa-0000-4000-8000-00000000000{index}"'
        f' onclick="{_onclick("recommend")}"><div>{_icon(icon)}'
        f"<p>{labels.recommend}</p></div></a>"
        for index, (kind, icon) in enumerate(
            (("request", "quote-outline-medium"), ("write", "comment-outline-medium"))
        )
    )


def _remove_connection_item(labels: Labels) -> str:
    return f"""
<div data-display-contents="true">
  <div componentkey="512be1f5-0000-4000-8000-000000000001">
    <div role="menuitem" tabindex="0" onclick="record('remove')">
      <div aria-label="{labels.remove}">
        {_icon("person-xmark-outline-medium")}<div><p>{labels.remove}</p></div></div>
    </div>
  </div>
</div>
"""


def _ignore_item(labels: Labels) -> str:
    return f"""
<div data-display-contents="true">
  <div componentkey="b7c94fd8-0000-4000-8000-000000000001">
    <div data-display-contents="true">
      <div role="menuitem" tabindex="0"
        componentkey="bf737319-0000-4000-8000-000000000001" onclick="record('ignore')">
        <div componentkey="bf737319-0000-4000-8000-000000000001"
          aria-label="{labels.ignore}">
          {_icon("xmark-outline-medium")}<div><p>{labels.ignore}</p></div></div>
      </div>
    </div>
  </div>
</div>
"""


def _menu_message_item(labels: Labels) -> str:
    return (
        '<a role="menuitem" tabindex="0"'
        f' href="/messaging/compose/?profileUrn=urn%3Ali%3Afsd_profile%3A{PROFILE_URN}"'
        ' componentkey="9294e28d-0000-4000-8000-000000000001"'
        f' onclick="{_onclick("menu-message")}">'
        f"<div>{_icon('paper-plane-diagonal-outline-medium')}"
        f"<div><p>{labels.message}</p></div></div></a>"
    )


def _video_player(labels: Labels) -> str:
    """A cover video above the action row: labeled buttons and an expander."""
    return f"""
<div class="cover-video">
  <button type="button" aria-label="{labels.play}" onclick="record('play')">&#9654;</button>
  <button type="button" aria-label="{labels.settings}" aria-expanded="false"
    onclick="record('player-settings')">&#9881;</button>
</div>
"""


def _edit_intro(labels: Labels) -> str:
    return (
        f'<a href="https://www.linkedin.com/in/{USER}/edit/intro/"'
        f' aria-label="{labels.edit}">{labels.edit}</a>'
    )


# --- the measured profiles --------------------------------------------------

#: williamhgates, 2026-11-11: Following in the top card, no Message, Connect
#: inside More. The old probe called this pending, from the sidebar.
NEVER_INVITED = Profile(
    row=lambda labels: (
        _following_button(labels) + _save_button(labels) + _newsletter_anchor(labels)
    ),
    menu=lambda labels: (
        _menu_head(labels) + _connect_state_item(labels, "connect") + _menu_tail(labels)
    ),
)

#: Measured pending profile, 2026-11-11: Message in the top card, Pending and
#: Following inside More. The old probe called this follow_only.
PENDING = Profile(
    row=lambda labels: _message_anchor(labels) + _save_button(labels),
    menu=lambda labels: (
        _menu_head(labels)
        + _connect_state_item(labels, "pending")
        + _follow_item(labels, following=True)
        + _menu_tail(labels)
    ),
)

#: Measured 1st-degree profile, 2026-11-11: Message in the top card,
#: recommendation links and Remove connection inside More.
FIRST_DEGREE = Profile(
    row=lambda labels: _message_anchor(labels) + _save_button(labels),
    menu=lambda labels: (
        _menu_head(labels)
        + _recommendation_items(labels)
        + _follow_item(labels, following=True)
        + _remove_connection_item(labels)
        + _menu_tail(labels)
    ),
)

#: Measured incoming-request profile, 2026-11-11: Accept in the top card, no
#: Message there; Ignore, Message and Follow inside More.
INCOMING = Profile(
    row=lambda labels: (
        _accept_button(labels) + _save_button(labels) + _newsletter_anchor(labels)
    ),
    menu=lambda labels: (
        _menu_head(labels)
        + _ignore_item(labels)
        + _menu_message_item(labels)
        + _follow_item(labels, following=False)
        + _menu_tail(labels)
    ),
)

INCOMING_WITH_COVER_VIDEO = Profile(
    row=INCOMING.row, menu=INCOMING.menu, card_extra=_video_player
)

#: Measured Connect-primary profile, 2026-11-11: the `_connect` key on a
#: labeled anchor in the top card, so the menu is never needed.
CONNECT_PRIMARY = Profile(
    row=lambda labels: _card_connect_anchor(labels) + _save_button(labels),
    menu=lambda labels: _menu_head(labels) + _menu_tail(labels),
)

# Synthetic shapes, not measured: claims about the algorithm only.
FOLLOW_ONLY = Profile(
    row=lambda labels: _following_button(labels) + _save_button(labels),
    menu=lambda labels: (
        _menu_head(labels) + _follow_item(labels, following=True) + _menu_tail(labels)
    ),
)
#: A Follow-primary row whose menu carries the Ignore icon and nothing else
#: of an incoming request.
FOLLOW_ONLY_WITH_IGNORE_ICON = Profile(
    row=FOLLOW_ONLY.row,
    menu=lambda labels: (
        _menu_head(labels)
        + _ignore_item(labels)
        + _follow_item(labels, following=True)
        + _menu_tail(labels)
    ),
)
#: The menu renders its head first and the Connect item with the closing
#: profile links 1.2 s later.
MENU_IN_TWO_BATCHES = Profile(
    row=NEVER_INVITED.row,
    menu=_menu_head,
    menu_late=lambda labels: (
        _connect_state_item(labels, "connect") + _menu_tail(labels)
    ),
)
STICKY_INVISIBLE = Profile(
    row=NEVER_INVITED.row, menu=NEVER_INVITED.menu, sticky="invisible"
)
STICKY_SHOWN = Profile(row=NEVER_INVITED.row, menu=NEVER_INVITED.menu, sticky="shown")
CONNECT_PRIMARY_STICKY_FIRST = Profile(
    row=CONNECT_PRIMARY.row, menu=CONNECT_PRIMARY.menu, sticky="shown-first"
)
STICKY_ON_SCROLL = Profile(
    row=NEVER_INVITED.row, menu=NEVER_INVITED.menu, sticky="on-scroll"
)
SELF = Profile(
    row=lambda labels: "",
    menu=lambda labels: _menu_tail(labels),
    card_extra=_edit_intro,
)
NO_TOP_CARD = Profile(
    row=NEVER_INVITED.row, menu=NEVER_INVITED.menu, contact_info=False
)
MENU_NEVER_OPENS = Profile(
    row=NEVER_INVITED.row, menu=NEVER_INVITED.menu, more_opens_menu=False
)


# --- drivers ----------------------------------------------------------------


@pytest.fixture
async def dom_page():
    """Real chromium page with the network cut off, or skip without a browser.

    Only launch/setup is guarded by the skip — the ``yield`` is outside it
    so an assertion failure or JS error in a test body is never swallowed
    into a skip.

    ``channel="chromium"`` names the browser this project installs. Without
    it Playwright picks the *binary* from the ``headless`` flag alone and
    asks for ``chromium-headless-shell``, which nothing here installs since
    the setup moved to ``--no-shell``: the launch would fail and every case
    in this file would skip itself, silently, wherever the real browser is.

    Every request is aborted: the fixtures link to linkedin.com the way the
    live page does, and a click that escaped ``preventDefault`` must fail
    here rather than reach it. A case that serves a page registers its own
    route, which takes precedence over this one.
    """
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            page = await browser.new_page()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            await page.route("**/*", lambda route: route.abort())
            yield page
        finally:
            await browser.close()


def _actions(page) -> ConnectionActions:
    """The owner wired the way the facade does, over a real browser page.

    The main-profile read is never reached: every case here stops at the
    signal read, so the borrow is a callable that refuses to be called.
    """

    async def unreachable(_username: str) -> dict[str, Any]:
        raise AssertionError("the DOM cases never read a profile")

    session = ScrapingSession(cast(Page, page))
    return ConnectionActions(session, PageNavigator(session), unreachable)


async def _clicks(page) -> str | None:
    return await page.evaluate("document.body.getAttribute('data-clicked')")


async def _state(page, html: str) -> ConnectionState:
    """Drive the production probe and classifier over one rendered page."""
    await page.set_content(html)
    signals = await _actions(page)._read_action_signals(USER)
    return detect_connection_state(signals)


async def _state_and_menu(page, html: str) -> tuple[ConnectionState, bool]:
    """The state, and whether the profile menu was left open afterwards."""
    state = await _state(page, html)
    left_open = await page.evaluate(
        "!!document.getElementById('profile-menu') &&"
        " !document.getElementById('profile-menu').hidden"
    )
    return state, left_open


async def _click(page, html: str, program: str) -> tuple[Any, str | None]:
    """Run one click program: what it reported, and what it actually hit."""
    await page.set_content(html)
    answer = await page.evaluate(program)
    return answer, await _clicks(page)


async def _in_every_locale(
    page,
    build: Build,
    expected: Any,
    read: Callable[[Any, str], Awaitable[Any]],
) -> None:
    """Assert one answer for the same structure in all four label sets.

    The assertion carries the whole mapping rather than one locale at a
    time, so a divergence names the locale that diverged instead of failing
    on whichever case ran first.
    """
    answers = {labels.locale: await read(page, build(labels)) for labels in LOCALES}
    assert answers == {labels.locale: expected for labels in LOCALES}


STATE_CASES: tuple[tuple[str, Profile, ConnectionState], ...] = (
    ("never-invited-follow-primary", NEVER_INVITED, "connectable"),
    ("pending-in-menu", PENDING, "pending"),
    ("first-degree", FIRST_DEGREE, "already_connected"),
    ("incoming-request", INCOMING, "incoming_request"),
    ("incoming-request-cover-video", INCOMING_WITH_COVER_VIDEO, "incoming_request"),
    ("follow-only", FOLLOW_ONLY, "follow_only"),
    ("connect-primary", CONNECT_PRIMARY, "connectable"),
    ("self-profile", SELF, "self_profile"),
    ("no-top-card", NO_TOP_CARD, "unavailable"),
    ("menu-never-opens", MENU_NEVER_OPENS, "unavailable"),
    # An Ignore icon alone is not an incoming request, and not a confident
    # follow_only either.
    ("ignore-icon-only", FOLLOW_ONLY_WITH_IGNORE_ICON, "unavailable"),
    ("menu-in-two-batches", MENU_IN_TWO_BATCHES, "connectable"),
    ("sticky-copy-shown", STICKY_SHOWN, "connectable"),
    ("connect-primary-sticky-first", CONNECT_PRIMARY_STICKY_FIRST, "connectable"),
)


class TestConnectionStateIsStructural:
    """Every state the classifier can reach, decided in all four label sets.

    Each case runs the real probe and the real classifier, so it covers the
    whole path from rendered markup to the decision the write gate reads.
    """

    @pytest.mark.parametrize(
        ("profile", "expected"),
        [case[1:] for case in STATE_CASES],
        ids=[case[0] for case in STATE_CASES],
    )
    async def test_state_is_the_same_in_every_locale(self, dom_page, profile, expected):
        await _in_every_locale(dom_page, profile.html, expected, _state)

    async def test_the_sidebar_never_makes_a_profile_pending(self, dom_page):
        # The reported bug: a never-invited Follow-primary profile read as
        # pending because the sidebar carried a labeled Message anchor. The
        # sidebar here holds a *pending* key for another member as well.
        state = await _state(dom_page, NEVER_INVITED.html(ENGLISH))
        assert state == "connectable"
        assert await _clicks(dom_page) == "more"

    async def test_the_menu_is_closed_after_the_read(self, dom_page):
        await _in_every_locale(
            dom_page, PENDING.html, ("pending", False), _state_and_menu
        )

    async def test_a_card_that_answers_costs_no_click(self, dom_page):
        await dom_page.set_content(CONNECT_PRIMARY.html(ENGLISH))
        await _actions(dom_page)._read_action_signals(USER)
        assert await _clicks(dom_page) is None

    async def test_the_hidden_sticky_copy_is_never_clicked(self, dom_page):
        for profile in (
            NEVER_INVITED,
            PENDING,
            FIRST_DEGREE,
            INCOMING,
            STICKY_INVISIBLE,
        ):
            await _state(dom_page, profile.html(ENGLISH))
            assert await _clicks(dom_page) == "more"

    async def test_a_menu_open_before_the_click_is_not_the_profile_menu(self, dom_page):
        # An unrelated menu already on screen, ahead of the profile's in DOM
        # order and holding a pending key. Only the menu the More click
        # brought up belongs to this profile.
        stray = (
            '<div role="menu"><a role="menuitem" href="/in/other-member-9/"'
            ' componentkey="ConnectButtonstate:invitation:urn:li:member:900000009'
            '_pending">x</a></div>'
        )
        html = NEVER_INVITED.html(ENGLISH).replace("<main>", stray + "<main>")
        assert await _state(dom_page, html) == "connectable"

    async def test_a_shown_sticky_copy_is_not_the_one_clicked(self, dom_page):
        # Both copies rendered, the sticky one last: the last expander in
        # the card is the sticky More.
        assert await _state(dom_page, STICKY_SHOWN.html(ENGLISH)) == "connectable"
        assert await _clicks(dom_page) == "more"

    async def test_the_read_starts_from_the_top_of_the_page(self, dom_page):
        # The profile read leaves the page at the bottom, where the in-flow
        # row is hidden and only the sticky copy, whose More opens nothing,
        # is shown.
        await dom_page.set_content(STICKY_ON_SCROLL.html(ENGLISH))
        await dom_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await dom_page.wait_for_function(
            "getComputedStyle(document.querySelector('[data-sticky]')).display"
            " === 'block'"
        )
        signals = await _actions(dom_page)._read_action_signals(USER)
        assert detect_connection_state(signals) == "connectable"
        assert await _clicks(dom_page) == "more"
        assert await dom_page.evaluate("window.scrollY") == 0

    async def test_a_menu_read_waits_for_its_closing_profile_links(self, dom_page):
        # The head of the menu renders, holds still, and the Connect item
        # comes 1.2 s later. Read early, this profile is follow_only.
        await dom_page.set_content(MENU_IN_TWO_BATCHES.html(ENGLISH))
        signals = await _actions(dom_page)._read_action_signals(USER)
        assert signals.menu_read is True
        assert signals.has_connect_item is True


class TestActionChoiceIsStructural:
    """Which control each write-side program picks, in all four label sets."""

    async def test_connect_clicks_the_menu_connect_item(self, dom_page):
        await _in_every_locale(
            dom_page,
            NEVER_INVITED.html,
            ({"clicked": True, "menuOpened": True}, "more menu-connect"),
            lambda page, html: _click(page, html, CLICK_CONNECT_JS),
        )

    async def test_connect_clicks_the_card_control_without_opening_more(self, dom_page):
        await _in_every_locale(
            dom_page,
            CONNECT_PRIMARY.html,
            ({"clicked": True, "menuOpened": False}, "card-connect"),
            lambda page, html: _click(page, html, CLICK_CONNECT_JS),
        )

    @pytest.mark.parametrize(
        "profile",
        [PENDING, FIRST_DEGREE, INCOMING, FOLLOW_ONLY],
        ids=["pending", "first-degree", "incoming", "follow-only"],
    )
    async def test_connect_clicks_nothing_without_a_connect_item(
        self, dom_page, profile
    ):
        # Not the Pending item (that would withdraw the invitation), not a
        # sidebar card's Connect for somebody else: only More was clicked.
        await _in_every_locale(
            dom_page,
            profile.html,
            ({"clicked": False, "menuOpened": True}, "more"),
            lambda page, html: _click(page, html, CLICK_CONNECT_JS),
        )

    async def test_connect_clicks_the_in_flow_copy_of_a_card_control(self, dom_page):
        # Both copies rendered, the sticky one first in DOM order.
        await _in_every_locale(
            dom_page,
            CONNECT_PRIMARY_STICKY_FIRST.html,
            ({"clicked": True, "menuOpened": False}, "card-connect"),
            lambda page, html: _click(page, html, CLICK_CONNECT_JS),
        )

    async def test_connect_after_a_read_reopens_the_menu(self, dom_page):
        # The signal read closes the menu; the click has to bring it back.
        await dom_page.set_content(NEVER_INVITED.html(ENGLISH))
        await _actions(dom_page)._read_action_signals(USER)
        assert await _actions(dom_page)._click_connect() is True
        assert await _clicks(dom_page) == "more more menu-connect"

    async def test_accept_clicks_accept_only(self, dom_page):
        await _in_every_locale(
            dom_page,
            INCOMING.html,
            (True, "accept"),
            lambda page, html: _click(page, html, CLICK_INCOMING_ACCEPT_JS),
        )

    async def test_accept_skips_a_cover_video_above_the_row(self, dom_page):
        # The player's expander comes first in the card, its labeled Play
        # button first among the card's labeled buttons.
        await _in_every_locale(
            dom_page,
            INCOMING_WITH_COVER_VIDEO.html,
            (True, "accept"),
            lambda page, html: _click(page, html, CLICK_INCOMING_ACCEPT_JS),
        )

    @pytest.mark.parametrize(
        "profile",
        [FOLLOW_ONLY_WITH_IGNORE_ICON, NEVER_INVITED, PENDING, FIRST_DEGREE],
        ids=["follow-row-with-ignore-icon", "never-invited", "pending", "first-degree"],
    )
    async def test_accept_clicks_nothing_on_a_row_that_is_not_incoming(
        self, dom_page, profile
    ):
        # A Follow-primary row leads with Following: clicking it unfollows.
        # Rows with Message (pending, 1st degree) lead with an anchor and a
        # Save button that has Accept's exact shape.
        await _in_every_locale(
            dom_page,
            profile.html,
            (False, None),
            lambda page, html: _click(page, html, CLICK_INCOMING_ACCEPT_JS),
        )

    async def test_accept_clicks_nothing_without_a_top_card(self, dom_page):
        await _in_every_locale(
            dom_page,
            Profile(row=INCOMING.row, menu=INCOMING.menu, contact_info=False).html,
            (False, None),
            lambda page, html: _click(page, html, CLICK_INCOMING_ACCEPT_JS),
        )


# The three kinds of dialog a profile page carries at once, in a synthetic
# container: a messaging-overlay chat window pinned bottom-right, the modal the
# Connect click opened (centred on the horizontal axis only, as measured), and a
# closed one that still holds its role. Nothing here imitates LinkedIn's
# markup; the claim is about the geometry the index reads.
_CHAT_WINDOW = (
    '<div role="dialog" style="position:fixed; right:0; bottom:0;'
    ' width:300px; height:400px">chat</div>'
)
_HIDDEN_DIALOG = '<div role="dialog" style="display:none">closed</div>'
_MODAL = (
    '<div role="dialog" style="position:fixed; top:32px; left:50%;'
    ' transform:translateX(-50%); width:552px; height:300px">modal'
    "<button>x</button></div>"
)


class TestModalDialogIndex:
    async def test_picks_the_centred_dialog_by_geometry_not_order(self, dom_page):
        # The modal is last in document order and the chat window first, so
        # a read that took the first dialog, the first visible one, or any
        # count of them answers 0 or 1 here, never 2.
        await dom_page.set_content(
            f"<html><body>{_CHAT_WINDOW}{_HIDDEN_DIALOG}{_MODAL}</body></html>"
        )
        assert await dom_page.evaluate(_MODAL_DIALOG_INDEX_JS) == 2

        # A chat window alone is not a modal: nothing is centred, so the
        # answer is the sentinel the caller turns into "no dialog".
        await dom_page.set_content(f"<html><body>{_CHAT_WINDOW}</body></html>")
        assert await dom_page.evaluate(_MODAL_DIALOG_INDEX_JS) == -1


# The invite modal as measured on 2026-11-11, in a synthetic container: a
# centred <dialog> that mounts around nothing but a live region (LinkedIn's
# notifications counter, which read "0 notifications") and is filled in later.
# The words are a parameter so the same structure runs in two languages; the
# decisions read counts and input types only.
_INVITE_WORDS = {
    "en": (
        "To verify this member knows you, please enter their email to connect.",
        "Learn why",
        "Add a note",
        "Send without a note",
    ),
    "de": (
        "Bitte geben Sie die E-Mail-Adresse dieses Mitglieds ein.",
        "Mehr erfahren",
        "Nachricht hinzufügen",
        "Ohne Nachricht senden",
    ),
}


def _invite_body(locale: str, *, email: bool) -> str:
    message, learn, add_note, send = _INVITE_WORDS[locale]
    field = '<input type="email" name="email">' if email else ""
    return (
        f'<p>{message} <a href="#">{learn}</a></p>{field}'
        f"<button onclick=\"record('add-note')\">{add_note}</button>"
        f"<button onclick=\"record('send')\">{send}</button>"
    )


def _invite_page(body: str, *, delay_ms: int = 0, extra: str = "") -> str:
    # Escape closes the dialog, as LinkedIn's does, so a dismissal is quick.
    return (
        "<html><head><script>"
        "function record(what) { document.body.setAttribute('data-clicked', what); }"
        "document.addEventListener('keydown', (e) => {"
        " if (e.key === 'Escape') document.getElementById('invite').close(); });"
        "</script></head><body>"
        f"{extra}"
        '<dialog id="invite" open style="width:544px">'
        '<div aria-live="polite" id="counter">0 notifications'
        "<button>Dismiss</button></div>"
        '<div id="content"></div></dialog>'
        f"<template id='late'>{body}</template>"
        "<script>"
        "setTimeout(() => document.getElementById('content').append("
        "document.getElementById('late').content.cloneNode(true)),"
        f" {delay_ms});"
        "</script></body></html>"
    )


class TestInviteDialogReadiness:
    async def test_waits_for_controls_that_arrive_late(self, dom_page):
        # Straight after the click the dialog is open, centred and rendered,
        # and holds only the live region, button and all: no modal yet. A wait that took the
        # first open dialog would answer at once with no buttons in it.
        await dom_page.set_content(
            _invite_page(_invite_body("en", email=False), delay_ms=1000)
        )
        assert await dom_page.evaluate(_MODAL_DIALOG_INDEX_JS) == -1

        loop = asyncio.get_running_loop()
        started = loop.time()
        ready = await _actions(dom_page)._ready_dialog(timeout=5000)
        elapsed = loop.time() - started

        assert ready is not None
        assert elapsed >= 0.9
        assert (ready.index, ready.buttons, ready.textareas, ready.email_input) == (
            0,
            2,
            0,
            False,
        )
        assert "notifications" not in ready.text
        assert await _clicks(dom_page) is None

    async def test_a_live_region_neither_counts_nor_blocks(self, dom_page):
        # A centred toast with a close button sits ahead of the modal in
        # document order, and the counter inside the modal keeps ticking.
        # Neither is the modal, and the ticking must not keep the modal
        # from settling.
        toast = (
            '<div role="dialog" aria-live="assertive" style="position:fixed;'
            " top:0; left:50%; transform:translateX(-50%); width:400px;"
            ' height:60px">Saved<button>x</button></div>'
        )
        ticker = (
            "<script>let n = 0; setInterval(() => {"
            " document.getElementById('counter').textContent ="
            " (n++) + ' notifications'; }, 100);</script>"
        )
        await dom_page.set_content(
            _invite_page(_invite_body("en", email=False), extra=toast).replace(
                "</body>", ticker + "</body>"
            )
        )

        ready = await _actions(dom_page)._ready_dialog(timeout=5000)

        assert ready is not None
        assert ready.index == 1
        assert ready.buttons == 2

    @pytest.mark.parametrize("locale", sorted(_INVITE_WORDS))
    async def test_the_email_gate_is_found_by_input_type(self, dom_page, locale):
        async def read(email: bool) -> _InviteDialog | None:
            await dom_page.set_content(_invite_page(_invite_body(locale, email=email)))
            return await _actions(dom_page)._ready_dialog(timeout=5000)

        gated = await read(True)
        plain = await read(False)

        assert gated is not None and gated.email_input is True
        assert plain is not None and plain.email_input is False
        assert (gated.buttons, gated.textareas) == (2, 0)

    async def test_the_email_gated_dialog_is_dismissed_unsent(self, dom_page):
        await dom_page.set_content(
            _invite_page(_invite_body("de", email=True), delay_ms=500)
        )

        result = await _actions(dom_page)._submit_invite_dialog("Hallo")

        submitted, note_sent, limit, text = result
        assert (submitted, note_sent, limit) == (False, False, None)
        assert text is not None and "Ohne Nachricht senden" in text
        assert await _clicks(dom_page) is None
        assert await dom_page.evaluate(_MODAL_DIALOG_INDEX_JS) == -1


_SENT_URL = "https://www.linkedin.com/mynetwork/invitation-manager/sent/"


async def _sent_list_has(
    page: Page, hrefs: list[str], username: str, *, nav: str = ""
) -> bool:
    """Serve a sent list of ``hrefs`` from the LinkedIn origin and ask.

    Served rather than ``set_content``: the program resolves each href
    against ``location.origin``, which on ``about:blank`` is the string
    ``"null"`` and makes every ``new URL`` throw. A relative href is only
    a claim on a page that has an origin to be relative to.
    """
    anchors = "".join(f'<a href="{href}">row</a>' for href in hrefs)
    html = (
        '<html><head><meta charset="utf-8"></head>'
        f"<body><header>{nav}</header><main>{anchors}</main></body></html>"
    )
    await page.route(
        "https://www.linkedin.com/**",
        lambda route: route.fulfill(content_type="text/html", body=html),
    )
    try:
        await page.goto(_SENT_URL)
    finally:
        await page.unroute("https://www.linkedin.com/**")
    return bool(await page.evaluate(_SENT_LIST_HAS_USER_JS, username))


class TestSentListHasUser:
    """A vanity-URL match on the sent list, and only a whole one.

    The rows are synthetic anchors, so this is a claim about the matcher,
    not about the page: what an href may look like and still name the
    user, and what looks alike and does not.
    """

    @pytest.mark.parametrize(
        ("href", "username"),
        [
            ("https://www.linkedin.com/in/itadic/", "itadic"),
            ("/in/itadic", "itadic"),
            ("/in/ItAdIc/", "itadic"),
            # Non-ASCII vanity names: the browser hands back the pathname
            # percent-encoded whether the markup carried it that way or not.
            ("/in/m%C3%BCller/", "müller"),
            ("/in/müller/", "müller"),
        ],
        ids=["absolute", "relative-no-slash", "case", "encoded", "literal"],
    )
    async def test_matches_the_users_vanity_url(self, dom_page, href, username):
        assert await _sent_list_has(dom_page, [href], username) is True

    async def test_does_not_match_a_longer_vanity_name(self, dom_page):
        # A prefix match would read itadic2's row as itadic's.
        assert await _sent_list_has(dom_page, ["/in/itadic2/"], "itadic") is False

    async def test_does_not_match_when_the_list_is_empty(self, dom_page):
        assert await _sent_list_has(dom_page, [], "itadic") is False

    async def test_does_not_match_the_global_nav(self, dom_page):
        # The nav links the account's own profile on every page.
        nav = '<a href="/in/itadic/">Me</a>'
        assert await _sent_list_has(dom_page, [], "itadic", nav=nav) is False
