"""Read the rows of LinkedIn's own connections page ("Recently added").

``/mynetwork/invite-connect/connections/`` is a server-rendered (SDUI) page: its
first ten rows arrive in the document and every further ten in the stream a
``/flagship-web/rsc-action/actions/pagination`` POST returns as the list scrolls.
Neither carries a connection timestamp. Measured live on 2026-11-27: no
``createdAt`` or similar field, and the only 13-digit epochs in a pagination
stream are profile-photo URL stamps. What the page shows is one line per row,
"Connected on November 26, 2026", so a connection's date is known to the day and
no finer, and only in the time zone LinkedIn rendered it in.

The row is found by structure: one ``/in/<public id>/`` link (twice, photo and
text), one Message link whose ``profileUrn`` query value is the member's
``urn:li:fsd_profile:`` URN, and a paragraph outside both links that holds the
date. Only that paragraph's words are read as text, through an explicit en-US
table: on a LinkedIn interface in another language ``connectedOn`` is null and
the raw text is still returned.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, TypedDict

CONNECTIONS_PATH = "/mynetwork/invite-connect/connections/"
CONNECTIONS_URL = f"https://www.linkedin.com{CONNECTIONS_PATH}"
PAGINATION_PATH = "/flagship-web/rsc-action/actions/pagination"

_PROFILE_ID = re.compile(r"^AC[A-Za-z0-9_-]{8,78}$")
_PUBLIC_ID = re.compile(r"^[^/?#\s]{1,100}$")

# en-US only: the one place this module reads words. Another locale's date
# line is kept as text and its date left null.
_CONNECTED_PREFIXES = ("Connected on ",)
_MONTHS = {
    name: index
    for index, name in enumerate(
        (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        start=1,
    )
}
_DATE = re.compile(r"^([A-Z][a-z]+) (\d{1,2}), (\d{4})$")


class Connection(TypedDict):
    name: str | None
    headline: str | None
    profileId: str | None
    publicIdentifier: str
    profileUrl: str
    connectedText: str | None
    connectedOn: str | None
    connectedOnPrecision: str


# Reads every connection row on the page, in page order. A row is the widest
# element around one public id's links that still names no other public id
# and holds the Message link; the date is the text of its paragraphs that sit
# outside every link and button.
ROWS_JS = r"""() => {
  /* linkedin-mcp:recent-connections-rows */
  const root = document.querySelector('main') || document.body;
  const slug = (href) => {
    const m = /\/in\/([^/?#]+)/.exec(href || '');
    return m ? m[1] : null;
  };
  const profileId = (href) => {
    try {
      const value = new URL(href, location.href).searchParams.get('profileUrn') || '';
      const m = /^urn:li:fsd_profile:([A-Za-z0-9_-]+)$/.exec(value);
      return m ? m[1] : null;
    } catch (e) {
      return null;
    }
  };
  const rows = [];
  const seen = new Set();
  for (const anchor of root.querySelectorAll('a[href*="/in/"]')) {
    const id = slug(anchor.getAttribute('href'));
    if (!id || seen.has(id)) continue;
    let el = anchor.parentElement;
    let row = null;
    while (el && el !== root) {
      const ids = new Set(
        [...el.querySelectorAll('a[href*="/in/"]')].map(a => slug(a.getAttribute('href')))
      );
      if (ids.size > 1) break;
      if (el.querySelector('a[href*="profileUrn="]')) row = el;
      el = el.parentElement;
    }
    if (!row) continue;
    seen.add(id);
    const profileIds = [...new Set(
      [...row.querySelectorAll('a[href*="profileUrn="]')]
        .map(a => profileId(a.getAttribute('href')))
        .filter(Boolean)
    )];
    const label = [...row.querySelectorAll('a[href*="/in/"]')]
      .map(a => (a.innerText || '').trim())
      .find(Boolean) || '';
    const loose = [...row.querySelectorAll('p')]
      .filter(p => !p.closest('a, button'))
      .map(p => (p.innerText || '').trim())
      .filter(Boolean);
    rows.push({publicIdentifier: id, profileIds, label, loose});
  }
  return rows;
}"""


def connected_on(text: str | None) -> date | None:
    """The date in an en-US "Connected on <Month> <D>, <YYYY>" line, else None."""
    if not text:
        return None
    text = " ".join(text.split())
    for prefix in _CONNECTED_PREFIXES:
        if text.startswith(prefix):
            match = _DATE.fullmatch(text[len(prefix) :])
            month = _MONTHS.get(match.group(1)) if match else None
            if match is None or month is None:
                return None
            try:
                return date(int(match.group(3)), month, int(match.group(2)))
            except ValueError:
                return None
    return None


def _lines(text: Any) -> list[str]:
    if not isinstance(text, str):
        return []
    return [" ".join(line.split()) for line in text.splitlines() if line.strip()]


def connection_from_row(raw: Any) -> Connection | None:
    """One row as returned; None when it does not carry a usable public id."""
    if not isinstance(raw, dict):
        return None
    public = raw.get("publicIdentifier")
    if not isinstance(public, str) or not _PUBLIC_ID.fullmatch(public):
        return None
    ids = [
        value
        for value in raw.get("profileIds") or []
        if isinstance(value, str) and _PROFILE_ID.fullmatch(value)
    ]
    label = _lines(raw.get("label"))
    loose = [line for item in raw.get("loose") or [] for line in _lines(item)]
    text = loose[0] if loose else None
    on = connected_on(text)
    return {
        "name": label[0] if label else None,
        "headline": " ".join(label[1:]) or None,
        # Two different ids in one row cannot be told apart: neither is given.
        "profileId": ids[0] if len(ids) == 1 else None,
        "publicIdentifier": public,
        "profileUrl": f"https://www.linkedin.com/in/{public}/",
        "connectedText": text,
        "connectedOn": on.isoformat() if on else None,
        "connectedOnPrecision": "day",
    }


def connection_line(connection: Connection) -> str:
    """The row as one line of the ``connections`` section text."""
    parts = [connection["name"] or connection["publicIdentifier"]]
    if connection["headline"]:
        parts.append(connection["headline"])
    parts.append(connection["connectedText"] or "connected date not shown")
    return " | ".join(parts)
