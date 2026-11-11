"""Invitation actions taken on a loaded person page.

``connection.py`` answers what a profile's action area *says* about the
relationship and stays browser-free; this module is everything that reads
or touches that area: the structural signal probe, the More menu, the
Connect and incoming-request Accept clicks, the invite dialog, the
non-submitting note-quota probe and the verification after a write.

Per the AGENTS.md Scraping Rules every decision here rests on a URL pattern
(``/in/USER/edit/intro/``, ``/edit/forms/recommendation/``,
``/messaging/compose/``, ``/overlay/contact-info/``), on a design-system
token LinkedIn does not translate (the ``ConnectButtonstate:`` component key
and its ``_connect`` / ``_pending`` suffix, one icon id), on the *presence*
of an attribute (``aria-expanded`` on the menu opener, ``aria-label`` on a
button) or on whether an element is rendered. No label value is read
anywhere, so a German or an opaquely labelled page classifies exactly as an
English one; ``tests/test_action_signals_dom.py`` holds that line against a
real DOM in all four label sets.

The write gate is the reason the order of the checks below matters: the
Connect control is clicked only when ``has_connect_item`` is true, and the
only other thing that may open an invite dialog is the note-quota probe,
which never clicks a primary button.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus

import asyncio
import logging

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

import linkedin_mcp_server.scraping.connection as connection
from linkedin_mcp_server.exceptions import ActionLimitError
from linkedin_mcp_server.scraping.connection import ActionSignals
from linkedin_mcp_server.scraping.identifiers import (
    normalize_person_identifier,
    person_profile_url,
)
from linkedin_mcp_server.scraping.invitation_withdrawal import InvitationWithdrawer
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

logger = logging.getLogger(__name__)

_DIALOG_SELECTOR = 'dialog[open], [role="dialog"]'
_DIALOG_PREMIUM_LINK_SELECTOR = (
    'dialog[open] a[href*="/premium/"], [role="dialog"] a[href*="/premium/"]'
)
_DIALOG_BUTTONS = "button, [role='button']"
_DIALOG_TEXTAREA = "textarea"

# Which of the page's dialogs is the modal the deeplink just opened. Open
# messaging-overlay conversation windows are role="dialog" too, survive
# navigation, and hug the bottom-right corner; a modal is centred on the
# horizontal axis (measured 2026-09-14: box left 364, width 552, viewport
# 1280). Positional "last button" across all dialogs resolved to a chat
# window's "Open send options" that day, one index away from its "Send".
# Vertical position is deliberately ignored: the same modal sat at top 32,
# nowhere near the vertical centre.
#
# A centred box is not yet the modal. Measured 2026-11-11: straight after
# the Connect click the invite <dialog> was open and centred while its whole
# text read "0 notifications"; about 2.5 s later it held the message, "Add a
# note" and "Send without a note", still followed by that same "0
# notifications" (a notifications counter rendered inside it). So a dialog
# counts only once it holds a rendered control outside any live region, the
# dialog itself included (``closest`` starts at the element), and live-region
# text is left out of what the readiness wait compares, so a counter ticking
# inside the dialog can neither fake nor hold back its readiness.
_INVITE_MODAL_FNS_JS = r"""
const LIVE = '[aria-live], [role="status"], [role="alert"], [role="log"]';
const CONTROLS = 'button, [role="button"], input, textarea';
const EMAIL_INPUT = 'input[type="email"], input[autocomplete="email"]';
const isShown = (el) => {
  const r = el.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return false;
  return el.checkVisibility
    ? el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})
    : getComputedStyle(el).visibility !== 'hidden';
};
const ownControls = (d, selector) => [...d.querySelectorAll(selector)].filter(
  (el) => isShown(el) && !el.closest(LIVE)
);
function ownText(d) {
  const copy = d.cloneNode(true);
  copy.querySelectorAll(LIVE).forEach((el) => el.remove());
  return (copy.textContent || '').replace(/\s+/g, ' ').trim();
}
function inviteModal() {
  const dialogs = [...document.querySelectorAll('dialog[open], [role="dialog"]')];
  let best = -1;
  let bestOff = Infinity;
  dialogs.forEach((d, i) => {
    if (!isShown(d)) return;
    if (ownControls(d, CONTROLS).length === 0) return;
    const r = d.getBoundingClientRect();
    const off = Math.abs(r.left + r.width / 2 - innerWidth / 2);
    if (off <= innerWidth * 0.1 && off < bestOff) {
      best = i;
      bestOff = off;
    }
  });
  return {index: best, dialog: best < 0 ? null : dialogs[best]};
}
"""

_MODAL_DIALOG_INDEX_JS = (
    "() => {"
    + _INVITE_MODAL_FNS_JS
    + """
  return inviteModal().index;
}
"""
)

# What the readiness wait compares between polls: the modal's position among
# the dialogs, its control counts and its own text. Counts and presence only,
# never a label: the email field is found by its input type.
_INVITE_DIALOG_SNAPSHOT_JS = (
    "() => {"
    + _INVITE_MODAL_FNS_JS
    + """
  const {index, dialog} = inviteModal();
  if (!dialog) return null;
  return {
    index,
    buttons: ownControls(dialog, 'button, [role="button"]').length,
    textareas: ownControls(dialog, 'textarea').length,
    emailInput: ownControls(dialog, EMAIL_INPUT).length > 0,
    text: ownText(dialog),
  };
}
"""
)

# Ground truth for "did the invite go out". The profile page is not: the
# More menu's Pending key is the state the page that sent the click holds,
# and measured 2026-09-14, an invite that LinkedIn quietly dropped read on
# the profile exactly like one it delivered. The sent list is sorted newest
# first and links every row by vanity URL, so a URL match on its first page
# is locale-independent and needs no text. It is also the fallback for a
# profile whose More menu could not be read, and only that: it holds the ten
# newest invitations, so a miss there proves nothing.
_SENT_INVITATIONS_URL = "https://www.linkedin.com/mynetwork/invitation-manager/sent/"

# Anchors on the sent-invitations page whose path is exactly /in/<username>/.
# ``new URL`` normalises relative and absolute hrefs; the trailing-slash
# compare stops ``/in/itadic`` from matching ``/in/itadic2``. Scoped to
# <main>: the global nav links the account's own profile, which would
# answer yes for a check against the account itself.
_SENT_LIST_HAS_USER_JS = r"""
(username) => {
  // pathname comes back percent-encoded; vanity names may be non-ASCII.
  const want = ("/in/" + encodeURIComponent(username) + "/").toLowerCase();
  return [...document.querySelectorAll('main a[href*="/in/"]')].some((a) => {
    try {
      const path = new URL(a.href, location.origin).pathname.toLowerCase();
      return path === want || path === want.slice(0, -1);
    } catch (e) {
      return false;
    }
  });
}
"""

# Text LinkedIn surfaces outside a dialog when it refuses to open one:
# toasts and inline banners carry a live-region role. Raw text, returned
# verbatim for the caller to read; never classified here.
# Every dialog control with whether it is actually rendered. Logged next to
# the dialog text so a failed click can be read back from the log.
_DIALOG_BUTTONS_JS = r"""
() => [...document.querySelectorAll(
    'dialog[open] button, [role="dialog"] button, [role="dialog"] [role="button"]'
  )].map((b) => {
    const r = b.closest('dialog, [role="dialog"]').getBoundingClientRect();
    return {
      label: b.getAttribute("aria-label"),
      text: (b.innerText || "").trim().slice(0, 40),
      visible: b.getBoundingClientRect().width > 0,
      dialogBox: [Math.round(r.left), Math.round(r.top), Math.round(r.width)],
    };
  })
"""

_PAGE_ALERTS_JS = r"""
() => [...document.querySelectorAll('[role="alert"], [role="status"], [role="alertdialog"]')]
  .map((e) => (e.innerText || "").trim())
  .filter(Boolean)
  .join(" | ")
"""

# Shared JS helpers, inlined into every program below so one change to the
# heuristics reaches all of them. Measured live 2026-11-11 on five profiles
# (never invited and Follow-primary, Connect-primary, pending, 1st degree,
# incoming request):
#
# - The top card is the innermost <section> around the profile's
#   /overlay/contact-info/ anchor; there is exactly one, and none in the
#   `main aside` sidebar. `main` holds no h1, and its first <section> is the
#   whole profile column (activity posts, a video player's menus), so
#   neither of the older anchors scopes anything.
# - The action row is rendered twice: the in-flow one and a copy for the
#   sticky header, hidden at the top of the page and shown once it scrolls.
#   The profile read leaves the page scrolled to the bottom, so every
#   program scrolls back to the top first; and where both copies are still
#   rendered, the one outside a fixed or sticky container wins.
# - More is a `button[aria-expanded]`. A cover video's player sits above the
#   action row in the same section and carries expanders too, so the last
#   rendered one is More. Its menu is portal-mounted outside <main> with no
#   aria-controls link, and the page already holds hidden [role=menu]
#   elements (video subtitle settings): the menu is the one that became
#   rendered after the click. It is read only once its last item is one of
#   the profile links that close every measured menu (Report / Block, About
#   this member: `a[role=menuitem]` to `/in/<vanity>/`); a menu read before
#   that could be missing the Connect or Pending item.
# - The connect state is `[componentkey^="ConnectButtonstate:"]`, suffixed
#   `_connect` or `_pending`, on the Connect / Pending anchor in the top card
#   (Connect-primary) or in the More menu (everything else). The sidebar
#   cards carry the same keys for other members, which is one more reason
#   the scope is the top card or its own menu, never the document.
_PROFILE_ACTION_FNS_JS = r"""
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
// Rendered and visible: a box, and not hidden by itself or an ancestor
// (display, visibility, content-visibility, opacity).
const isRendered = (el) => {
  const r = el.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return false;
  return el.checkVisibility
    ? el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})
    : getComputedStyle(el).visibility !== 'hidden';
};
function inStickyCopy(el, card) {
  for (let node = el.parentElement; node && node !== card; node = node.parentElement) {
    const position = getComputedStyle(node).position;
    if (position === 'fixed' || position === 'sticky') return true;
  }
  return false;
}
// The rendered elements, preferring those outside a sticky copy whenever
// any are; DOM order is kept.
function inFlow(elements, card) {
  const rendered = elements.filter(isRendered);
  const flowing = rendered.filter((el) => !inStickyCopy(el, card));
  return flowing.length ? flowing : rendered;
}

async function scrollToTop() {
  if (window.scrollY === 0 && document.documentElement.scrollTop === 0) return;
  window.scrollTo(0, 0);
  await sleep(400);
}

function findTopCard(main) {
  const sections = new Set();
  for (const a of main.querySelectorAll('a[href*="/overlay/contact-info/"]')) {
    if (a.closest('main aside')) continue;
    const section = a.closest('section');
    if (section) sections.add(section);
  }
  return sections.size === 1 ? [...sections][0] : null;
}

function findMoreButton(card) {
  const openers = inFlow([...card.querySelectorAll('button[aria-expanded]')], card);
  return openers.length ? openers[openers.length - 1] : null;
}

// The rendered controls carrying a connect-state key with this suffix. The
// key sits on the clickable element and on its label wrapper, so both
// resolve to the one control.
function connectStateControls(scope, suffix, card) {
  const selector =
    `[componentkey^="ConnectButtonstate:"][componentkey$="_${suffix}"]`;
  const controls = new Set();
  for (const el of inFlow([...scope.querySelectorAll(selector)], card || scope)) {
    const control = el.closest('a, button, [role="menuitem"], [role="button"]');
    controls.add(control && scope.contains(control) ? control : el);
  }
  return [...controls];
}

const PROFILE_PATH = /^\/in\/[^/]+\/?$/;
function menuHasSettled(menu) {
  const items = [...menu.querySelectorAll('[role="menuitem"]')];
  const last = items[items.length - 1];
  if (!last || last.tagName !== 'A') return false;
  if ((last.getAttribute('componentkey') || '').startsWith('ConnectButtonstate:')) {
    return false;
  }
  return PROFILE_PATH.test(last.pathname);
}

// Click More and wait for its menu to render, close with a profile link and
// stop growing. Returns {clicked, menu}; menu is null when no single new
// menu settled in time.
async function openMoreMenu(card) {
  const more = findMoreButton(card);
  if (!more) return {clicked: false, menu: null};
  if (more.getAttribute('aria-expanded') === 'true') {
    more.click();
    await sleep(300);
  }
  const before = new Set(
    [...document.querySelectorAll('[role="menu"]')].filter(isRendered)
  );
  more.click();
  let candidate = null;
  let lastCount = -1;
  let stablePolls = 0;
  for (let waited = 0; waited < 5000; waited += 100) {
    await sleep(100);
    const opened = [...document.querySelectorAll('[role="menu"]')].filter(
      (m) => isRendered(m) && !before.has(m) && !m.closest('main aside')
    );
    if (opened.length !== 1) {
      candidate = null;
      continue;
    }
    const count = opened[0].querySelectorAll('[role="menuitem"]').length;
    if (opened[0] === candidate && count === lastCount && menuHasSettled(candidate)) {
      stablePolls += 1;
      if (stablePolls >= 4) return {clicked: true, menu: candidate};
    } else {
      stablePolls = 0;
    }
    candidate = opened[0];
    lastCount = count;
  }
  return {clicked: true, menu: null};
}
"""

# Locale-independent connection-state probe. Reads the top card first and
# opens More only when the card has not already answered, so a profile with
# Connect or Pending in its top card costs no click. Returns the fields of
# ``ActionSignals`` plus ``moreClicked``, which tells the caller a menu may
# be open and needs closing.
#
# The username is CSS-escaped before interpolation into the attribute
# selector to defend against malformed inputs containing characters that
# would otherwise break the selector syntax (quotes, brackets).
ACTION_SIGNALS_JS = (
    r"""
(async (username) => {
"""
    + _PROFILE_ACTION_FNS_JS
    + r"""
  const main = document.querySelector('main');
  if (!main) return null;
  await scrollToTop();

  const safe = CSS.escape(username);
  const signals = {
    topCard: false,
    editIntro: !!main.querySelector(`a[href*="/in/${safe}/edit/intro/"]`),
    pendingItem: false,
    connectItem: false,
    menuRead: false,
    recommendationLink: false,
    ignoreItem: false,
    messageItemInMenu: false,
    composeInTopCard: false,
    moreClicked: false,
  };
  const card = findTopCard(main);
  if (!card) return signals;
  signals.topCard = true;
  signals.composeInTopCard =
    inFlow([...card.querySelectorAll('a[href*="/messaging/compose/"]')], card)
      .length > 0;
  signals.pendingItem = connectStateControls(card, 'pending').length > 0;
  signals.connectItem = connectStateControls(card, 'connect').length > 0;
  if (signals.editIntro || signals.pendingItem || signals.connectItem) {
    return signals;
  }

  const opened = await openMoreMenu(card);
  signals.moreClicked = opened.clicked;
  const menu = opened.menu;
  if (!menu) return signals;
  signals.menuRead = true;
  signals.pendingItem = connectStateControls(menu, 'pending').length > 0;
  signals.connectItem = connectStateControls(menu, 'connect').length > 0;
  signals.recommendationLink = !!menu.querySelector(
    'a[href*="/edit/forms/recommendation/"]'
  );
  signals.ignoreItem = !!menu.querySelector(
    '[role="menuitem"] svg[id="xmark-outline-medium"]'
  );
  // A direct Message (profileUrn=), as opposed to "Send profile in a
  // message" (body=), which every measured menu carries.
  signals.messageItemInMenu = !!menu.querySelector(
    'a[href*="/messaging/compose/"][href*="profileUrn="]'
  );
  return signals;
})
"""
)

# Click the one ``_connect`` control: in the top card when it is there,
# otherwise in the More menu, opened again here because the signal probe
# closed it. Copies of one control (the sticky header's) share its
# component key and resolve to the in-flow one; two different keys, or
# none, click nothing. Returns {clicked, menuOpened}; after a successful
# click the menu closes on its own and the invite dialog opens, so the
# caller must not press Escape.
CLICK_CONNECT_JS = (
    r"""
(async () => {
"""
    + _PROFILE_ACTION_FNS_JS
    + r"""
  const main = document.querySelector('main');
  if (!main) return {clicked: false, menuOpened: false};
  await scrollToTop();
  const card = findTopCard(main);
  if (!card) return {clicked: false, menuOpened: false};
  let connectControls = connectStateControls(card, 'connect');
  let menuOpened = false;
  if (connectControls.length === 0) {
    const opened = await openMoreMenu(card);
    menuOpened = opened.clicked;
    if (!opened.menu) return {clicked: false, menuOpened};
    connectControls = connectStateControls(opened.menu, 'connect');
  }
  const keys = new Set(connectControls.map((c) => c.getAttribute('componentkey')));
  if (connectControls.length === 0 || keys.size !== 1) {
    return {clicked: false, menuOpened};
  }
  connectControls[0].click();
  return {clicked: true, menuOpened};
})
"""
)

# Click Accept on an incoming-request profile. The action row is the
# smallest ancestor of More holding another rendered button; measured
# 2026-11-11 it reads Accept, Save in Sales Navigator, More, with Ignore
# moved into the More menu, so no button in the row ignores the request.
#
# The row itself has to look like an incoming row, so that the program
# clicks nothing on any other profile even if it is reached by mistake: no
# compose anchor (1st degree, pending and Premium rows carry Message), no
# Follow/Following toggle (`auto-component-` keys, Follow-primary rows) and
# no connect-state control. Accept is then the first labeled button that
# is not More and carries no icon. Save in Sales Navigator has exactly the
# same shape, so "exactly one candidate" is not available as a guard: on a
# Sales Navigator account it would refuse every Accept. Order is the
# discriminator, Accept leading, and the caller verifies afterwards.
CLICK_INCOMING_ACCEPT_JS = (
    r"""
(async () => {
"""
    + _PROFILE_ACTION_FNS_JS
    + r"""
  const main = document.querySelector('main');
  if (!main) return false;
  await scrollToTop();
  const card = findTopCard(main);
  const more = card ? findMoreButton(card) : null;
  if (!more) return false;
  let row = more.parentElement;
  while (
    row &&
    row !== card &&
    ![...row.querySelectorAll('button')].some((b) => b !== more && isRendered(b))
  ) {
    row = row.parentElement;
  }
  if (!row || row === card) return false;
  const disqualifying = [
    ...row.querySelectorAll(
      'a[href*="/messaging/compose/"], [componentkey^="auto-component-"],' +
        ' [componentkey^="ConnectButtonstate:"]'
    ),
  ];
  if (inFlow(disqualifying, card).length) return false;
  const candidates = inFlow(
    [...row.querySelectorAll('button[aria-label]:not([aria-expanded])')],
    card
  ).filter((b) => !b.querySelector('svg'));
  if (!candidates.length) return false;
  const acceptButton = candidates[0];
  acceptButton.click();
  return true;
})
"""
)


def _connection_result(
    url: str,
    status: str,
    message: str,
    *,
    note_sent: bool = False,
    profile: str = "",
) -> dict[str, Any]:
    """Build a structured response for a profile connection attempt."""
    result: dict[str, Any] = {
        "url": url,
        "status": status,
        "message": message,
        "note_sent": note_sent,
    }
    if profile:
        result["profile"] = profile
    return result


@dataclass(frozen=True, slots=True)
class _InviteDialog:
    """One read of the invite modal, compared whole between polls."""

    index: int
    buttons: int
    textareas: int
    email_input: bool
    text: str


#: Consecutive identical reads, 250 ms apart, before the modal is acted on.
_DIALOG_SETTLED_READS = 3


# One main-profile read of one member, by username. The person workflow owns
# that read and this one only ever needs its ``main_profile`` text, so the
# borrow is a callable rather than the scraper: this module never learns what
# the facade is, and the day the read moves again only the wiring does.
ReadMainProfile = Callable[[str], Awaitable[dict[str, Any]]]


class ConnectionActions:
    """Send, accept and probe invitations for one LinkedIn member."""

    def __init__(
        self,
        session: ScrapingSession,
        navigator: PageNavigator,
        read_main_profile: ReadMainProfile,
    ):
        self._session = session
        self._navigator = navigator
        self._read_main_profile = read_main_profile

    async def _modal_index(self) -> int:
        """Index of the invite modal among the page's dialogs, -1 if none."""
        try:
            return int(await self._session.page.evaluate(_MODAL_DIALOG_INDEX_JS))
        except Exception:
            return -1

    async def _dialog_snapshot(self) -> _InviteDialog | None:
        try:
            data = await self._session.page.evaluate(_INVITE_DIALOG_SNAPSHOT_JS)
            return _InviteDialog(
                index=int(data["index"]),
                buttons=int(data["buttons"]),
                textareas=int(data["textareas"]),
                email_input=bool(data["emailInput"]),
                text=str(data["text"]),
            )
        except Exception:
            return None

    async def _ready_dialog(self, *, timeout: int = 10000) -> _InviteDialog | None:
        """Poll until the invite modal is present and settled, ``None`` if not.

        Deliberately not "any dialog is visible": chat windows are dialogs
        that never close, so that question always answers yes. Settled means
        the same read ``_DIALOG_SETTLED_READS`` times running: the modal
        mounts before its content does, and a decision made on the first
        read is made on a dialog that is not there yet.
        """
        deadline = asyncio.get_running_loop().time() + timeout / 1000
        last: _InviteDialog | None = None
        same = 0
        while True:
            snapshot = await self._dialog_snapshot()
            same = same + 1 if snapshot is not None and snapshot == last else 1
            last = snapshot
            if snapshot is not None and same >= _DIALOG_SETTLED_READS:
                return snapshot
            if asyncio.get_running_loop().time() >= deadline:
                if snapshot is not None:
                    logger.info("Invite dialog never settled: %s", snapshot)
                return None
            await asyncio.sleep(0.25)

    async def _dialog_is_closed(self, *, timeout: int = 1000) -> bool:
        """Poll until no invite modal is present, or ``timeout`` ms pass."""
        return await self._poll_modal(present=False, timeout=timeout)

    async def _poll_modal(self, *, present: bool, timeout: int) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout / 1000
        while True:
            if (await self._modal_index() >= 0) == present:
                return True
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.25)

    async def _invite_dialog(self):
        """Locator scoped to the invite modal; ``None`` when there is none.

        No fallback to "the first dialog": with the modal gone that is a
        chat window, and one index below its last button is its Send.
        """
        index = await self._modal_index()
        if index < 0:
            return None
        return self._session.page.locator(_DIALOG_SELECTOR).nth(index)

    async def _click_dialog_primary_button(self, *, timeout: int = 5000) -> bool:
        """Click the last (primary/Send) button in the open dialog.

        LinkedIn consistently places the primary action as the last button.
        Returns False (rather than raising) when the click is intercepted or
        times out, so callers can fall back to a keyboard submit.
        """
        dialog = await self._invite_dialog()
        if dialog is None:
            return False
        buttons = dialog.locator(_DIALOG_BUTTONS)
        count = await buttons.count()
        if count == 0:
            return False
        try:
            await buttons.nth(count - 1).click(timeout=timeout)
            return True
        except Exception:
            logger.debug("Primary dialog button click failed", exc_info=True)
            return False

    async def _fill_dialog_textarea(self, value: str, *, timeout: int = 5000) -> bool:
        """Fill the first textarea inside the open dialog (structural)."""
        dialog = await self._invite_dialog()
        if dialog is None:
            return False
        textareas = dialog.locator(_DIALOG_TEXTAREA)
        try:
            if await textareas.count() == 0:
                return False
            await textareas.first.fill(value, timeout=timeout)
            return True
        except Exception:
            logger.debug("Invite note fill failed", exc_info=True)
            return False

    async def _dismiss_dialog(self) -> None:
        """Dismiss any open dialog via Escape key (structural)."""
        await self._session.page.keyboard.press("Escape")
        await self._dialog_is_closed(timeout=3000)

    async def _get_premium_upsell_message(self, *, timeout: int = 2500) -> str | None:
        """Return the raw LinkedIn Premium upsell dialog text when visible.

        LinkedIn intercepts invite-with-note flows with an upsell modal when
        the free personalized-note quota is exhausted. The detector itself is
        locale-independent: the modal links to ``/premium/...``. The returned
        message is the dialog text as rendered by LinkedIn, not a synthesized
        explanation.
        """
        locator = self._session.page.locator(_DIALOG_PREMIUM_LINK_SELECTOR).first
        try:
            await locator.wait_for(state="visible", timeout=timeout)
        except PlaywrightTimeoutError:
            return None
        except Exception:
            try:
                if not await locator.is_visible():
                    return None
            except Exception:
                return None

        try:
            message = await self._session.page.evaluate(
                """() => {
                    const link = document.querySelector(
                        'dialog[open] a[href*="/premium/"], [role="dialog"] a[href*="/premium/"]'
                    );
                    const dialog = link?.closest('dialog,[role="dialog"]');
                    return dialog?.innerText || dialog?.textContent || link?.innerText || '';
                }"""
            )
            if isinstance(message, str) and message.strip():
                return message.strip()
        except Exception:
            logger.debug("Could not read Premium upsell dialog text", exc_info=True)

        try:
            link_text = await locator.inner_text()
            if link_text.strip():
                return link_text.strip()
        except Exception:
            pass
        return "LinkedIn Premium upsell modal detected."

    async def _dialog_text(self) -> str:
        """Raw innerText of the open dialog, empty on any failure."""
        try:
            dialog = await self._invite_dialog()
            if dialog is None:
                return ""
            text = await dialog.inner_text(timeout=2000)
        except Exception:
            return ""
        return " ".join(text.split())

    async def _page_alerts(self) -> str:
        """Raw text of live-region banners/toasts on the page, empty if none."""
        try:
            return str(await self._session.page.evaluate(_PAGE_ALERTS_JS) or "")
        except Exception:
            return ""

    async def _invitation_in_sent_list(self, username: str) -> bool:
        """Whether ``username`` appears on the first page of sent invitations.

        One navigation. The first page holds the ten newest rows, so a
        just-sent invite is there unless ten more went out in between.
        """
        # Let the invite request finish before leaving the page: the modal
        # closes on click, not on the server's answer, and a navigation
        # aborts whatever is still in flight.
        await asyncio.sleep(1.5)
        await self._navigator._navigate_to_page(_SENT_INVITATIONS_URL)
        # Scoped to <main>: the global nav links /in/ too, and matched at
        # once on a page whose list had not rendered yet (measured: a
        # delivered invite read as absent one second after the click).
        try:
            await self._session.page.locator('main a[href*="/in/"]').first.wait_for(
                timeout=10000
            )
        except Exception:
            logger.debug("Sent-invitations list rendered no profile links")
        for _ in range(4):
            try:
                if await self._session.page.evaluate(_SENT_LIST_HAS_USER_JS, username):
                    return True
            except Exception:
                logger.debug("Sent-invitations list check failed", exc_info=True)
                return False
            await asyncio.sleep(1.5)
        return False

    async def _close_more_menu(self) -> None:
        """Close an open More menu so it cannot intercept the next action."""
        try:
            await self._session.page.keyboard.press("Escape")
        except Exception:
            logger.debug("Escape after the More-menu read failed", exc_info=True)

    async def _click_connect(self) -> bool:
        """Click the profile's Connect control, locale-independently.

        Delegates to ``CLICK_CONNECT_JS``, which clicks the one rendered
        ``_connect`` connect-state control in the top card or its More menu
        and nothing when there is not exactly one. Returns True iff the
        click landed.
        """
        try:
            data = await self._session.page.evaluate(CLICK_CONNECT_JS)
        except Exception:
            logger.debug("Connect click via JS failed", exc_info=True)
            return False
        if not isinstance(data, dict):
            return False
        if data.get("clicked"):
            return True
        if data.get("menuOpened"):
            await self._close_more_menu()
        return False

    async def _click_incoming_accept(self) -> bool:
        """Click Accept on an incoming-request profile, locale-independently.

        Delegates to ``CLICK_INCOMING_ACCEPT_JS``, which clicks the first
        labeled button of the top-card action row. The caller fires it only
        once the profile classified as ``incoming_request`` and verifies the
        transition afterwards. Returns True iff the click landed.
        """
        try:
            return bool(await self._session.page.evaluate(CLICK_INCOMING_ACCEPT_JS))
        except Exception:
            logger.debug("Incoming accept click via JS failed", exc_info=True)
            return False

    async def _read_action_signals(self, username: str) -> ActionSignals:
        """Read locale-independent structural signals for a profile's
        relationship state.

        Runs ``ACTION_SIGNALS_JS``, which reads the top card and, unless the
        card already answered, opens the More menu and reads that too. The
        menu is closed again before returning, so the page is left as it
        was found. A page without ``<main>``, or a probe that fails midway,
        yields no signals at all, which classifies as ``unavailable``; a
        failed probe may have clicked More, so it is followed by Escape too.
        """
        data: Any = None
        failed = False
        try:
            data = await self._session.page.evaluate(ACTION_SIGNALS_JS, username)
        except Exception:
            failed = True
            logger.warning(
                "Connection-state probe failed for %s", username, exc_info=True
            )
        finally:
            if failed or (isinstance(data, dict) and data.get("moreClicked")):
                await self._close_more_menu()
        if not isinstance(data, dict):
            data = {}
        return ActionSignals(
            has_top_card=bool(data.get("topCard")),
            has_edit_intro_anchor=bool(data.get("editIntro")),
            has_pending_item=bool(data.get("pendingItem")),
            has_connect_item=bool(data.get("connectItem")),
            menu_read=bool(data.get("menuRead")),
            has_recommendation_link=bool(data.get("recommendationLink")),
            has_ignore_item=bool(data.get("ignoreItem")),
            has_message_item_in_menu=bool(data.get("messageItemInMenu")),
            has_compose_anchor_in_top_card=bool(data.get("composeInTopCard")),
        )

    async def _submit_invite_dialog(
        self, note: str | None
    ) -> tuple[bool, bool, str | None, str | None]:
        """Submit the invite dialog opened by the Connect click.

        Returns ``(submitted, note_sent, note_limit_message, dialog_text)``.

        ``dialog_text`` is the raw text of whatever dialog the click
        opened, read before anything is clicked, and ``None`` when no
        dialog opened at all. It is the only record of what LinkedIn
        actually showed: a weekly-limit or add-an-email dialog closes on
        its last button just like the invite dialog does, so ``submitted``
        alone cannot tell them apart.

        ``note_sent`` reports *delivery*, not textarea fill — it stays
        False on any failure path, including the Premium upsell that
        LinkedIn shows when the free personalized-note quota is exhausted.
        ``note_limit_message`` is the raw LinkedIn Premium dialog text when
        the upsell was detected; in that case ``submitted`` is False, the
        dialog is dismissed, and callers should surface that text directly.

        All interaction uses structural selectors and positional indexing
        — no localized text matching. Owns dialog cleanup: the dialog is
        dismissed on every failure path, callers must not dismiss again.
        """
        ready = await self._ready_dialog()
        if ready is None:
            if await self._modal_index() < 0:
                return False, False, None, None
            # Present but still changing: nothing in it is safe to click.
            dialog_text = await self._dialog_text()
            await self._dismiss_dialog()
            return False, False, None, dialog_text
        dialog = await self._invite_dialog()
        if dialog is None:
            return False, False, None, None
        dialog_text = await self._dialog_text()
        logger.info("Invite dialog text: %s", dialog_text)
        try:
            logger.debug(
                "Invite dialog buttons: %s",
                await self._session.page.evaluate(_DIALOG_BUTTONS_JS),
            )
        except Exception:
            logger.debug("Invite dialog button dump failed", exc_info=True)

        if ready.email_input:
            # LinkedIn asks for the member's email address before it will
            # send; the server has none to give, so the dialog is reported
            # as one that could not be submitted.
            logger.info("Invite dialog asks for the member's email")
            await self._dismiss_dialog()
            return False, False, None, dialog_text

        note_filled = False
        if note:
            textarea_count = await dialog.locator(_DIALOG_TEXTAREA).count()
            if textarea_count == 0:
                # Reveal the note textarea via the secondary action.
                # Two layouts are now in the wild and both place "Add a
                # note" at index ``btn_count - 2``:
                #   * Legacy invite dialog (3 buttons): dismiss, secondary
                #     "Add a note", primary "Send" -> nth(1) is secondary.
                #   * "Add a note to your invitation?" gating dialog (2
                #     buttons, rolled out 2026-05): "Add a note",
                #     "Send without a note" -> nth(0) is the only path
                #     that mounts the textarea. See issue #455.
                # If LinkedIn ever serves a 2-button dismiss/primary
                # no-note layout, the click below misroutes to dismiss;
                # the textarea-presence recheck via _fill_dialog_textarea
                # then fails and the caller returns connect_unavailable
                # without sending — the same outcome as today.
                buttons = dialog.locator(_DIALOG_BUTTONS)
                btn_count = await buttons.count()
                if btn_count >= 2:
                    await buttons.nth(btn_count - 2).click()
                    textarea_appeared = True
                    try:
                        await dialog.locator(_DIALOG_TEXTAREA).first.wait_for(
                            state="visible", timeout=3000
                        )
                    except PlaywrightTimeoutError:
                        logger.debug("Note textarea did not appear")
                        textarea_appeared = False
                    # LinkedIn now renders a persistent Premium nudge
                    # banner on this step even when quota is NOT exhausted
                    # (observed: "3 personalized invitations remaining this
                    # month" alongside a live, fillable textarea). Bailing on
                    # banner presence alone false-positives on every note
                    # send. Only treat it as a real block when the textarea
                    # never mounted at all — the one case where LinkedIn
                    # actually replaces the note UI with the upsell instead of
                    # showing both.
                    if not textarea_appeared:
                        note_limit_message = await self._get_premium_upsell_message()
                        if note_limit_message is not None:
                            logger.info(
                                "Premium upsell blocked opening invite note editor"
                            )
                            await self._dismiss_dialog()
                            return False, False, note_limit_message, dialog_text

            note_filled = await self._fill_dialog_textarea(note)
            if not note_filled:
                # Same gate as the reveal step: the Premium nudge banner sits
                # beside a live textarea, so a failed fill is a quota block
                # only once no visible textarea is left. A count that fails
                # proves no absence, so it claims no block either: a false
                # block invites the caller to resend without the note.
                try:
                    textarea_visible = (
                        await dialog.locator(
                            f"{_DIALOG_TEXTAREA} >> visible=true"
                        ).count()
                        > 0
                    )
                except Exception:
                    textarea_visible = True
                if textarea_visible:
                    logger.info(
                        "Invite note fill failed without evidence of a quota block"
                    )
                    await self._dismiss_dialog()
                    return False, False, None, dialog_text
                note_limit_message = await self._get_premium_upsell_message()
                if note_limit_message is not None:
                    logger.info("Premium upsell blocked filling invite note")
                    await self._dismiss_dialog()
                    return False, False, note_limit_message, dialog_text
                await self._dismiss_dialog()
                return False, False, None, dialog_text

        sent = await self._click_dialog_primary_button()
        if not sent:
            # Fallback: focus the primary button positionally so a subsequent
            # Enter targets it instead of a focused textarea (where Enter
            # would just insert a newline).
            buttons = dialog.locator(_DIALOG_BUTTONS)
            btn_count = await buttons.count()
            if btn_count > 0:
                try:
                    await buttons.nth(btn_count - 1).focus()
                    await self._session.page.keyboard.press("Enter")
                    sent = await self._dialog_is_closed(timeout=2000)
                except Exception:
                    logger.debug("Keyboard submit fallback failed", exc_info=True)
            if not sent:
                # The Send click can also fail because LinkedIn swapped the
                # invite dialog for the Premium upsell at submit time — the
                # original primary button is then detached or pointer-event
                # covered, so the click raises or times out. Check for the
                # upsell here so we surface the raw note-limit message
                # instead of dismissing silently and returning
                # connect_unavailable.
                if note:
                    note_limit_message = await self._get_premium_upsell_message()
                    if note_limit_message is not None:
                        logger.info(
                            "Premium upsell modal intercepted invite submit click"
                        )
                        await self._dismiss_dialog()
                        return False, False, note_limit_message, dialog_text
                await self._dismiss_dialog()
                return False, False, None, dialog_text

        dialog_closed = await self._dialog_is_closed(timeout=5000)
        if not dialog_closed:
            logger.debug("Invite dialog did not close after submit")

        # LinkedIn may swap the invite dialog for a Premium upsell when the
        # free note quota is exhausted, instead of closing it after Send —
        # the textarea was filled but the invite was not delivered, so
        # surface LinkedIn's raw dialog text. Gated on the dialog still
        # being open: the same benign nudge banner that can sit alongside a
        # live, fillable textarea (see the reveal-step fix above) can also
        # still be in the DOM for a moment right after a successful Send,
        # before it unmounts with the closing dialog. Checking unconditionally
        # here would report a genuinely delivered invite as blocked. A dialog
        # that failed to close is real evidence something went wrong; one
        # that closed on schedule is not, banner or no banner.
        if note and not dialog_closed:
            note_limit_message = await self._get_premium_upsell_message()
            if note_limit_message is not None:
                logger.info("Premium upsell modal intercepted invite submit")
                await self._dismiss_dialog()
                return False, False, note_limit_message, dialog_text

        return True, note_filled, None, dialog_text

    async def _probe_invite_note_limit(self) -> str | None:
        """Open the note editor only to read a Premium note-quota message.

        This is used when the profile exposed no Connect control. Navigating
        to the custom-invite deeplink and opening the note editor is
        non-destructive, but submitting would weaken the write gate for
        follow-only/unavailable profiles. Therefore this helper never clicks
        the primary Send button: it returns the raw LinkedIn Premium dialog
        text if LinkedIn shows it while opening the note editor, then
        dismisses the dialog.
        """
        if await self._ready_dialog() is None:
            if await self._modal_index() >= 0:
                await self._dismiss_dialog()
            return None
        dialog = await self._invite_dialog()
        if dialog is None:
            return None
        note_limit_message = await self._get_premium_upsell_message(timeout=500)
        if note_limit_message is not None:
            await self._dismiss_dialog()
            return note_limit_message

        try:
            textarea_count = await dialog.locator(_DIALOG_TEXTAREA).count()
        except Exception:
            textarea_count = 0
        if textarea_count > 0:
            await self._dismiss_dialog()
            return None

        buttons = dialog.locator(_DIALOG_BUTTONS)
        try:
            btn_count = await buttons.count()
        except Exception:
            btn_count = 0
        if btn_count >= 3:
            try:
                await buttons.nth(btn_count - 2).click()
            except Exception:
                logger.debug("Could not open invite note editor", exc_info=True)
            try:
                await dialog.locator(_DIALOG_TEXTAREA).first.wait_for(
                    state="visible", timeout=3000
                )
            except PlaywrightTimeoutError:
                logger.debug("Note textarea did not appear during quota probe")

        note_limit_message = await self._get_premium_upsell_message()
        await self._dismiss_dialog()
        return note_limit_message

    async def withdraw_invitations(
        self,
        older_than_days: int,
        *,
        exclude_usernames: list[str] | None = None,
        max_withdrawals: int = 20,
        dry_run: bool = True,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Withdraw (or list, on a dry run) old pending sent invitations."""
        return await InvitationWithdrawer(
            self,
            sent_list_url=_SENT_INVITATIONS_URL,
        ).withdraw_invitations(
            older_than_days,
            exclude_usernames=exclude_usernames,
            max_withdrawals=max_withdrawals,
            dry_run=dry_run,
            deadline=deadline,
        )

    async def connect_with_person(
        self,
        username: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Send a LinkedIn connection request or accept an incoming one.

        Detection is locale-independent (see ``connection.py``): the state
        comes from the top card and its More menu, and every state is
        reported only on positive evidence. The write gate is
        ``connectable``, which only a ``_connect`` connect-state control
        produces; the send path clicks that same control, so a Pending,
        1st-degree, follow-only or unreadable profile cannot trigger a
        write. If a note was requested and no Connect control exists, the
        custom-invite deeplink may still be opened, only as a non-submitting
        note-quota probe.
        """
        username = normalize_person_identifier(username)
        url = person_profile_url(username, "/")

        profile = await self._read_main_profile(username)
        page_text = profile.get("sections", {}).get("main_profile", "")
        if not page_text:
            refusal = profile.get("section_errors", {}).get("main_profile")
            if (
                isinstance(refusal, dict)
                and refusal.get("error_type") == "limit_exceeded"
            ):
                raise ActionLimitError.from_section_error(refusal)
            return _connection_result(
                url, "unavailable", "Could not read profile page."
            )

        signals = await self._read_action_signals(username)
        state = connection.detect_connection_state(signals)
        logger.info(
            "Connection signals for %s: state=%s signals=%s", username, state, signals
        )

        # Fallback only for the states that rest on absence: a menu that could
        # not be read, signals no state explains, or a menu without a Connect
        # or Pending item, which is also what a menu read in the middle of a
        # re-render looks like. The sent list shows the newest ten
        # invitations, so a hit proves pending while a miss proves nothing,
        # and it costs a navigation a conclusive menu does not.
        if state in (
            "unavailable",
            "follow_only",
        ) and await self._invitation_in_sent_list(username):
            state = "pending"

        if state == "self_profile":
            return _connection_result(
                url,
                "connect_unavailable",
                "Cannot send a connection request to your own profile.",
                profile=page_text,
            )
        if state == "already_connected":
            return _connection_result(
                url,
                "already_connected",
                "You are already connected with this profile.",
                profile=page_text,
            )
        if state == "pending":
            return _connection_result(
                url,
                "pending",
                "A connection request is already pending for this profile.",
                profile=page_text,
            )

        if state == "incoming_request":
            # There is deliberately no locale-text fallback: accepting is
            # irreversible, so when the structural click does not land we
            # report send_failed rather than guess.
            clicked = await self._click_incoming_accept()
            if not clicked:
                return _connection_result(
                    url,
                    "send_failed",
                    "Could not find or click the Accept button.",
                    profile=page_text,
                )
            # LinkedIn propagates the accepted state asynchronously; an
            # immediate re-read can still render the old top card and
            # would report send_failed for a successful accept (observed
            # live 2026-06-11). Verify with one settle retry.
            verified_text = ""
            verified_state = None
            for attempt in range(2):
                if attempt:
                    await asyncio.sleep(3.0)
                verified = await self._read_main_profile(username)
                verified_text = verified.get("sections", {}).get("main_profile", "")
                verified_signals = await self._read_action_signals(username)
                verified_state = connection.detect_connection_state(verified_signals)
                if verified_state == "already_connected":
                    break
            if verified_state != "already_connected":
                return _connection_result(
                    url,
                    "send_failed",
                    "Accepted, but the profile did not transition to 1st-degree.",
                    profile=verified_text or page_text,
                )
            return _connection_result(
                url,
                "accepted",
                "Connection request accepted.",
                profile=verified_text,
            )

        if state != "connectable":
            # When a note is requested without a Connect control, open the
            # deeplink only as a non-submitting probe so we can report the
            # Premium note-quota block without accidentally sending from a
            # follow-only or otherwise unavailable profile.
            if note:
                logger.info(
                    "No Connect control for %s; probing custom-invite deeplink "
                    "because a personalized note was requested",
                    username,
                )
                await self._navigator._navigate_to_page(
                    "https://www.linkedin.com/preload/custom-invite/"
                    f"?vanityName={quote_plus(username)}"
                )
                note_limit_message = await self._probe_invite_note_limit()
                if note_limit_message is not None:
                    return _connection_result(
                        url,
                        "custom_note_limit_reached",
                        note_limit_message,
                        note_sent=False,
                        profile=page_text,
                    )
            if state == "unavailable":
                return _connection_result(
                    url,
                    "connect_unavailable",
                    "Could not determine this profile's connection state from"
                    " its top card and More menu; no invitation was sent.",
                    profile=page_text,
                )
            return _connection_result(
                url,
                "connect_unavailable",
                "LinkedIn did not expose a usable Connect action for this profile.",
                profile=page_text,
            )

        if not await self._click_connect():
            return _connection_result(
                url,
                "connect_unavailable",
                "LinkedIn showed a Connect action for this profile, but it could"
                " not be clicked; no invitation was sent.",
                profile=page_text,
            )

        (
            submitted,
            note_sent,
            note_limit_message,
            dialog_text,
        ) = await self._submit_invite_dialog(note)
        if note_limit_message is not None:
            return _connection_result(
                url,
                "custom_note_limit_reached",
                note_limit_message,
                note_sent=False,
                profile=page_text,
            )
        if not submitted and dialog_text is not None:
            return _connection_result(
                url,
                "connect_unavailable",
                "LinkedIn opened a dialog that could not be submitted as an invite."
                f" Dialog text: {dialog_text}",
                profile=page_text,
            )
        no_dialog = not submitted
        landed = alerts = ""
        if no_dialog:
            # Read before the sent-list navigation leaves the page. Measured
            # 2026-09-14 on an account with ~255 pending invites: Connect did
            # nothing even when clicked by hand, so this is usually LinkedIn
            # refusing, and its notice is the caller's only clue.
            landed = self._session.page.url
            alerts = await self._page_alerts()

        # The click itself may have sent the invite without asking for a
        # note, so even a click that opened no dialog is checked against the
        # sent list before it is reported as unsent.
        try:
            listed = await self._invitation_in_sent_list(username)
        except Exception as exc:
            # The write may already have happened; a failed read must not
            # hide it.
            logger.warning("Could not verify invitation to %s: %s", username, exc)
            return _connection_result(
                url,
                "send_unverified",
                (
                    "Clicked Connect; no invite dialog opened"
                    if no_dialog
                    else "Submitted the invite dialog"
                )
                + " but could not read the sent invitations list to confirm"
                f" whether it was sent: {exc}",
                note_sent=note_sent,
                profile=page_text,
            )
        if listed:
            return _connection_result(
                url,
                "connected",
                "Connection request sent; it appears in the sent invitations list."
                + (
                    " LinkedIn sent it on the Connect click without an invite"
                    " dialog, so no note was attached."
                    if no_dialog and note
                    else ""
                ),
                note_sent=note_sent,
                profile=page_text,
            )
        if no_dialog:
            return _connection_result(
                url,
                "dialog_not_found",
                "LinkedIn opened no invite dialog after the Connect click"
                f" (landed on {landed}; selector {_DIALOG_SELECTOR!r}), and the"
                " invitation does not appear in the sent invitations list."
                + (f" Page notice: {alerts}" if alerts else ""),
                profile=page_text,
            )
        return _connection_result(
            url,
            "not_sent",
            "Submitted the invite dialog but the invitation does not appear in"
            f" the sent invitations list. Dialog text: {dialog_text}",
            note_sent=False,
            profile=page_text,
        )
