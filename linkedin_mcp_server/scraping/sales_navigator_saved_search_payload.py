"""The saved-search requests Sales Navigator sends, and how its answers read.

Measured 2026-11-27 on the lead and account search pages. Turning on the
results page's "Save search" switch makes the app POST
``salesApiSavedSearchesV2`` at once, with a name it generates from the
filters (no form), the member's seat, the search as JSON with display labels,
``useCase`` ``SAVED_LEAD_SEARCH`` or ``SAVED_ACCOUNT_SEARCH`` and
``frequency`` ``DAILY``; it answers 201 with ``x-restli-id``
``{seat=urn:li:seat:<n>, id=<n>}``. Naming it is a second write, from the
Saved searches panel's Edit form: POST
``salesApiSavedSearchesV2/(seat:<urn, encoded>,id:<n>)`` with
``{"patch": {"$set": {"name": ...}}}``, answered 204. The panel reads the
searches with ``GET salesApiSavedSearchesV2?q=savedPeopleSearches`` (or
``savedCompanySearches``), each element carrying ``id``, ``name`` and its
``filters`` as ``singleFilterMetadata`` with ids and display values.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal
from urllib.parse import quote

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.scraping.identifiers import normalize_sales_company_id
from linkedin_mcp_server.scraping.sales_navigator_list_guard import PlannedListWrite
from linkedin_mcp_server.scraping.sales_navigator_payload import SearchFilter

SAVE_TOOL = "save_sales_navigator_search"
SAVED_SEARCHES = "salesApiSavedSearchesV2"
SAVED_SEARCHES_URL = "https://www.linkedin.com/sales/search/saved-searches"
# Which search page, request query and saved-search finder each kind uses.
KINDS: Mapping[str, tuple[Literal["company", "people"], str, str, str]] = {
    "leads": ("people", "leadSearchQuery", "SAVED_LEAD_SEARCH", "savedPeopleSearches"),
    "accounts": (
        "company",
        "accountSearchQuery",
        "SAVED_ACCOUNT_SEARCH",
        "savedCompanySearches",
    ),
}
# The panel's name field takes 256 characters (its maxlength).
MAX_NAME_LENGTH = 256

_SHARED = ("keywords", "industry", "exclude_industry")
_LEAD_ONLY = (
    "title",
    "seniority",
    "current_company",
    "exclude_current_company",
    "location",
    "company_hq_location",
    "network",
    "function",
    "company_size",
    "years_in_current_role",
    "years_at_current_company",
    "account_list",
    "lead_list",
    "exclude_lead_list",
)
_ACCOUNT_ONLY = (
    "hq_location",
    "size",
    "headcount_growth_min",
    "headcount_growth_max",
    "department_headcount",
)
_PARAMETERS = {"leads": _SHARED + _LEAD_ONLY, "accounts": _SHARED + _ACCOUNT_ONLY}
_RESTLI_KEY = re.compile(r"^\{seat=(urn:li:seat:\d+), id=(\d+)\}$")


def _given(value: Any) -> bool:
    return value is not None and value != [] and value != ""


def search_criteria(
    kind: str, given: Mapping[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """The criteria for ``kind``'s search, or why the call is refused."""
    allowed = _PARAMETERS.get(kind)
    if allowed is None:
        return {}, f"kind must be one of {sorted(_PARAMETERS)!r}."
    foreign = sorted(k for k, v in given.items() if _given(v) and k not in allowed)
    if foreign:
        other = "accounts" if kind == "leads" else "leads"
        return {}, (
            f"{', '.join(foreign)} {'is a filter' if len(foreign) == 1 else 'are filters'}"
            f" of the {other} search, not of the {kind} search."
        )
    criteria = {k: v for k, v in given.items() if k in allowed and _given(v)}
    if not criteria:
        return {}, "Pass keywords or at least one filter."
    return criteria, None


def _company_lookups(value: str) -> int:
    """Page loads to resolve a company: none for an id, About for a URL, else two."""
    try:
        normalize_sales_company_id(value)
    except InvalidReferenceError:
        return 1 if "/company/" in value else 2
    return 0


def planned_actions(criteria: Mapping[str, Any]) -> int:
    """The most actions a confirmed save can count against the rolling hour.

    The search page, each location or company name to resolve, each kind of
    list to look up, the create, the rename and the read-back page. Counted
    before the browser starts, so the rename is never the action the hour
    runs out on; a name already cached costs less, never more.
    """
    geo = sum(
        not str(value).removeprefix("urn:li:geo:").isdigit()
        for key in ("location", "company_hq_location", "hq_location")
        for value in criteria.get(key) or []
    )
    companies = sum(
        _company_lookups(str(value))
        for key in ("current_company", "exclude_current_company")
        for value in criteria.get(key) or []
    )
    lists = bool(criteria.get("lead_list") or criteria.get("exclude_lead_list")) + bool(
        criteria.get("account_list")
    )
    return 4 + geo + companies + lists


def refuse_invalid_search_name(name: str) -> str | None:
    """Why ``name`` cannot be a saved search's name, or None."""
    text = name.strip()
    if not text:
        return "name must not be empty."
    if "\n" in text or "\r" in text:
        return "name must be one line."
    if len(text) > MAX_NAME_LENGTH:
        return f"name must be at most {MAX_NAME_LENGTH} characters."
    return None


def saved_search_result(
    status: str,
    message: str,
    *,
    dry_run: bool,
    kind: str,
    name: str,
    retry_safe: bool = True,
    search_url: str | None = None,
    saved_id: str | None = None,
) -> dict[str, Any]:
    """The one result shape of ``save_sales_navigator_search``."""
    search: dict[str, Any] = {"kind": kind, "name": name.strip()}
    if search_url is not None:
        search["url"] = search_url
    if saved_id is not None:
        search["id"] = saved_id
    return {
        "url": search_url or SAVED_SEARCHES_URL,
        "tool": SAVE_TOOL,
        "status": status,
        "message": message,
        "dry_run": dry_run,
        "retry_safe": retry_safe,
        "search": search,
    }


def _wanted_values(search_filter: SearchFilter) -> set[tuple[str, str, str]]:
    """A filter's values as ``(key, value, selectionType)``: an id, or a text."""
    values = {("id", v, "INCLUDED") for v in search_filter.ids}
    values |= {("text", v, "INCLUDED") for v in search_filter.texts}
    values |= {("id", v, "EXCLUDED") for v in search_filter.excluded_ids}
    return values


def _sent_values(values: Any, label: str) -> set[tuple[str, str, str]] | None:
    """The values a body or a read-back carries; ``label`` names a text's key."""
    if not isinstance(values, list):
        return None
    found: set[tuple[str, str, str]] = set()
    for value in values:
        if not isinstance(value, Mapping):
            return None
        selection = value.get("selectionType")
        if value.get("id") is not None:
            found.add(("id", str(value["id"]), str(selection)))
        elif isinstance(value.get(label), str):
            found.add(("text", value[label], str(selection)))
        else:
            return None
    return found


def _range(search_filter: SearchFilter) -> dict[str, str]:
    return {
        name: str(bound)
        for name, bound in (
            ("min", search_filter.minimum),
            ("max", search_filter.maximum),
        )
        if bound is not None
    }


def _sent_range(value: Any) -> dict[str, str] | None:
    """A body's ``rangeValue`` with its bounds as text, the way the app sends them.

    Measured 2026-11-28: a department headcount's create carries
    ``{"min": "20", "max": "500"}``, strings. A bound sent as a number is read
    the same; anything else is no range.
    """
    if not isinstance(value, Mapping):
        return None
    bounds: dict[str, str] = {}
    for name, bound in value.items():
        if isinstance(bound, bool) or not isinstance(bound, (int, str)):
            return None
        bounds[str(name)] = str(bound)
    return bounds


_NUMBER = re.compile(r"\d(?:[\d,.\u00a0\u202f]*\d)?")


def _range_matches(sent: Any, wanted: Mapping[str, str], *, nested: bool) -> bool:
    """Whether a ``rangeValue`` holds exactly the ``wanted`` bounds.

    Measured 2026-11-28: a saved department headcount reads back as
    ``{"displayValue": "Engineering 20 – 500"}``, the department's label and
    the bounds, with no ids. The numbers that label shows are the bounds, in
    order.
    """
    if nested and isinstance(sent, Mapping) and set(sent) == {"displayValue"}:
        label = sent["displayValue"]
        if not isinstance(label, str):
            return False
        shown = [re.sub(r"\D", "", number) for number in _NUMBER.findall(label)]
        return shown == list(wanted.values())
    return _sent_range(sent) == wanted


def filters_mismatch(
    wanted: Sequence[SearchFilter], sent: Any, *, label: str, nested: bool = False
) -> str | None:
    """How ``sent`` differs from exactly the ``wanted`` filters, or None.

    ``label`` is the key a text value carries (``text`` in the app's body,
    ``displayValue`` in a read-back), whose ids are matched and whose
    display labels for an id are ignored. A read-back (``nested``) wraps each
    filter in ``singleFilterMetadata`` and lists the empty ones too. The
    difference names filter types and fields only, never a value. A read-back
    of a department headcount names its department by a display label only,
    so there the department is the create's to check, and the bounds are read
    from the label.
    """
    if not isinstance(sent, list):
        return "the filters are not a list"
    by_type: dict[str, Mapping[str, Any]] = {}
    for item in sent:
        record = (
            item.get("singleFilterMetadata")
            if nested and isinstance(item, Mapping)
            else item
        )
        if not isinstance(record, Mapping) or not isinstance(record.get("type"), str):
            return "a filter has no type"
        if nested and not record.get("values") and not record.get("rangeValue"):
            continue
        if record["type"] in by_type:
            return f"the {record['type']} filter is there twice"
        by_type[record["type"]] = record
    asked = {f.type: f for f in wanted if _wanted_values(f) or _range(f)}
    if extra := sorted(set(by_type) - set(asked)):
        return f"it has filters not asked: {', '.join(extra)}"
    if missing := sorted(set(asked) - set(by_type)):
        return f"it lacks the asked filters: {', '.join(missing)}"
    for kind, search_filter in asked.items():
        record = by_type[kind]
        if (
            search_filter.sub_filter is not None
            and (not nested or "selectedSubFilter" in record)
            and str(record.get("selectedSubFilter")) != search_filter.sub_filter
        ):
            return f"the {kind} filter's selectedSubFilter differs"
        if _range(search_filter):
            if not _range_matches(
                record.get("rangeValue"), _range(search_filter), nested=nested
            ):
                return f"the {kind} filter's rangeValue differs"
            if not _wanted_values(search_filter):
                continue
        if _sent_values(record.get("values"), label) != _wanted_values(search_filter):
            return f"the {kind} filter's values differ"
    return None


def create_request(
    kind: str, keywords: str | None, filters: Sequence[SearchFilter]
) -> PlannedListWrite:
    """The app's create, admitted when its search is exactly the plan."""
    _, query_key, use_case, _ = KINDS[kind]

    def mismatch(body: Any) -> str | None:
        if not isinstance(body, Mapping) or body.get("useCase") != use_case:
            return f"its useCase is not {use_case}"
        search = body.get("searchQuery")
        if not isinstance(search, Mapping) or set(search) != {query_key}:
            return f"its searchQuery is not one {query_key}"
        inner = search[query_key]
        if not isinstance(inner, Mapping):
            return f"its {query_key} is not an object"
        if (inner.get("keywords") or None) != keywords or (
            body.get("keywords") or None
        ) not in (None, keywords):
            return "its keywords differ"
        return filters_mismatch(filters, inner.get("filters", []), label="text")

    return PlannedListWrite(
        "POST",
        SAVED_SEARCHES,
        None,
        (),
        {"useCase": use_case, "keywords": keywords, "filters": len(filters)},
        body_mismatch=mismatch,
    )


def created_key(restli_id: str | None) -> tuple[str, str] | None:
    """``(seat urn, saved search id)`` from the create's ``x-restli-id``."""
    match = _RESTLI_KEY.match((restli_id or "").strip())
    return (match.group(1), match.group(2)) if match else None


def rename_request(seat: str, saved_id: str, name: str) -> PlannedListWrite:
    """The panel's Edit form saving ``name`` for that one saved search."""
    entity = f"(seat:{quote(seat, safe='')},id:{saved_id})"
    return PlannedListWrite(
        "POST", SAVED_SEARCHES, entity, (), {"patch": {"$set": {"name": name}}}
    )


def find_saved(payload: Any, saved_id: str) -> Mapping[str, Any] | None:
    """The saved search ``saved_id`` in a panel read, or None."""
    elements = payload.get("elements") if isinstance(payload, Mapping) else None
    for element in elements if isinstance(elements, list) else ():
        if isinstance(element, Mapping) and str(element.get("id")) == saved_id:
            return element
    return None


def readback_mismatch(
    element: Mapping[str, Any],
    name: str,
    keywords: str | None,
    filters: Sequence[SearchFilter],
) -> str | None:
    """Why a read-back is not the search that was saved, or None."""
    if element.get("name") != name:
        return "its name is not the one asked"
    if (element.get("keywords") or None) != keywords:
        return "its keywords are not the ones asked"
    why = filters_mismatch(
        filters, element.get("filters"), label="displayValue", nested=True
    )
    if why is not None:
        return f"its filters are not the ones asked ({why})"
    return None
