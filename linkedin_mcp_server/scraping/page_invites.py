"""Invite 1st-degree connections to follow a company page the account administers.

Both workflows open the page's own "Invite to follow" dialog
(``/company/<id>/admin/dashboard/?invite=true``, one charged page load) and read
the candidates, their invitability and the page's monthly credits from the
responses the dialog itself receives (``page_invite_payload``). Nothing is
replayed: more candidates come from the dialog's "Show more results" control,
a name search from typing into its search box, and the invitation from ticking
the planned rows and pressing its Invite button.

An account that does not administer the page is sent to ``/company/no-access/``
by LinkedIn; that path, not any text, is the signal.

The write is held to the plan at three points: each row is ticked by the
profile id in its checkbox id; the dialog's checked set must then equal the
plan exactly; and ``page_invite_guard`` lets through only one batch whose
invitees are exactly the plan. An invitation whose outcome is unknown is
reported as such and never sent again.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlparse

from patchright.async_api import Error as PlaywrightError

from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
    LinkedInScraperException,
)
from linkedin_mcp_server.exceptions import ActionLimitError
from linkedin_mcp_server.pacing import (
    PAGE_INVITES,
    JobStore,
    cooldown_resume_at,
    hourly_headroom,
    kind_headroom,
    load_account_budget,
    record_write,
    refuse_if_limited,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.page_invite_api import (
    InviteResponse,
    PageInviteCapture,
)
from linkedin_mcp_server.scraping.page_invite_guard import (
    explicit_page_invite,
    without_page_invites,
)
from linkedin_mcp_server.scraping.page_invite_payload import (
    CREDITS,
    INVITATIONS,
    STATUSES,
    SUGGESTIONS,
    TYPEAHEAD,
    Candidate,
    Credits,
    InviteeRef,
    batch_statuses,
    can_match_a_row,
    dialog_community,
    embedded_credits,
    invitable_by_profile,
    invite_dialog_url,
    invitee_ref,
    matching_rows,
    page_credits,
    profile_url,
    ref_matches,
    status_labels,
    suggestion_candidates,
    suggestion_page,
    typeahead_candidates,
)
from linkedin_mcp_server.scraping.session import ScrapingSession

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 200
# How far an invite pages through the candidates to find the people it was
# given: each page is one in-app request (and the status request after it), not
# a page load. 300 is 28 "Show more results" presses at about 2.5 s each, which
# fits the default tool deadline (180 s x 0.75) after the page load and the
# send's reserve; the old 500 (48 presses) ran into the deadline first, so its
# last pages were never reached and only added requests.
MAX_INVITE_CANDIDATES = 300
MAX_INVITEES = 20
_READY_TIMEOUT = 12.0
_PAGE_TIMEOUT = 10.0
_SEND_TIMEOUT = 15.0
# What a send needs once it starts: up to 20 ticks about a second apart, Invite
# enabling (2 s) and LinkedIn's answer (``_SEND_TIMEOUT``), with a margin. Less
# than this left and nothing is ticked, so a deadline never cuts a send short.
_SEND_BUDGET_SECONDS = 40.0
_POLL_SECONDS = 0.25
# A name search fires per keystroke; the last result set is the one shown once
# no new one has arrived for this long.
_SEARCH_SETTLE_SECONDS = 1.5
# What one name search takes: typing at 120 ms a key, the results settling and
# their status call (1.5 s each), with a margin.
_SEARCH_SECONDS = 8.0
# How long to wait for the dialog's status response once the candidates are in:
# it follows its suggestions page by about half a second (measured 0.24-0.54 s).
_STATUS_TIMEOUT = 3.0


# Each "Show more results" adds 10 candidates after the first 20.
def _more_pages(candidates: int) -> int:
    return max(0, (candidates - 20) // 10)


_NO_ACCESS_PATH = "/company/no-access"
_DIALOG_PATH = "/admin/dashboard"
# The only text this module matches on: the paging control has no attribute
# or structure that sets it apart from the dialog's other buttons. en-US only;
# another locale stops paging with ``more_control_not_found``.
SHOW_MORE_LABELS = ("Show more results",)

_DIALOG_JS = r"""
  // The invite dialog: the open dialog whose listbox holds invitee rows.
  const dialog = () => [...document.querySelectorAll('[role="dialog"]')].find(
    (d) => d.querySelector('[role="listbox"] input[type="checkbox"][id*="invitee-suggestion-"]'));
  const rowId = (input) => {
    const m = /invitee-suggestion-([A-Za-z0-9_-]+)$/.exec(input.id || '');
    return m ? m[1] : null;
  };
"""

# What the dialog shows now: whether it is open, its rows' profile ids, which
# are checked, and how many checkboxes are checked in all (one outside the rows
# would not be named by ``checked``).
_DIALOG_STATE_JS = (
    "() => { // linkedin-mcp:page-invite-state\n"
    + _DIALOG_JS
    + r"""
  const d = dialog();
  if (!d) return {open: false, rows: [], checked: [], checkedTotal: 0};
  const boxes = [...d.querySelectorAll('input[type="checkbox"]')];
  const rows = boxes.map(rowId).filter(Boolean);
  return {
    open: true,
    rows,
    checked: boxes.filter((b) => b.checked).map(rowId).filter(Boolean),
    checkedTotal: boxes.filter((b) => b.checked).length,
  };
}
"""
)

# Tick the one row whose checkbox id ends with the profile id. Answers
# "ticked", "already_checked", "not_found" or "ambiguous" (nothing clicked).
_TICK_JS = (
    "(profileId) => { // linkedin-mcp:page-invite-tick\n"
    + _DIALOG_JS
    + r"""
  const d = dialog();
  if (!d) return 'not_found';
  const boxes = [...d.querySelectorAll('input[type="checkbox"]')]
    .filter((b) => rowId(b) === profileId);
  if (boxes.length === 0) return 'not_found';
  if (boxes.length > 1) return 'ambiguous';
  if (boxes[0].checked) return 'already_checked';
  if (boxes[0].disabled) return 'not_found';
  boxes[0].click();
  return boxes[0].checked ? 'ticked' : 'not_found';
}
"""
)

# Press the dialog's last button, its Invite action, only when its text holds
# exactly one whole number and that number is the count selected. Measured:
# disabled until a row is ticked, then its one text span reads "Invite 1" (no
# brackets, no aria-label), and a ticked row adds a button just before it, so
# it stays last while "Unselect all" is no longer next to it. Position alone
# could press another control, and digits are not a locale's words.
# Answers "clicked", "disabled", "count_mismatch" or "not_found".
_PRESS_INVITE_JS = (
    "(count) => { // linkedin-mcp:page-invite-press\n"
    + _DIALOG_JS
    + r"""
  const d = dialog();
  if (!d) return 'not_found';
  const buttons = [...d.querySelectorAll('button')];
  const invite = buttons[buttons.length - 1];
  if (!invite) return 'not_found';
  if (invite.disabled || invite.getAttribute('aria-disabled') === 'true') return 'disabled';
  const tokens = (invite.innerText || invite.textContent || '').trim().split(/\s+/);
  const numbers = tokens.filter((t) => /^\d+$/.test(t));
  if (numbers.length !== 1 || numbers[0] !== String(count)) return 'count_mismatch';
  invite.click();
  return 'clicked';
}
"""
)

# Press the paging control, found by its label alone. Answers "clicked" or
# "none" (absent, disabled, or more than one).
_SHOW_MORE_JS = (
    "(labels) => { // linkedin-mcp:page-invite-more\n"
    + _DIALOG_JS
    + r"""
  const d = dialog();
  if (!d) return 'none';
  const matches = [...d.querySelectorAll('button')]
    .filter((b) => labels.includes((b.innerText || '').trim()));
  if (matches.length !== 1 || matches[0].disabled) return 'none';
  matches[0].click();
  return 'clicked';
}
"""
)

_SEARCH_BOX = '[role="dialog"] input[role="combobox"]'


class NotPageAdminError(InvalidReferenceError):
    """LinkedIn sent the member to ``/company/no-access/`` for this page."""

    def __init__(self, company_id: str):
        super().__init__(
            f"This LinkedIn account is not an admin of company page {company_id}, "
            "so LinkedIn did not open its invite dialog. Only a page admin can "
            "invite connections to follow it."
        )


def _from_dialog(response: InviteResponse, company_id: str) -> bool:
    return (
        response.ok
        and response.call.method == "GET"
        and response.call.params.get("community") == dialog_community(company_id)
    )


def _is_suggestions(response: InviteResponse, company_id: str, start: int) -> bool:
    return (
        response.call.resource == SUGGESTIONS
        and _from_dialog(response, company_id)
        and response.call.params.get("start", "0") == str(start)
    )


def _is_search(response: InviteResponse) -> bool:
    return (
        response.ok
        and response.call.resource == TYPEAHEAD
        and response.call.method == "GET"
    )


def _is_credits(response: InviteResponse, company_id: str) -> bool:
    return response.call.resource == CREDITS and _from_dialog(response, company_id)


def _is_send(response: InviteResponse) -> bool:
    return response.call.resource == INVITATIONS and response.call.method == "POST"


class _Dialog:
    """What the dialog's own responses said, rebuilt from the capture."""

    def __init__(self, capture: PageInviteCapture, company_id: str):
        self._capture = capture
        self._company_id = company_id

    def candidates(self) -> tuple[list[Candidate], bool] | None:
        """Every suggestion page in order, and whether the last one was full.

        ``None`` when a page could not be read or the pages do not join up.
        """
        pages: dict[int, tuple[int, list[Candidate]]] = {}
        for response in self._capture.responses:
            if response.call.resource != SUGGESTIONS or not _from_dialog(
                response, self._company_id
            ):
                continue
            rows = suggestion_candidates(response.payload)
            asked = response.call.params.get("count", "")
            span = suggestion_page(response.payload)
            if rows is None or span is None or not asked.isdigit():
                return None
            pages[span[0]] = (int(asked), rows)
        ordered: list[Candidate] = []
        full = False
        for start in sorted(pages):
            if start != len(ordered):
                return None
            asked, rows = pages[start]
            ordered.extend(rows)
            full = len(rows) >= asked
        seen: set[str] = set()
        unique = [
            row
            for row in ordered
            if not (row["profileId"] in seen or seen.add(row["profileId"]))
        ]
        return unique, full

    def search_results(self, after: int) -> list[Candidate] | None:
        found = self._capture.matching(_is_search, after)
        return typeahead_candidates(found[-1].payload) if found else None

    def invitable(self) -> dict[str, bool]:
        statuses: dict[str, bool] = {}
        for response in self._capture.responses:
            if response.call.resource == STATUSES and _from_dialog(
                response, self._company_id
            ):
                statuses.update(invitable_by_profile(response.payload) or {})
        return statuses

    def labels(self) -> dict[str, str]:
        labels: dict[str, str] = {}
        for response in self._capture.responses:
            if response.call.resource == STATUSES and _from_dialog(
                response, self._company_id
            ):
                labels.update(status_labels(response.payload))
        return labels

    def credits(self) -> Credits | None:
        found = [r for r in self._capture.responses if _is_credits(r, self._company_id)]
        return page_credits(found[-1].payload) if found else None


def _decorate(rows: list[Candidate], invitable: dict[str, bool]) -> list[Candidate]:
    out: list[Candidate] = []
    for row in rows:
        candidate: Candidate = {**row}
        candidate["profileUrl"] = profile_url(row)
        candidate["invitable"] = invitable.get(row["profileId"])
        out.append(candidate)
    return out


def _line(candidate: Candidate) -> str:
    parts = [
        candidate.get("name") or "(name not shown)",
        candidate.get("headline") or "",
        candidate["profileId"],
        {True: "invitable", False: "not invitable", None: "invitability unknown"}[
            candidate.get("invitable")
        ],
    ]
    return " | ".join(part for part in parts if part)


def _pacing_headroom(
    now: datetime, *, dry_run: bool = False
) -> tuple[int, str | None, str | None]:
    """Page invitations the account may send now, why none when zero, and the
    schedule note.

    A dry run sends nothing, so outside working hours it still plans under the
    real caps and reports the schedule apart, as the third value, instead of
    planning no one.
    """
    store = JobStore()
    resume_at = cooldown_resume_at(store, now)
    if resume_at is not None:
        return (
            0,
            f"account_cooldown until {resume_at.isoformat(timespec='seconds')}",
            None,
        )
    budget = load_account_budget(store, now)
    schedule: str | None = None
    try:
        refuse_if_limited(budget, PAGE_INVITES, now)
    except ActionLimitError as exc:
        if not (dry_run and exc.schedule_only):
            return 0, _refusal(exc), None
        schedule = _refusal(exc)
        try:
            refuse_if_limited(budget, PAGE_INVITES, now, ignore_schedule=True)
        except ActionLimitError as capped:
            return 0, _refusal(capped), schedule
    left, _wait = kind_headroom(budget, PAGE_INVITES, now, ignore_schedule=dry_run)
    hourly = hourly_headroom(budget, now)
    if hourly <= 0:
        return 0, "hourly_cap", schedule
    return min(left, hourly), None, schedule


def _refusal(exc: ActionLimitError) -> str:
    prefix = "outside_working_hours" if exc.schedule_only else "action_limit"
    return f"{prefix}: {exc}"


def _searchable(ref: InviteeRef) -> bool:
    """Whether a name search's rows could confirm ``ref``: they carry the
    profile id, never the public identifier a vanity name is matched on."""
    return can_match_a_row(ref) and ref.kind != "vanity"


PersonStatus = Literal[
    "would_invite",
    "invited",
    "refused",
    "unconfirmed",
    "not_sent",
    "not_invitable",
    "not_found",
    "invitability_unknown",
    "no_credits",
    "pacing_cap",
    "duplicate",
]


class PageInviteActions:
    """Own the two workflows on a company page's "Invite to follow" dialog."""

    def __init__(self, session: ScrapingSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    @property
    def _page(self) -> Any:
        return self._session.page

    async def _open(self, capture: PageInviteCapture, company_id: str) -> None:
        """Load the dialog and wait for its first page of candidates."""
        await self._navigator._navigate_to_page(invite_dialog_url(company_id))
        await self._session.check_rate_limit()
        end = time.monotonic() + _READY_TIMEOUT
        while True:
            path = urlparse(self._page.url).path
            if path.startswith(_NO_ACCESS_PATH):
                raise NotPageAdminError(company_id)
            if capture.matching(lambda r: _is_suggestions(r, company_id, 0)):
                break
            if time.monotonic() >= end:
                if _DIALOG_PATH not in path:
                    raise LinkedInScraperException(
                        f"The page's invite dialog did not open (LinkedIn showed "
                        f"{path}), so nothing was read or sent."
                    )
                # Raises, counting a half throttle signal like any missed payload.
                await capture.wait_for(
                    lambda r: _is_suggestions(r, company_id, 0),
                    timeout=0.01,
                    wanted=(SUGGESTIONS,),
                )
            await asyncio.sleep(_POLL_SECONDS)
        await capture.settle()

    async def _credits(self, dialog: _Dialog, company_id: str) -> Credits | None:
        # A direct load renders the credits into the page instead of fetching them.
        captured = dialog.credits()
        if captured is not None:
            return captured
        try:
            page_html = await self._page.content()
        except PlaywrightError as exc:
            logger.warning(
                "Reading the page for its invitation credits failed: %s", exc
            )
            return None
        return embedded_credits(page_html, company_id)

    async def _dialog_state(self) -> dict[str, Any]:
        state = await self._page.evaluate(_DIALOG_STATE_JS)
        return state if isinstance(state, dict) else {"open": False}

    async def _show_more(
        self, capture: PageInviteCapture, company_id: str, start: int
    ) -> str | None:
        """Load the next page of candidates; why not, when it did not."""
        outcome = await self._page.evaluate(_SHOW_MORE_JS, list(SHOW_MORE_LABELS))
        if outcome != "clicked":
            return "more_control_not_found"
        try:
            await capture.wait_for(
                lambda r: _is_suggestions(r, company_id, start),
                timeout=_PAGE_TIMEOUT,
                wanted=(SUGGESTIONS,),
            )
        except LinkedInScraperException:
            return "page_not_loaded"
        await capture.settle()
        return None

    async def _load_until(
        self,
        capture: PageInviteCapture,
        dialog: _Dialog,
        company_id: str,
        *,
        enough: Any,
        deadline: float | None,
        most: int = MAX_CANDIDATES,
    ) -> tuple[list[Candidate], bool, str]:
        """Page through candidates until ``enough(rows)``, the end or a stop."""
        pages = 0
        while True:
            read = dialog.candidates()
            if read is None:
                raise LinkedInScraperException(
                    "LinkedIn returned the page's invite candidates in a shape this "
                    "tool does not recognise, so nothing was reported or sent."
                )
            rows, full = read
            if enough(rows):
                return rows, full, "limit"
            if not full:
                return rows, False, "complete"
            if pages >= _more_pages(most):
                return rows, True, "max_pages"
            if deadline is not None and time.monotonic() + _PAGE_TIMEOUT >= deadline:
                return rows, True, "deadline"
            # A person reads a page of rows before asking for the next.
            await asyncio.sleep(random.uniform(0.8, 2.0))
            stop = await self._show_more(capture, company_id, len(rows))
            if stop is not None:
                return rows, True, stop
            pages += 1

    async def _await_statuses(
        self,
        capture: PageInviteCapture,
        dialog: _Dialog,
        company_id: str,
        rows: list[Candidate],
        deadline: float | None,
    ) -> list[str]:
        """Wait, briefly, for the status of every row; the ids still unknown."""
        wanted = {row["profileId"] for row in rows}

        def covered(response: InviteResponse) -> bool:
            return (
                response.call.resource == STATUSES
                and _from_dialog(response, company_id)
                and wanted <= dialog.invitable().keys()
            )

        timeout = _STATUS_TIMEOUT
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic())
        if wanted and timeout > 0 and not capture.matching(covered):
            try:
                # A miss is reported per row, not read as a throttle.
                await capture.wait_for(
                    covered, timeout=timeout, throttle=False, wanted=(STATUSES,)
                )
            except LinkedInScraperException:
                pass
            await capture.settle()
        known = dialog.invitable()
        return [row["profileId"] for row in rows if row["profileId"] not in known]

    async def _search(
        self,
        capture: PageInviteCapture,
        dialog: _Dialog,
        keywords: str,
        *,
        clear: bool = False,
    ) -> list[Candidate]:
        after = len(capture.responses)
        box = self._page.locator(_SEARCH_BOX)
        await box.click()
        if clear:
            await box.fill("")
        await self._page.keyboard.type(keywords, delay=120)
        await capture.wait_for(_is_search, after=after, wanted=(TYPEAHEAD,))
        seen = len(capture.matching(_is_search, after))
        quiet_since = time.monotonic()
        while time.monotonic() - quiet_since < _SEARCH_SETTLE_SECONDS:
            await asyncio.sleep(_POLL_SECONDS)
            now_seen = len(capture.matching(_is_search, after))
            if now_seen != seen:
                seen, quiet_since = now_seen, time.monotonic()
        # The status call for the result set follows it.
        await asyncio.sleep(_SEARCH_SETTLE_SECONDS)
        await capture.settle()
        rows = dialog.search_results(after)
        if rows is None:
            raise LinkedInScraperException(
                "LinkedIn returned the page's connection search in a shape this "
                "tool does not recognise, so nothing was reported."
            )
        return rows

    async def list_follow_candidates(
        self,
        company_id: str,
        *,
        keywords: str | None = None,
        limit: int = 20,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Read the invite dialog's candidates and the page's credits."""
        limit = max(1, min(limit, MAX_CANDIDATES))
        page = self._page
        async with without_page_invites(page), PageInviteCapture(page) as capture:
            dialog = _Dialog(capture, company_id)
            await self._open(capture, company_id)
            if keywords:
                rows = (await self._search(capture, dialog, keywords))[:limit]
                more, reason = False, "search"
            else:
                rows, more, reason = await self._load_until(
                    capture,
                    dialog,
                    company_id,
                    enough=lambda found: len(found) >= limit,
                    deadline=deadline,
                )
                more = more or len(rows) > limit
                rows = rows[:limit]
            unknown = await self._await_statuses(
                capture, dialog, company_id, rows, deadline
            )
            candidates = _decorate(rows, dialog.invitable())
            credits = await self._credits(dialog, company_id)
        result: dict[str, Any] = {
            "url": invite_dialog_url(company_id),
            "company_id": company_id,
            "sections": {"candidates": "\n".join(_line(c) for c in candidates)},
            "candidates": candidates,
            "credits": credits,
            "paging": {"count": len(candidates), "more": more, "reason": reason},
        }
        if keywords:
            result["keywords"] = keywords
        errors: dict[str, Any] = {}
        if credits is None:
            errors["credits"] = {
                "error_type": "unavailable",
                "error_message": "LinkedIn did not return the page's "
                "invitation credits.",
            }
        if unknown:
            errors["invitable"] = {
                "error_type": "unavailable",
                "error_message": f"LinkedIn did not say whether {len(unknown)} of "
                f"{len(candidates)} candidates can be invited; their invitable "
                "is null. Listing again may show it.",
            }
        if errors:
            result["section_errors"] = errors
        return result

    async def invite_to_follow(
        self,
        company_id: str,
        people: list[str],
        *,
        confirm_send: bool,
        names: list[str | None] | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Invite exactly ``people`` to follow the page, or plan it on a dry run.

        Each person is any identifier ``invitee_ref`` reads, matched to a
        dialog row by member id or public identifier, never by name.
        ``names``, in the order of ``people``, is only what the dialog's own
        name search is asked for a person its candidate list does not show:
        the dialog lists about the oldest 100 connections and no more. A row
        that search returns is still taken only when its id is the person's.
        """
        refs: list[InviteeRef] = []
        for value in people:
            ref = invitee_ref(value)
            if ref is None:
                raise InvalidReferenceError(
                    f"{value!r} is not a LinkedIn profile, lead or member identifier."
                )
            refs.append(ref)
        page = self._page
        report = _Report(company_id, refs, dry_run=not confirm_send)
        async with without_page_invites(page), PageInviteCapture(page) as capture:
            dialog = _Dialog(capture, company_id)
            try:
                await self._open(capture, company_id)
            except NotPageAdminError as exc:
                return report.finish("not_page_admin", str(exc))
            plan_deadline = (
                deadline - _SEND_BUDGET_SECONDS
                if deadline is not None and confirm_send
                else deadline
            )
            rows, _more, reason = await self._load_until(
                capture,
                dialog,
                company_id,
                # A person who cannot name any row is not paged for.
                enough=lambda found: all(
                    not can_match_a_row(ref)
                    or any(ref_matches(ref, row) for row in found)
                    for ref in refs
                ),
                deadline=plan_deadline,
                most=MAX_INVITE_CANDIDATES,
            )
            named = {i: name for i, name in enumerate(names or []) if name}
            lookups = [
                index
                for index, ref in enumerate(refs)
                if index in named and _searchable(ref) and not matching_rows(ref, rows)
            ]
            searched: dict[int, tuple[str, list[Candidate]]] = {}
            for index in lookups:
                if (
                    plan_deadline is not None
                    and time.monotonic() + _SEARCH_SECONDS >= plan_deadline
                ):
                    break
                if searched:
                    # A person reads one result set before typing the next name.
                    await asyncio.sleep(random.uniform(0.8, 2.0))
                found = await self._search(
                    capture, dialog, named[index], clear=bool(searched)
                )
                searched[index] = (named[index], found)
            # Only the rows asked for need a status; the rest are not planned.
            asked = [row for row in rows if any(ref_matches(r, row) for r in refs)]
            for index, (_name, found) in searched.items():
                asked.extend(matching_rows(refs[index], found))
            await self._await_statuses(
                capture, dialog, company_id, asked, plan_deadline
            )
            report.credits = await self._credits(dialog, company_id)
            if report.credits is None:
                report.credits_error = (
                    "LinkedIn did not show the page's invitation credits."
                )
            # A send never goes out without the credits; a dry run still plans.
            if report.credits is None and confirm_send:
                return report.finish(
                    "credits_unavailable",
                    "LinkedIn did not show the page's invitation credits, so "
                    "nothing was invited.",
                )
            headroom, pacing_stop, schedule = _pacing_headroom(
                datetime.now(timezone.utc), dry_run=not confirm_send
            )
            chosen = report.plan(
                rows,
                dialog.invitable(),
                dialog.labels(),
                headroom=headroom,
                paging_stop=reason,
                lookups=set(lookups),
                searched=searched,
            )
            if pacing_stop:
                report.pacing = pacing_stop
            report.schedule = schedule
            if not confirm_send:
                if report.credits is None:
                    return report.finish(
                        "dry_run",
                        "Nothing was ticked or sent. LinkedIn did not show the "
                        "page's invitation credits, so a send would be refused "
                        "until it does; credits allowing, the people marked "
                        "would_invite are who would be invited.",
                    )
                return report.finish(
                    "dry_run",
                    "Nothing was ticked or sent. Call again with confirm_send "
                    "true to invite the people marked would_invite.",
                )
            if not chosen:
                return report.finish(
                    "nothing_to_invite", "No one asked for can be invited now."
                )
            return await self._send(capture, dialog, report, chosen, deadline)

    async def _send(
        self,
        capture: PageInviteCapture,
        dialog: _Dialog,
        report: _Report,
        chosen: list[str],
        deadline: float | None,
    ) -> dict[str, Any]:
        page = self._page
        shown = set((await self._dialog_state()).get("rows") or [])
        # The people the dialog does not show now: each is searched for again
        # by the name that found them, or their row's, before their tick.
        hidden = [pid for pid in chosen if pid not in shown]
        needed = _SEND_BUDGET_SECONDS + _SEARCH_SECONDS * len(hidden)
        if deadline is not None and deadline - time.monotonic() < needed:
            return report.finish(
                "deadline",
                "Too little of the call's time was left to tick, invite and wait "
                "for LinkedIn's answer, so nothing was ticked or sent. Call again.",
            )
        ordered = [pid for pid in chosen if pid in shown] + hidden
        for index, profile_id in enumerate(ordered):
            if index:
                await asyncio.sleep(random.uniform(0.4, 1.0))
            if profile_id in hidden:
                keywords = report.search_name(profile_id)
                if keywords is None:
                    return report.finish(
                        "selection_failed",
                        "A planned person's row is no longer shown and has no "
                        "name to search for, so nothing was sent.",
                    )
                await self._search(capture, dialog, keywords, clear=True)
            outcome = await page.evaluate(_TICK_JS, profile_id)
            if outcome != "ticked":
                return report.finish(
                    "selection_failed",
                    f"A planned person's row could not be ticked ({outcome}), so "
                    "nothing was sent.",
                )
        state = await self._dialog_state()
        checked = state.get("checked") or []
        if hidden:
            # Rows ticked under an earlier search are not shown: what is shown
            # must be planned, and Invite's own count must be the plan's.
            matches = set(checked) <= set(chosen)
        else:
            matches = set(checked) == set(chosen)
        if not matches or state.get("checkedTotal") != len(
            checked if hidden else chosen
        ):
            return report.finish(
                "selection_mismatch",
                "The dialog's selection did not match the plan, so nothing was sent.",
            )
        after = len(capture.responses)
        response: InviteResponse | None = None
        pressed = "not_found"
        async with explicit_page_invite(
            page, company_id=report.company_id, planned=chosen
        ) as window:
            try:
                # The button enables, and shows the count, on the app's
                # re-render after the ticks.
                pressed = await page.evaluate(_PRESS_INVITE_JS, len(chosen))
                for _ in range(8):
                    if pressed not in ("disabled", "count_mismatch"):
                        break
                    await asyncio.sleep(_POLL_SECONDS)
                    pressed = await page.evaluate(_PRESS_INVITE_JS, len(chosen))
                if pressed == "clicked":
                    try:
                        response = await capture.wait_for(
                            _is_send,
                            after=after,
                            timeout=_SEND_TIMEOUT,
                            throttle=False,
                        )
                    except LinkedInScraperException:
                        response = None
            except Exception:
                # The click may have run before the call failed.
                logger.warning("Pressing Invite failed", exc_info=True)
                pressed = "clicked"
            finally:
                # A press nothing was seen leaving may have sent elsewhere.
                if window.admitted or (pressed == "clicked" and not window.refused):
                    now = datetime.now(timezone.utc)
                    for _ in chosen:
                        record_write(JobStore(), PAGE_INVITES, now)
        if pressed != "clicked":
            return report.finish(
                "invite_control_not_found",
                f"The dialog's Invite button could not be pressed ({pressed}), so "
                "nothing was sent.",
            )
        if not window.admitted:
            if window.refused:
                return report.finish(
                    "blocked_by_guard",
                    "The dialog tried to send invitations that did not match the "
                    f"plan ({window.refused[0]}); they were stopped in the "
                    "browser and nothing was sent.",
                )
            # The guard aborts every other Voyager write in the window, but a
            # request outside the Voyager API would not be seen: unknown.
            return report.sent_unknown(
                chosen,
                "Invite was pressed but no invitation request was seen, so "
                "whether anyone was invited is unknown. Check the page's "
                "invitations before trying again; a repeat could invite twice.",
            )
        if response is None:
            return report.sent_unknown(
                chosen,
                "The invitations were sent but LinkedIn's answer did not arrive. "
                "Check the page's invitations before trying again; a repeat "
                "could invite twice.",
            )
        return report.answered(chosen, response)


class _Report:
    """The per-person outcome of one invite call, and the result it builds.

    Keyed by the person's position in the call, so two forms of one person
    are reported apart; ``resolved`` holds the dialog row each one matched.
    """

    def __init__(self, company_id: str, people: list[InviteeRef], *, dry_run: bool):
        self.company_id = company_id
        self.people = people
        self.dry_run = dry_run
        self.credits: Credits | None = None
        self.credits_error: str | None = None
        self.pacing: str | None = None
        self.schedule: str | None = None
        self.resolved: dict[int, Candidate] = {}
        # The name each person found only by the dialog's search was found by.
        self.found_by: dict[str, str] = {}
        self.statuses: dict[int, PersonStatus] = {}
        self.details: dict[int, str] = {}
        self.retry_safe = True

    def _index(self, profile_id: str) -> int:
        return next(
            index
            for index, row in self.resolved.items()
            if row["profileId"] == profile_id
            and self.statuses.get(index) != "duplicate"
        )

    def plan(
        self,
        rows: list[Candidate],
        invitable: dict[str, bool],
        labels: dict[str, str],
        *,
        headroom: int,
        paging_stop: str,
        lookups: set[int] | None = None,
        searched: dict[int, tuple[str, list[Candidate]]] | None = None,
    ) -> list[str]:
        """Each person's status; the ones to invite, within credits and pacing.

        A person the candidates lack is looked for among the rows their name
        search returned, by identifier alone, like any other row.
        """
        lookups = lookups or set()
        searched = searched or {}
        # None only on a dry run: a send stops before planning without credits.
        credits = self.credits["remaining"] if self.credits else None
        chosen: list[str] = []
        seen: set[str] = set()
        for index, ref in enumerate(self.people):
            matches = matching_rows(ref, rows)
            if not matches and index in searched:
                name, found = searched[index]
                matches = matching_rows(ref, found)
                if not matches:
                    self.statuses[index] = "not_invitable"
                    self.details[index] = "not_a_candidate"
                    continue
                if len(matches) == 1:
                    self.found_by[matches[0]["profileId"]] = name
            elif not matches and index in lookups:
                self.statuses[index] = "not_found"
                self.details[index] = "name_search_not_run (deadline)"
                continue
            if len(matches) > 1:
                self.statuses[index] = "not_invitable"
                self.details[index] = "ambiguous_identifier"
                continue
            if not matches:
                # A list read to its end lacks this person; a stopped one may
                # only not have reached them.
                if not can_match_a_row(ref):
                    self.statuses[index] = "not_found"
                    self.details[index] = "unmatchable_identifier"
                elif paging_stop == "complete":
                    self.statuses[index] = "not_invitable"
                    self.details[index] = "not_a_candidate"
                else:
                    self.statuses[index] = "not_found"
                    self.details[index] = (
                        f"not_in_candidates_read ({len(rows)} read, {paging_stop})"
                    )
                continue
            row = matches[0]
            self.resolved[index] = row
            profile_id = row["profileId"]
            if profile_id in seen:
                self.statuses[index] = "duplicate"
                continue
            seen.add(profile_id)
            able = invitable.get(profile_id)
            if able is None:
                # Never invited on a guess: LinkedIn may say false once it answers.
                self.statuses[index] = "invitability_unknown"
                self.details[index] = "status_not_received"
            elif not able:
                self.statuses[index] = "not_invitable"
                label = labels.get(profile_id)
                self.details[index] = (
                    f"linkedin_declined: {label}" if label else "linkedin_declined"
                )
            elif credits is not None and len(chosen) >= credits:
                self.statuses[index] = "no_credits"
            elif len(chosen) >= headroom:
                self.statuses[index] = "pacing_cap"
            else:
                self.statuses[index] = "would_invite" if self.dry_run else "not_sent"
                chosen.append(profile_id)
        return chosen

    def search_name(self, profile_id: str) -> str | None:
        """What to search for to show this person's row again."""
        if profile_id in self.found_by:
            return self.found_by[profile_id]
        return self.resolved[self._index(profile_id)].get("name")

    def sent_unknown(self, chosen: list[str], message: str) -> dict[str, Any]:
        self.retry_safe = False
        for profile_id in chosen:
            self.statuses[self._index(profile_id)] = "unconfirmed"
        return self.finish("send_unconfirmed", message)

    def answered(self, chosen: list[str], response: InviteResponse) -> dict[str, Any]:
        self.retry_safe = False
        if response.status == 429:
            for profile_id in chosen:
                index = self._index(profile_id)
                self.statuses[index] = "refused"
                self.details[index] = "rate_limited"
            return self.finish(
                "refused_by_linkedin",
                "LinkedIn refused the invitations for now (HTTP 429, rate "
                "limited). Do not retry; wait before inviting again.",
            )
        if not response.ok:
            if response.status >= 500:
                return self.sent_unknown(
                    chosen,
                    f"LinkedIn answered HTTP {response.status} to the invitations. "
                    "Check the page's invitations before trying again.",
                )
            for profile_id in chosen:
                index = self._index(profile_id)
                self.statuses[index] = "refused"
                self.details[index] = f"HTTP {response.status}"
            return self.finish(
                "refused_by_linkedin",
                f"LinkedIn refused the invitations (HTTP {response.status}).",
            )
        unconfirmed = False
        for profile_id, status in zip(
            chosen, batch_statuses(response.payload, len(chosen)), strict=True
        ):
            index = self._index(profile_id)
            if status is not None and 200 <= status < 300:
                self.statuses[index] = "invited"
            elif status is not None and 400 <= status < 500:
                self.statuses[index] = "refused"
                self.details[index] = f"HTTP {status}"
            else:
                self.statuses[index] = "unconfirmed"
                unconfirmed = True
        if unconfirmed:
            return self.finish(
                "send_unconfirmed",
                "LinkedIn answered, but not for every invitation; check the page's "
                "invitations before inviting the unconfirmed people again.",
            )
        return self.finish("sent", "LinkedIn answered for every invitation sent.")

    def finish(self, status: str, message: str) -> dict[str, Any]:
        people: list[dict[str, Any]] = []
        for index, ref in enumerate(self.people):
            entry: dict[str, Any] = {
                "input": ref.given,
                "status": self.statuses.get(index, "not_sent"),
            }
            row = self.resolved.get(index)
            if row is not None:
                entry["profileId"] = row["profileId"]
                for key in ("name", "headline", "publicIdentifier"):
                    if row.get(key):
                        entry[key] = row[key]
            if index in self.details:
                entry["detail"] = self.details[index]
            people.append(entry)
        credits: dict[str, Any] | None = None
        if self.credits is not None:
            credits = {
                **self.credits,
                "needed": sum(
                    p["status"] in ("would_invite", "invited", "unconfirmed")
                    for p in people
                ),
            }
        result: dict[str, Any] = {
            "url": invite_dialog_url(self.company_id),
            "company_id": self.company_id,
            "status": status,
            "message": message,
            "dry_run": self.dry_run,
            "retry_safe": self.retry_safe,
            "people": people,
            "credits": credits,
        }
        if self.pacing:
            result["pacing"] = self.pacing
        if self.schedule:
            result["schedule"] = self.schedule
        if self.credits_error:
            result["section_errors"] = {
                "credits": {
                    "error_type": "unavailable",
                    "error_message": self.credits_error,
                }
            }
        return result
