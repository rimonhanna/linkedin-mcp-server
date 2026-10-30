"""Withdraw old pending sent invitations from the sent-invitations page.

The sent list is the only place LinkedIn shows how old an invitation is, and
that age is free text ("Sent 3 weeks ago"). It is parsed here into approximate
days, and anything that does not parse counts as *not old*: a withdrawal is
irreversible (the person can be re-invited only after a waiting period), so an
uncertain row is never touched.

The page is opened through ``PageNavigator`` like every other tool, so the
load is charged to the shared hourly budget. The clicks that follow are not
navigations; between them the account cooldown is re-read so a throttle signal
raised by another session stops this loop at once.

The list loads by infinite scroll. Every result carries ``rows_seen``; zero
rows ends the run with ``stopped_reason`` ``no_rows_found``, so an empty
report can be told apart from a page that could not be read.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import asyncio
import logging
import random
import re
import time
from urllib.parse import unquote

from linkedin_mcp_server.exceptions import ActionLimitError
from linkedin_mcp_server.core.exceptions import RateLimitError
from linkedin_mcp_server.pacing import (
    WITHDRAWALS,
    JobStore,
    admit_write,
    cooldown_resume_at,
    note_throttle_signal,
    record_write,
    step_delay_range,
)
from linkedin_mcp_server.scraping.identifiers import normalize_person_identifier

logger = logging.getLogger(__name__)

MAX_WITHDRAWALS_LIMIT = 50
# Infinite scroll loads 10 rows per round; this bounds a pathological list.
_MAX_LOAD_ROUNDS = 200
_STABLE_ROUNDS = 3
# A scroll round ends as soon as the row count grows; this bounds the wait when
# it does not (the end of the list, or a slow load).
_SCROLL_POLL_SECONDS = 0.25
_SCROLL_ROUND_POLLS = 12
# A row that is gone must stay gone this long before it counts as withdrawn.
_REMOVAL_RECHECK_SECONDS = 1.5

_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}
# "Sent today", "Sent yesterday", "Sent 3 weeks ago", "Sent a month ago".
# Hours and minutes are not units: they round to today. A whole line only: a
# headline or a note that merely says "sent 3 weeks ago" is not the age.
_AGE_RE = re.compile(
    r"^[ \t]*sent[ \t]+(?:(?P<word>today|yesterday)"
    r"|(?:(?P<count>\d+|an?)[ \t]+(?P<unit>second|minute|hour|day|week|month|year)s?[ \t]+ago))"
    r"[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_IN_PATH_RE = re.compile(r"/in/([^/?#]+)")

_ROW_SELECTOR = 'main [role="listitem"]'

# Rows are ``div[role="listitem"]`` (not ``li``), one per invitation, each with
# a profile link. The age is the full text of a leaf ``span`` inside a ``p``;
# only leaf texts starting with "Sent" are returned, one per line, so a "Sent"
# buried in a longer element never counts. Text is returned raw for Python.
_READ_ROWS_JS = r"""
() => {
  const rows = [];
  document.querySelectorAll('main [role="listitem"]').forEach((row) => {
    const a = row.querySelector('a[href*="/in/"]');
    if (!a) return;
    const control = row.querySelector('a[aria-label^="Withdraw invitation sent to"]');
    const label = control ? control.getAttribute('aria-label') : '';
    const named = [...row.querySelectorAll('a[href*="/in/"]')]
      .map((x) => (x.innerText || '').trim()).find(Boolean) || '';
    const ages = [...row.querySelectorAll('p span')]
      .filter((el) => el.children.length === 0)
      .map((el) => (el.textContent || '').trim())
      .filter((t) => /^sent\b/i.test(t));
    rows.push({
      href: a.getAttribute('href') || '',
      name: label.replace(/^Withdraw invitation sent to /, '').trim()
        || named.split('\n')[0].trim(),
      text: ages.join('\n'),
    });
  });
  return rows;
}
"""

# How many invitation rows are on the page right now.
_COUNT_ROWS_JS = r"""
() => [...document.querySelectorAll('main [role="listitem"]')]
  .filter((row) => row.querySelector('a[href*="/in/"]')).length
"""

# The list loads by infinite scroll: scrolling the container's last child (an
# empty sentinel) into view loads 10 more rows. Returns whether it was found.
_SCROLL_LIST_JS = r"""
() => {
  const row = [...document.querySelectorAll('main [role="listitem"]')]
    .find((r) => r.querySelector('a[href*="/in/"]'));
  const sentinel = row && row.parentElement && row.parentElement.lastElementChild;
  if (!sentinel) return false;
  sentinel.scrollIntoView({ block: 'end' });
  return true;
}
"""

# Shared by the click and the removal check so both name a row the same way.
_SLUG_JS = r"""
  const slug = (href) => {
    const m = /\/in\/([^/?#]+)/.exec(href || '');
    if (!m) return '';
    try { return decodeURIComponent(m[1]).toLowerCase(); } catch (e) { return m[1].toLowerCase(); }
  };
"""

# Click the row's Withdraw control, an ``<a>`` labelled "Withdraw invitation
# sent to <name>". The row is the one whose OWN first profile link is the
# target (a mutual-connection link inside another row must not select it);
# names compare URL-decoded and lower-cased. More than one such row, or other
# than one control, is a refusal: which control withdraws would be a guess.
_CLICK_WITHDRAW_JS = (
    r"""
(username) => {
"""
    + _SLUG_JS
    + r"""
  const rows = [...document.querySelectorAll('main [role="listitem"]')].filter((row) => {
    const a = row.querySelector('a[href*="/in/"]');
    return a && slug(a.getAttribute('href')) === username;
  });
  if (rows.length !== 1) return false;
  const controls = rows[0].querySelectorAll('a[aria-label^="Withdraw invitation sent to"]');
  if (controls.length !== 1) return false;
  controls[0].click();
  return true;
}
"""
)

# How many list rows belong to the person, by the same rule the click uses. A
# profile link elsewhere on the page (a toast, a mutual-connection link) is not
# a row.
_ROWS_FOR_USER_JS = (
    r"""
(username) => {
"""
    + _SLUG_JS
    + r"""
  return [...document.querySelectorAll('main [role="listitem"]')].filter((row) => {
    const a = row.querySelector('a[href*="/in/"]');
    return a && slug(a.getAttribute('href')) === username;
  }).length;
}
"""
)

# Text of every open native ``<dialog>``. Closed ones (ad options) are in the
# page too and are not "open".
_OPEN_DIALOGS_JS = r"""
() => [...document.querySelectorAll('dialog[open]')]
  .filter((d) => d.getClientRects().length > 0)
  .map((d) => d.innerText || '')
"""

# Press the button whose text is exactly "Withdraw" in the open dialog that
# says "Withdraw invitation", but only when its aria-label names the planned
# person. The label abbreviates the last name ("Dajana M."), so only the first
# name (lower-cased, passed in) is compared. Answers "clicked", "name_mismatch"
# (nothing pressed) or "not_found".
_CONFIRM_WITHDRAW_JS = r"""
(firstName) => {
  const dialogs = [...document.querySelectorAll('dialog[open]')]
    .filter((d) => (d.innerText || '').includes('Withdraw invitation'));
  if (dialogs.length !== 1) return 'not_found';
  const buttons = [...dialogs[0].querySelectorAll('button')]
    .filter((b) => (b.innerText || '').trim() === 'Withdraw');
  if (buttons.length !== 1) return 'not_found';
  const label = (buttons[0].getAttribute('aria-label') || '').toLowerCase();
  const named = label.startsWith('withdraw invitation sent to ')
    ? label.slice('withdraw invitation sent to '.length) : null;
  if (!firstName || named === null
      || !(named === firstName || named.startsWith(firstName + ' '))) {
    return 'name_mismatch';
  }
  buttons[0].click();
  return 'clicked';
}
"""

# Close the withdraw dialog without withdrawing: its Cancel button, else the
# Dismiss (X) button. Answers whether one was pressed.
_CANCEL_DIALOG_JS = r"""
() => {
  const dialogs = [...document.querySelectorAll('dialog[open]')]
    .filter((d) => (d.innerText || '').includes('Withdraw invitation'));
  if (dialogs.length !== 1) return false;
  const buttons = [...dialogs[0].querySelectorAll('button')];
  const button = buttons.find((b) => (b.innerText || '').trim() === 'Cancel')
    || buttons.find((b) => b.getAttribute('aria-label') === 'Dismiss');
  if (!button) return false;
  button.click();
  return true;
}
"""


def _single_age_match(text: str) -> re.Match[str] | None:
    """The one "Sent ..." line of a row; ``None`` for none or for several."""
    matches = list(_AGE_RE.finditer(text or ""))
    return matches[0] if len(matches) == 1 else None


def parse_age_days(text: str) -> int | None:
    """Approximate age in days from a row's text, ``None`` when unreadable."""
    match = _single_age_match(text)
    if match is None:
        return None
    word = (match.group("word") or "").lower()
    if word:
        return 0 if word == "today" else 1
    unit = match.group("unit").lower()
    if unit in ("second", "minute", "hour"):
        return 0
    count = match.group("count").lower()
    return (1 if count in ("a", "an") else int(count)) * _UNIT_DAYS[unit]


def age_text(text: str) -> str:
    """The matched "Sent ..." phrase, for reporting."""
    match = _single_age_match(text)
    return match.group(0).strip() if match else ""


def username_from_href(href: str) -> str:
    """Lower-cased vanity name from a profile href, empty when none."""
    match = _IN_PATH_RE.search(href or "")
    return unquote(match.group(1)).lower() if match else ""


def normalize_exclusions(values: list[str] | None) -> set[str]:
    """Usernames or /in/ URLs reduced to lower-cased usernames."""
    return {normalize_person_identifier(v).lower() for v in values or []}


def plan_withdrawals(
    rows: list[dict[str, Any]],
    *,
    older_than_days: int,
    excluded: set[str],
    limit: int,
) -> dict[str, Any]:
    """Split rows into those to withdraw (capped at ``limit``) and the skips.

    Exclusions are reported only for rows that would otherwise be withdrawn.
    """
    candidates: list[tuple[int, dict[str, str]]] = []
    skipped_excluded: list[str] = []
    skipped_unparsed: list[str] = []
    for row in rows:
        username = username_from_href(row.get("href", ""))
        if not username:
            continue
        text = row.get("text", "")
        days = parse_age_days(text)
        if days is None:
            skipped_unparsed.append(username)
            continue
        if days < older_than_days:
            continue
        if username in excluded:
            skipped_excluded.append(username)
            continue
        candidates.append(
            (
                days,
                {
                    "username": username,
                    "name": row.get("name", ""),
                    "age_text": age_text(text),
                },
            )
        )
    # Oldest first, so a capped run clears the stalest invitations.
    candidates.sort(key=lambda item: -item[0])
    return {
        "chosen": [invite for _, invite in candidates[:limit]],
        "skipped_excluded": skipped_excluded,
        "skipped_unparsed_age": skipped_unparsed,
    }


class InvitationWithdrawer:
    """Reads the sent list and withdraws old invitations through the connection owner."""

    def __init__(self, actions: Any, *, sent_list_url: str) -> None:
        # ``actions`` is the connection owner, which hands over the sent-list
        # URL it already uses for send verification; importing it here would
        # make the two modules import each other.
        self._actions = actions
        self._sent_list_url = sent_list_url
        self._sent_list_path = "/mynetwork/invitation-manager/sent"
        self._confirm_clicked = False
        self._alert_text = ""

    @property
    def _page(self):
        return self._actions._session.page

    async def _load_rows(
        self, deadline: float | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """The rows, and why loading more stopped early (``None`` when it did not)."""
        await self._actions._navigator._navigate_to_page(self._sent_list_url)
        try:
            await self._page.locator(_ROW_SELECTOR).first.wait_for(timeout=10000)
        except Exception:
            logger.debug("Sent-invitations list rendered no rows")
        count = int(await self._page.evaluate(_COUNT_ROWS_JS))
        stable = 0
        rounds = 0
        while count and stable < _STABLE_ROUNDS:
            if rounds >= _MAX_LOAD_ROUNDS or (
                deadline is not None and time.monotonic() >= deadline
            ):
                return await self._page.evaluate(_READ_ROWS_JS), "list_not_fully_loaded"
            rounds += 1
            if not await self._page.evaluate(_SCROLL_LIST_JS):
                break
            grown = count
            for _ in range(_SCROLL_ROUND_POLLS):
                await asyncio.sleep(_SCROLL_POLL_SECONDS)
                grown = int(await self._page.evaluate(_COUNT_ROWS_JS))
                if grown > count:
                    break
            stable = stable + 1 if grown == count else 0
            count = grown
        rows: list[dict[str, Any]] = await self._page.evaluate(_READ_ROWS_JS)
        return rows, None

    async def _open_dialogs(self) -> list[str]:
        return list(await self._page.evaluate(_OPEN_DIALOGS_JS))

    async def _confirm_dialog_is(self, *, present: bool, timeout: int) -> bool:
        """Poll until the "Withdraw invitation" dialog is (not) open, or ``timeout`` ms pass."""
        deadline = asyncio.get_running_loop().time() + timeout / 1000
        while True:
            texts = await self._open_dialogs()
            if any("Withdraw invitation" in t for t in texts) == present:
                return True
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.25)

    async def _cancel_dialog(self) -> None:
        """Close the withdraw dialog without confirming, so a stop leaves none open."""
        try:
            if await self._page.evaluate(_CANCEL_DIALOG_JS):
                await self._confirm_dialog_is(present=False, timeout=5000)
        except Exception:
            logger.debug("Could not close the withdraw dialog", exc_info=True)

    async def _row_listed(self, username: str) -> bool:
        return int(await self._page.evaluate(_ROWS_FOR_USER_JS, username)) > 0

    async def _withdraw_one(self, invite: dict[str, str]) -> str | None:
        """Withdraw one invitation; ``None`` on success, else the stop reason."""
        username = invite["username"]
        name_parts = invite.get("name", "").split()
        first_name = name_parts[0].lower() if name_parts else ""
        self._confirm_clicked = False
        self._alert_text = ""
        if self._sent_list_path not in self._page.url:
            return "left_sent_list"
        if await self._open_dialogs():
            return "dialog_already_open"
        if not await self._page.evaluate(_CLICK_WITHDRAW_JS, username):
            return "withdraw_button_not_found"
        if not await self._confirm_dialog_is(present=True, timeout=5000):
            return "confirmation_dialog_not_found"
        # Charged as soon as the confirm may have been pressed: if the script
        # throws, the click can already have run.
        self._confirm_clicked = True
        try:
            # The dialog shell opens ~0.1s after the click, its buttons ~0.3s
            # later; keep looking for the Withdraw button for up to 3s.
            for _ in range(12):
                outcome = await self._page.evaluate(_CONFIRM_WITHDRAW_JS, first_name)
                if outcome != "not_found":
                    break
                await asyncio.sleep(0.25)
        except Exception:
            record_write(JobStore(), WITHDRAWALS, datetime.now(timezone.utc))
            raise
        if outcome != "clicked":
            self._confirm_clicked = False
            await self._cancel_dialog()
            return (
                "confirmation_dialog_name_mismatch"
                if outcome == "name_mismatch"
                else "confirmation_click_failed"
            )
        record_write(JobStore(), WITHDRAWALS, datetime.now(timezone.utc))
        if not await self._confirm_dialog_is(present=False, timeout=5000):
            await self._cancel_dialog()
            return "confirmation_dialog_still_open"
        if self._sent_list_path not in self._page.url:
            note_throttle_signal("withdraw_left_sent_list")
            return "left_sent_list"
        removed = False
        for _ in range(4):
            if not await self._row_listed(username):
                removed = True
                break
            await asyncio.sleep(1.0)
        if removed:
            # A row that comes back (an optimistic removal rolled back by a
            # failed request) is not withdrawn.
            await asyncio.sleep(_REMOVAL_RECHECK_SECONDS)
            removed = not await self._row_listed(username)
        alerts = await self._actions._page_alerts()
        self._alert_text = alerts
        # "Invitation withdrawn" is LinkedIn confirming success, not pushback.
        if alerts and "withdrawn" not in alerts.lower():
            note_throttle_signal("withdraw_alert")
            return f"linkedin_alert: {alerts[:200]}"
        return None if removed else "withdrawal_unverified"

    async def withdraw_invitations(
        self,
        older_than_days: int,
        *,
        exclude_usernames: list[str] | None = None,
        max_withdrawals: int = 20,
        dry_run: bool = True,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Withdraw (or, on a dry run, list) sent invitations older than a threshold.

        ``deadline`` is a ``time.monotonic()`` instant after which no further
        withdrawal starts, so the partial report is returned before the tool
        timeout cancels the call.
        """
        if older_than_days < 1:
            raise ValueError("older_than_days must be at least 1")
        excluded = normalize_exclusions(exclude_usernames)
        limit = max(0, min(max_withdrawals, MAX_WITHDRAWALS_LIMIT))
        result: dict[str, Any] = {
            "withdrawn": [],
            "skipped_excluded": [],
            "skipped_unparsed_age": [],
            "unconfirmed": [],
            "rows_seen": 0,
            "stopped_reason": None,
        }
        if dry_run:
            result["would_withdraw"] = []
        try:
            rows, load_stop = await self._load_rows(deadline)
        except (RateLimitError, ActionLimitError) as exc:
            result["stopped_reason"] = f"throttled: {exc}"
            return result
        result["rows_seen"] = len(rows)
        if not rows:
            # An empty list is "could not read the page", never "nothing old".
            result["stopped_reason"] = load_stop or "no_rows_found"
            return result
        if load_stop is not None:
            # The list is only partly loaded and is sorted newest first, so
            # the oldest invitations are the ones missing.
            result["stopped_reason"] = load_stop
            return result

        plan = plan_withdrawals(
            rows, older_than_days=older_than_days, excluded=excluded, limit=limit
        )
        result["skipped_excluded"] = plan["skipped_excluded"]
        result["skipped_unparsed_age"] = plan["skipped_unparsed_age"]
        if dry_run:
            result["would_withdraw"] = plan["chosen"]
            return result

        low, high = step_delay_range()
        for index, invite in enumerate(plan["chosen"]):
            pause = random.uniform(low, high) if index else 0.0
            # The pause and one withdrawal (about 20 s of waits) must fit.
            if deadline is not None and time.monotonic() + pause + 20 >= deadline:
                result["stopped_reason"] = "deadline"
                return result
            if pause:
                await asyncio.sleep(pause)
            try:
                resume_at = cooldown_resume_at(JobStore(), datetime.now(timezone.utc))
                if resume_at is None:
                    admit_write(JobStore(), WITHDRAWALS, datetime.now(timezone.utc))
            except ActionLimitError as exc:
                result["stopped_reason"] = (
                    "hourly_cap" if exc.window == "1 h" else f"action_limit: {exc}"
                )
                return result
            except Exception as exc:
                logger.warning("Pacing check failed", exc_info=True)
                result["stopped_reason"] = f"error: {type(exc).__name__}: {exc}"[:200]
                return result
            if resume_at is not None:
                result["stopped_reason"] = (
                    f"account_cooldown until {resume_at.isoformat(timespec='seconds')}"
                )
                return result
            try:
                reason = await self._withdraw_one(invite)
            except Exception as exc:
                logger.warning(
                    "Withdrawing %s failed", invite["username"], exc_info=True
                )
                reason = f"error: {type(exc).__name__}: {exc}"[:200]
            if reason is not None:
                if self._confirm_clicked:
                    result["unconfirmed"].append(
                        {**invite, "alert_text": self._alert_text}
                    )
                result["stopped_reason"] = reason
                return result
            result["withdrawn"].append({**invite, "alert_text": self._alert_text})
        return result
