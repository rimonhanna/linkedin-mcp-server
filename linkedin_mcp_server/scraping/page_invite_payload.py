"""Pure readers and builders for a company page's "Invite to follow" dialog.

The dialog (``/company/<id>/admin/dashboard/?invite=true``) is fed by three
Voyager REST resources and one GraphQL typeahead, all GETs the app makes on its
own; the invitation itself is one Rest.li ``batch_create`` POST. Measured
2026-11-24 on a page the account administers:

* ``voyagerRelationshipsDashCommunityInviteeSuggestions`` (``q=community``,
  ``useCase=INVITEE_PICKER``): 20 candidates on open, then 10 per "Show more
  results"; ``paging`` has no total. Each element names a
  ``urn:li:fsd_profile:`` and ``included`` holds its Profile (first and last
  name, headline, public identifier).
* ``voyagerRelationshipsDashCommunityInviteeSuggestionStatus``
  (``q=communityAndInvitees``): ``ableToBeInvited`` per profile, after every
  page of candidates and every search.
* ``voyagerRelationshipsDashCommunityInviterStatistics``: the page's
  invitation credits (``remainingCredit``, ``initialCreditGrant``,
  ``nextCreditGrantOn``), shared by every admin and granted monthly. On a
  direct load of the dialog URL (measured 2026-11-25) no request is made for
  it: the server renders it into the page as a datalet, a
  ``<code id="datalet-bpr-guid-N">`` naming the request and a
  ``<code id="bpr-guid-N">`` holding its body.
* GraphQL ``voyagerSearchDashReusableTypeahead`` (``type:CONNECTIONS``): the
  name search; one result set of up to 40, name in ``title.text``.

The dialog asks for every resource with
``community=urn:li:fs_normalized_company:<id>``; the dashboard's own invite
card behind it asks for five candidates under ``urn:li:fsd_company:<id>``, and
those must not be mixed in. Everything here returns ``None`` on a wrong shape
and never raises.
"""

from __future__ import annotations

import base64
import binascii
import html
import json
import re
from dataclasses import dataclass
from typing import Any, TypedDict
from urllib.parse import parse_qsl, unquote, urlparse

from linkedin_mcp_server.scraping.restli import (
    as_dicts,
    as_int,
    as_mapping,
    as_str,
    envelope_elements,
    envelope_paging,
    included_by_urn,
)

VOYAGER_API = "/voyager/api/"
SUGGESTIONS = "voyagerRelationshipsDashCommunityInviteeSuggestions"
STATUSES = "voyagerRelationshipsDashCommunityInviteeSuggestionStatus"
CREDITS = "voyagerRelationshipsDashCommunityInviterStatistics"
INVITATIONS = "voyagerRelationshipsDashInvitations"
TYPEAHEAD = "voyagerSearchDashReusableTypeahead"
_GRAPHQL = "graphql"
_RESOURCES = frozenset({SUGGESTIONS, STATUSES, CREDITS, INVITATIONS})
PROFILE_URN = "urn:li:fsd_profile:"
# What the app sends as each invitation's type for a company page.
ORGANIZATION_INVITATION = "ORGANIZATION"
_PROFILE_ID = re.compile(r"^[A-Za-z0-9_-]{6,80}$")
_TYPEAHEAD_CONNECTIONS = "type:CONNECTIONS"


@dataclass(frozen=True, slots=True)
class InviteCall:
    resource: str
    method: str
    params: dict[str, str]


class Candidate(TypedDict, total=False):
    profileId: str
    name: str
    headline: str
    publicIdentifier: str
    profileUrl: str
    invitable: bool | None


class Credits(TypedDict):
    remaining: int
    total: int | None
    nextGrantOn: str | None


def dialog_community(company_id: str) -> str:
    return f"urn:li:fs_normalized_company:{company_id}"


def invite_dialog_url(company_id: str) -> str:
    return f"https://www.linkedin.com/company/{company_id}/admin/dashboard/?invite=true"


def is_profile_id(value: str) -> bool:
    return bool(_PROFILE_ID.fullmatch(value))


def profile_url(candidate: Candidate) -> str:
    slug = candidate.get("publicIdentifier") or candidate["profileId"]
    return f"https://www.linkedin.com/in/{slug}/"


def parse_invite_call(url: str, method: str) -> InviteCall | None:
    """The call a Voyager response answers, when it is one the dialog needs."""
    parsed = urlparse(url)
    if not parsed.path.startswith(VOYAGER_API):
        return None
    resource = parsed.path[len(VOYAGER_API) :].split("/", 1)[0]
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if resource == _GRAPHQL:
        query_id = params.get("queryId", "")
        if not query_id.startswith(TYPEAHEAD + "."):
            return None
        if _TYPEAHEAD_CONNECTIONS not in params.get("variables", ""):
            return None
        resource = TYPEAHEAD
    elif resource not in _RESOURCES:
        return None
    return InviteCall(resource, method.upper(), params)


def _profile_id(urn: Any) -> str | None:
    value = as_str(urn)
    if value is None or not value.startswith(PROFILE_URN):
        return None
    identifier = value[len(PROFILE_URN) :]
    return identifier if is_profile_id(identifier) else None


def _full_name(profile: dict[str, Any]) -> str | None:
    parts = [as_str(profile.get(key)) for key in ("firstName", "lastName")]
    name = " ".join(part.strip() for part in parts if part and part.strip())
    return name or None


def suggestion_candidates(payload: Any) -> list[Candidate] | None:
    """The candidates of one suggestions page, in the dialog's order."""
    elements = envelope_elements(payload)
    if elements is None:
        return None
    profiles = included_by_urn(payload)
    candidates: list[Candidate] = []
    for element in elements:
        urn = element.get("suggestedProfile")
        profile_id = _profile_id(urn)
        if profile_id is None:
            return None
        profile = profiles.get(str(urn), {})
        candidate: Candidate = {"profileId": profile_id}
        name = _full_name(profile)
        if name:
            candidate["name"] = name
        headline = as_str(profile.get("headline"))
        if headline:
            candidate["headline"] = headline
        public = as_str(profile.get("publicIdentifier"))
        if public:
            candidate["publicIdentifier"] = public
        candidates.append(candidate)
    return candidates


def suggestion_page(payload: Any) -> tuple[int, int] | None:
    """``(start, count)`` the suggestions page reports."""
    paging = envelope_paging(payload)
    if paging is None:
        return None
    start, count = as_int(paging.get("start")), as_int(paging.get("count"))
    if start is None or count is None:
        return None
    return start, count


def typeahead_candidates(payload: Any) -> list[Candidate] | None:
    """The connections a name search returned, in the dialog's order."""
    record = as_mapping(payload)
    data = as_mapping(as_mapping(record.get("data")) if record else None)
    data = as_mapping(data.get("data")) if data is not None else None
    if data is None:
        return None
    roots = [as_mapping(value) for key, value in data.items() if key[:1] != "$"]
    if len(roots) != 1 or roots[0] is None:
        return None
    elements = roots[0].get("elements")
    if not isinstance(elements, list):
        return None
    candidates: list[Candidate] = []
    for element in as_dicts(elements):
        target = as_mapping(element.get("target")) or {}
        profile_id = _profile_id(target.get("*profile"))
        if profile_id is None:
            return None
        candidate: Candidate = {"profileId": profile_id}
        name = as_str((as_mapping(element.get("title")) or {}).get("text"))
        if name:
            candidate["name"] = name
        headline = as_str((as_mapping(element.get("subtitle")) or {}).get("text"))
        if headline:
            candidate["headline"] = headline
        candidates.append(candidate)
    return candidates


def invitable_by_profile(payload: Any) -> dict[str, bool] | None:
    """``ableToBeInvited`` per profile id, from one status response."""
    elements = envelope_elements(payload)
    if elements is None:
        return None
    statuses: dict[str, bool] = {}
    for element in elements:
        profile_id = _profile_id(element.get("suggestedProfile"))
        able = element.get("ableToBeInvited")
        if profile_id is None or not isinstance(able, bool):
            return None
        statuses[profile_id] = able
    return statuses


def status_labels(payload: Any) -> dict[str, str]:
    """The ``statusLabel`` LinkedIn gives a candidate, by profile id.

    Display text in the member's language, reported as given, never decided on.
    """
    labels: dict[str, str] = {}
    for element in envelope_elements(payload) or []:
        profile_id = _profile_id(element.get("suggestedProfile"))
        label = element.get("statusLabel")
        if isinstance(label, dict):
            label = label.get("text")
        if profile_id and isinstance(label, str) and label.strip():
            labels[profile_id] = label.strip()
    return labels


def page_credits(payload: Any) -> Credits | None:
    """The page's invitation credits, from the inviter statistics."""
    elements = envelope_elements(payload)
    if not elements or len(elements) != 1:
        return None
    element = elements[0]
    remaining = as_int(element.get("remainingCredit"))
    if remaining is None:
        return None
    grant = as_mapping(element.get("nextCreditGrantOn")) or {}
    year, month, day = (as_int(grant.get(key)) for key in ("year", "month", "day"))
    next_grant = (
        f"{year:04d}-{month:02d}-{day:02d}"
        if year is not None and month is not None and day is not None
        else None
    )
    return {
        "remaining": max(remaining, 0),
        "total": as_int(element.get("initialCreditGrant")),
        "nextGrantOn": next_grant,
    }


_CODE_BLOCK = re.compile(
    r'<code\b[^>]*\bid="((?:datalet-)?bpr-guid-[0-9]+)"[^>]*>(.*?)</code>', re.S
)


def _json(text: str | None) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def embedded_credits(page_html: str, company_id: str) -> Credits | None:
    """The page's credits from the statistics rendered into its HTML.

    Only a datalet for this page's dialog community counts.
    """
    blocks = {
        match.group(1): html.unescape(match.group(2))
        for match in _CODE_BLOCK.finditer(page_html)
    }
    found: Credits | None = None
    for block_id, text in blocks.items():
        if not block_id.startswith("datalet-"):
            continue
        datalet = as_mapping(_json(text))
        if datalet is None or datalet.get("status") != 200:
            continue
        request = as_str(datalet.get("request"))
        method = as_str(datalet.get("method"))
        body = as_str(datalet.get("body"))
        if request is None or method is None or body is None:
            continue
        call = parse_invite_call(request, method)
        if (
            call is None
            or call.resource != CREDITS
            or call.method != "GET"
            or call.params.get("community") != dialog_community(company_id)
        ):
            continue
        credits = page_credits(_json(blocks.get(body)))
        if credits is not None:
            found = credits
    return found


def invitation_batch(body: Any) -> list[str] | None:
    """Profile ids of a ``batch_create`` body that invites to follow a page.

    ``None`` when any element is something else (another invitation type, a
    message, an email invitee): the guard admits only what this can read.
    """
    record = as_mapping(body)
    elements = record.get("elements") if record is not None else None
    if not isinstance(elements, list) or not elements:
        return None
    allowed = {"inviteeMember", "genericInvitationType", "trackingId"}
    profile_ids: list[str] = []
    for element in elements:
        element = as_mapping(element)
        if element is None or set(element) - allowed:
            return None
        if element.get("genericInvitationType") != ORGANIZATION_INVITATION:
            return None
        profile_id = _profile_id(element.get("inviteeMember"))
        if profile_id is None:
            return None
        profile_ids.append(profile_id)
    return profile_ids


def batch_statuses(payload: Any, count: int) -> list[int | None]:
    """Each element's HTTP status in a ``batch_create`` answer, request order.

    Rest.li answers a batch create with one ``{status}`` per element, in the
    order they were sent. Unreadable or missing entries are ``None``: the
    caller cannot say whether that invitation exists.
    """
    elements = envelope_elements(payload)
    if elements is None or len(elements) != count:
        return [None] * count
    return [as_int(element.get("status")) for element in elements]


# The URN kinds that name a company page. A member or profile URN never names
# the page, whatever its id.
_PAGE_URN_KINDS = "fsd_company|fs_normalized_company|company|organization"


def inviter_allowed(url: str, company_id: str) -> bool:
    """Whether a POST's ``inviter`` is absent or names this page alone.

    Measured 2026-11-26: the page dialog sends
    ``?inviter=(organizationUrn:urn%3Ali%3Afsd_company%3A<id>)``, a Rest.li
    record with one field, and no other query key. That record, or a bare
    page URN, is admitted only when it names this page's id; any other query
    key, or any other inviter, is refused.
    """
    query = parse_qsl(urlparse(url).query, keep_blank_values=True)
    if any(key != "inviter" for key, _value in query):
        return False
    values = [unquote(value) for key, value in query if key == "inviter"]
    if not values:
        return True
    page = rf"urn:li:(?:{_PAGE_URN_KINDS}):{re.escape(company_id)}"
    return len(values) == 1 and bool(
        re.fullmatch(rf"{page}|\(organizationUrn:{page}\)", values[0])
    )


# LinkedIn's opaque member ids (``ACoA...`` in ``fsd_profile`` URNs, ``ACwA...``
# in Sales Navigator ``fs_salesProfile`` URNs) read as base64url of a 4-byte
# header (``00 2a 00 00`` / ``00 2c 00 00``), the member's numeric id as 4
# bytes, then bytes of their own. Inferred 2026-11-24 from the dialog's ids,
# not yet checked against a known member id (``/voyager/api/me``'s ``plainId``,
# a Sales Navigator lead's ``objectUrn``); that live check is listed in the PR.
# The numeric id is what every form shares, so it is what Sales Navigator and
# member forms are matched by; an ``ACoA`` id given as such is also matched
# exactly, so it never depends on the inference.
_ENCODED_ID = re.compile(r"^AC[A-Za-z0-9_-]{8,78}$")
_MEMBER_ID = re.compile(r"^[0-9]{1,12}$")
_PROFILE_URNS = ("urn:li:fsd_profile:", "urn:li:fs_profile:", "urn:li:fs_miniProfile:")
_SALES_PROFILE_URN = re.compile(r"^urn:li:fs_salesProfile:\(([^,()]+)(?:,[^()]*)?\)$")
_VANITY = re.compile(r"^[A-Za-z0-9\-_%.\u00c0-\uffff]{3,100}$")


def member_id_of(encoded: str) -> str | None:
    """The numeric member id inside an ``ACoA``/``ACwA`` id, when it decodes."""
    if not _ENCODED_ID.fullmatch(encoded):
        return None
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (binascii.Error, ValueError):
        return None
    if len(raw) < 8 or raw[0] != 0 or raw[2] != 0 or raw[3] != 0:
        return None
    member = int.from_bytes(raw[4:8], "big")
    # No member is 0: an id that decodes to it is not one this reading explains.
    return str(member) if member else None


@dataclass(frozen=True, slots=True)
class InviteeRef:
    """One person as the caller named them, reduced to what can be matched.

    ``kind`` is ``member`` (numeric member id, from any member URN or encoded
    id that decodes), ``profile`` (an encoded id that does not decode, matched
    exactly) or ``vanity`` (a public profile name, matched against the
    candidate's public identifier). Never a display name.
    """

    given: str
    kind: str
    key: str
    # The ``ACoA`` id itself when the caller gave one, matched exactly first.
    exact: str | None = None


def _encoded_ref(given: str, encoded: str) -> InviteeRef | None:
    if not _ENCODED_ID.fullmatch(encoded):
        return None
    member = member_id_of(encoded)
    exact = encoded if encoded.startswith("ACoA") else None
    if member is not None:
        return InviteeRef(given, "member", member, exact)
    return InviteeRef(given, "profile", encoded, exact)


def _path_ref(given: str, path: str) -> InviteeRef | None:
    segments = [unquote(part) for part in path.split("/") if part]
    if len(segments) >= 2 and segments[0].lower() == "in":
        return _segment_ref(given, segments[1])
    if (
        len(segments) >= 3
        and segments[0].lower() == "sales"
        and segments[1].lower()
        in (
            "lead",
            "people",
        )
    ):
        return _encoded_ref(given, segments[2].split(",", 1)[0])
    return None


def _segment_ref(given: str, value: str) -> InviteeRef | None:
    encoded = _encoded_ref(given, value)
    if encoded is not None:
        return encoded
    if _VANITY.fullmatch(value) and not value.startswith("."):
        return InviteeRef(given, "vanity", value.lower())
    return None


def invitee_ref(value: str) -> InviteeRef | None:
    """Read a person from any form the other tools return; None when unreadable.

    Accepted: an ``ACoA``/``ACwA`` id, a ``urn:li:fsd_profile:`` (or
    ``fs_profile``/``fs_miniProfile``) URN, a Sales Navigator lead URN
    ``urn:li:fs_salesProfile:(<id>,...)``, a ``urn:li:member:<n>`` URN, a
    ``/in/<name or id>/`` or ``/sales/lead/<id>,...`` URL, or a public profile
    name.
    """
    given = value
    value = value.strip()
    if not value:
        return None
    if value.startswith("urn:li:member:"):
        member = value[len("urn:li:member:") :]
        return (
            InviteeRef(given, "member", member)
            if _MEMBER_ID.fullmatch(member)
            else None
        )
    for prefix in _PROFILE_URNS:
        if value.startswith(prefix):
            return _encoded_ref(given, value[len(prefix) :])
    sales = _SALES_PROFILE_URN.fullmatch(value)
    if sales:
        return _encoded_ref(given, sales.group(1))
    if value.startswith("urn:"):
        return None
    if "/" in value:
        parsed = urlparse(value if "://" in value else "https://" + value.lstrip("/"))
        host = (parsed.hostname or "").lower()
        if host and host != "linkedin.com" and not host.endswith(".linkedin.com"):
            return None
        return _path_ref(given, parsed.path)
    return _segment_ref(given, value)


def ref_matches(ref: InviteeRef, candidate: Candidate) -> bool:
    """Whether ``candidate`` is the person ``ref`` names, by identifier only."""
    if ref.exact is not None and candidate["profileId"] == ref.exact:
        return True
    if ref.kind == "member":
        return member_id_of(candidate["profileId"]) == ref.key
    if ref.kind == "profile":
        return candidate["profileId"] == ref.key
    public = candidate.get("publicIdentifier")
    return public is not None and unquote(public).lower() == ref.key


def can_match_a_row(ref: InviteeRef) -> bool:
    """Whether ``ref`` could name any dialog row at all.

    Dialog rows are ``fsd_profile`` (``ACoA``) ids, so an id kept as given
    because it does not decode (a Sales Navigator ``ACwA`` id) matches none.
    """
    return ref.kind != "profile" or ref.key.startswith("ACoA")


def matching_rows(ref: InviteeRef, rows: list[Candidate]) -> list[Candidate]:
    """Every row ``ref`` names: the row with its exact ``ACoA`` id when one has
    it, else every row it matches. More than one means the identifier is
    ambiguous and must not select anyone.
    """
    if ref.exact is not None:
        exact = [row for row in rows if row["profileId"] == ref.exact]
        if exact:
            return exact
    return [row for row in rows if ref_matches(ref, row)]
