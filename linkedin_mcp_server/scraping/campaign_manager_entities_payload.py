"""Pure payload shapes and builders for Campaign Manager's write support.

Parsers return ``None`` on a wrong shape and never raise; ``[]`` is a valid
empty page. Display text (``name``, ``description``, non-sponsorable reasons)
is whatever language the account uses: ids and URNs carry identity.

The one builder here, :func:`company_list_csv`, produces the file the company
list upload takes. It raises ``InvalidReferenceError`` for input it refuses.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal, NotRequired, TypedDict

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.scraping.campaign_manager_payload import (
    envelope_elements,
    included_by_urn,
)
from linkedin_mcp_server.scraping.identifiers import (
    normalize_company_page_url,
    normalize_website,
)

# Column names of LinkedIn's public account-match template, in its order
# (LinkedIn_Ads_Account_Match_Template.csv, read 2026-11-17).
ACCOUNT_TEMPLATE_HEADERS = (
    "companyname",
    "companywebsite",
    "companyemaildomain",
    "linkedincompanypageurl",
    "stocksymbol",
    "industry",
    "city",
    "state",
    "companycountry",
    "zipcode",
)
_NAME_HEADER = "companyname"
_WEBSITE_HEADER = "companywebsite"
_PAGE_HEADER = "linkedincompanypageurl"

# Our bound. LinkedIn accepts 300 000 rows, but one tool call should not queue
# an upload that large.
MAX_COMPANY_ROWS = 10_000
MAX_CSV_BYTES = 20 * 1024 * 1024

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_FORMULA_PREFIXES = ("=", "+", "-", "@")


class TargetingEntity(TypedDict):
    urn: str
    name: str
    facetUrn: str
    description: NotRequired[str]
    targetable: bool


class SponsorableContent(TypedDict):
    urn: str
    sponsorable: bool
    nonSponsorableReasons: list[str]
    visibility: str
    contentType: NotRequired[str]
    lifecycleState: NotRequired[str]


class MatchedAudience(TypedDict):
    """An uploaded or derived audience.

    ``segmentUrn`` is built as ``urn:li:adSegment:<id>`` from the bundle's
    segment naming; no account had a matched audience to confirm it live, so
    treat it as unverified until a typeahead row returns the same URN.
    """

    kind: Literal["matched_audience"]
    id: int
    segmentUrn: str
    name: str
    listType: NotRequired[str]
    source: NotRequired[str]
    status: NotRequired[str]
    audienceCount: NotRequired[int]


class SavedAudience(TypedDict):
    kind: Literal["saved_audience"]
    id: int
    name: str
    description: NotRequired[str]
    audienceCount: NotRequired[int]
    lastModifiedAt: NotRequired[int]
    lastAppliedAt: NotRequired[int]


class CompanyRow(TypedDict):
    name: str
    linkedin_url: NotRequired[str]
    website: NotRequired[str]


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def typeahead_entities(payload: Any) -> list[TargetingEntity] | None:
    """Rows of a targeting typeahead answer, in either envelope.

    A row without ``targetable`` is reported as not targetable, so a caller
    never selects what the app has not declared selectable.
    """
    elements = envelope_elements(payload)
    if elements is None:
        return None
    entities: list[TargetingEntity] = []
    for element in elements:
        urn = _str(element.get("urn"))
        name = _str(element.get("name"))
        facet = _str(element.get("facetUrn"))
        if urn is None or name is None or facet is None:
            return None
        entity: TargetingEntity = {
            "urn": urn,
            "name": name,
            "facetUrn": facet,
            "targetable": element.get("targetable") is True,
        }
        description = _str(element.get("description")) or _str(element.get("subLine"))
        if description is not None:
            entity["description"] = description
        entities.append(entity)
    return entities


def content_library_entries(payload: Any) -> list[SponsorableContent] | None:
    """Entries of a content library answer, in the order the app lists them.

    Each element's ``content["*sponsoredUpdateContent"]`` URN is resolved
    against ``included``. ``sponsorable`` is the server's verdict only: the app
    also applies client-side checks (landing page, objective) that no payload
    carries.
    """
    elements = envelope_elements(payload)
    if elements is None:
        return None
    included = included_by_urn(payload)
    entries: list[SponsorableContent] = []
    for element in elements:
        content = element.get("content")
        ref = (
            content.get("*sponsoredUpdateContent")
            if isinstance(content, dict)
            else None
        )
        if not isinstance(ref, str) or ref not in included:
            return None
        entity = included[ref]
        sponsorable = entity.get("sponsorable")
        reasons = entity.get("nonSponsorableReasons", [])
        visibility = _str(entity.get("visibility"))
        if (
            not isinstance(sponsorable, bool)
            or visibility is None
            or not isinstance(reasons, list)
            or not all(isinstance(reason, str) for reason in reasons)
        ):
            return None
        entry: SponsorableContent = {
            "urn": ref,
            "sponsorable": sponsorable,
            "nonSponsorableReasons": [r for r in reasons if isinstance(r, str)],
            "visibility": visibility,
        }
        generated = entity.get("userGeneratedAdContent")
        content_type = (
            _str(generated.get("contentType")) if isinstance(generated, dict) else None
        )
        if content_type is not None:
            entry["contentType"] = content_type
        state = entity.get("state")
        lifecycle = (
            _str(state.get("lifecycleState")) if isinstance(state, dict) else None
        )
        if lifecycle is not None:
            entry["lifecycleState"] = lifecycle
        entries.append(entry)
    return entries


def matched_audiences(payload: Any) -> list[MatchedAudience] | None:
    """Matched audiences of ``campaignManagerMatchedAudiences?q=account``."""
    elements = envelope_elements(payload)
    if elements is None:
        return None
    audiences: list[MatchedAudience] = []
    for element in elements:
        audience_id = _int(element.get("id"))
        name = _str(element.get("name"))
        if audience_id is None or name is None:
            return None
        audience: MatchedAudience = {
            "kind": "matched_audience",
            "id": audience_id,
            "segmentUrn": f"urn:li:adSegment:{audience_id}",
            "name": name,
        }
        for key, field in (
            ("segmentType", "listType"),
            ("maSegmentType", "source"),
            ("maSegmentStatus", "status"),
        ):
            text = _str(element.get(key))
            if text is not None:
                audience[field] = text  # type: ignore[literal-required]
        count = _int(element.get("audienceCount"))
        if count is not None:
            audience["audienceCount"] = count
        audiences.append(audience)
    return audiences


def saved_audiences(payload: Any) -> list[SavedAudience] | None:
    """The account's own saved audiences; LinkedIn's templates are skipped."""
    elements = envelope_elements(payload)
    if elements is None:
        return None
    audiences: list[SavedAudience] = []
    for element in elements:
        if element.get("type") != "USER_AUDIENCE":
            continue
        audience_id = _int(element.get("id"))
        name = _str(element.get("name"))
        if audience_id is None or name is None:
            return None
        audience: SavedAudience = {
            "kind": "saved_audience",
            "id": audience_id,
            "name": name,
        }
        description = _str(element.get("description"))
        if description is not None:
            audience["description"] = description
        for key in ("audienceCount", "lastModifiedAt", "lastAppliedAt"):
            number = _int(element.get(key))
            if number is not None:
                audience[key] = number  # type: ignore[literal-required]
        audiences.append(audience)
    return audiences


def _line(head: str, *details: str | None) -> str:
    text = ", ".join(detail for detail in details if detail)
    return f"{head}: {text}" if text else head


def render_audiences(audiences: Iterable[MatchedAudience | SavedAudience]) -> str:
    lines = []
    for audience in audiences:
        count = audience.get("audienceCount")
        size = f"{count} members" if count is not None else None
        head = f"{audience['name']} (id {audience['id']})"
        if audience["kind"] == "matched_audience":
            lines.append(
                _line(
                    head,
                    audience.get("listType"),
                    audience.get("source"),
                    audience.get("status"),
                    size,
                    audience["segmentUrn"],
                )
            )
        else:
            lines.append(_line(head, audience.get("description"), size))
    return "\n".join(lines)


def render_entities(entities: Iterable[TargetingEntity]) -> str:
    return "\n".join(
        _line(
            f"{entity['name']} ({entity['urn']})",
            entity.get("description"),
            None if entity["targetable"] else "not targetable",
        )
        for entity in entities
    )


def _cell(value: str) -> str:
    return "'" + value if value.startswith(_FORMULA_PREFIXES) else value


def _clean(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidReferenceError(f"{field} must be text.")
    value = value.strip()
    if _CONTROL.search(value):
        raise InvalidReferenceError(f"{field} contains a control character.")
    return value


def company_list_csv(
    rows: Sequence[Mapping[str, Any]], headers: tuple[str, ...]
) -> str:
    """The company list upload file: ``headers`` first, then one row per company.

    Only the name, website and LinkedIn page columns are filled. A row with just
    a name is valid. Rows repeating a (name, LinkedIn page) pair are dropped,
    keeping the first. Any cell starting with ``=``, ``+``, ``-`` or ``@`` is
    prefixed with ``'`` so a spreadsheet never reads it as a formula.
    """
    if _NAME_HEADER not in headers:
        raise InvalidReferenceError("The company list template has no name column.")
    if not rows:
        raise InvalidReferenceError("companies is empty. Pass at least one company.")
    if len(rows) > MAX_COMPANY_ROWS:
        raise InvalidReferenceError(
            f"companies has {len(rows)} rows; at most {MAX_COMPANY_ROWS} per upload."
        )
    seen: set[tuple[str, str]] = set()
    body: list[dict[str, str]] = []
    for position, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            raise InvalidReferenceError(f"companies[{position}] must be an object.")
        name = _clean(row.get("name"), f"companies[{position}].name")
        if not name:
            raise InvalidReferenceError(f"companies[{position}].name is empty.")
        page = _clean(
            row.get("linkedin_url") or "", f"companies[{position}].linkedin_url"
        )
        website = _clean(row.get("website") or "", f"companies[{position}].website")
        page = normalize_company_page_url(page) if page else ""
        website = normalize_website(website) if website else ""
        key = (name.casefold(), page)
        if key in seen:
            continue
        seen.add(key)
        body.append({_NAME_HEADER: name, _PAGE_HEADER: page, _WEBSITE_HEADER: website})
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(headers)
    for entry in body:
        writer.writerow([_cell(entry.get(header, "")) for header in headers])
    text = buffer.getvalue()
    if len(text.encode("utf-8")) >= MAX_CSV_BYTES:
        raise InvalidReferenceError("The company list is too large to upload (20 MB).")
    return text
