"""Locale-independent query building and parsing for Sales Navigator's API.

Sales Navigator answers from ``/sales-api/`` in flat Rest.li (``elements``,
``paging``, ``metadata``). A search page URL carries the same Rest.li ``query``
tuple the page sends to the API, so a search is one navigation to a URL built
here. Everything here is pure: builders return ``None`` on a wrong shape and
never raise.

Display-text fields (``industry``, ``employeeCountRange``, seniority ``name``)
are labels in the member's UI language; ids carry identity.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, NotRequired, TypedDict, TypeVar, cast
from urllib.parse import quote, urlparse

from linkedin_mcp_server.scraping.identifiers import sales_company_url
from linkedin_mcp_server.scraping.restli import (
    as_dicts,
    as_int,
    as_mapping,
    as_str,
    iso_from_ms,
    plain_elements,
    restli_list,
)

SN_API_PREFIX = "/sales-api/"
PAGE_SIZE = 25
RESULT_CAP = 1000
MAX_PAGE = 40

INDUSTRY = "INDUSTRY"
REGION = "REGION"
COMPANY_HEADCOUNT = "COMPANY_HEADCOUNT"
COMPANY_HEADCOUNT_GROWTH = "COMPANY_HEADCOUNT_GROWTH"
CURRENT_TITLE = "CURRENT_TITLE"
SENIORITY_LEVEL = "SENIORITY_LEVEL"
CURRENT_COMPANY = "CURRENT_COMPANY"

SENIORITY_IDS: Mapping[str, str] = {
    "in_training": "100",
    "entry_level": "110",
    "senior": "120",
    "strategic": "130",
    "entry_level_manager": "200",
    "experienced_manager": "210",
    "director": "220",
    "vice_president": "300",
    "cxo": "310",
    "owner_partner": "320",
}
COMPANY_FILTER_ID = "urn:li:organization:{}"

WITHHELD_REASON = (
    "These members asked LinkedIn not to share their data with third-party tools."
)

T = TypeVar("T")

_SALES_COMPANY_URN = "urn:li:fs_salesCompany:"
_SALES_PROFILE_URN = "urn:li:fs_salesProfile:("
_INCLUDED = "selectionType:INCLUDED"
# The UI sends every company value with this parent marker; sent the same way.
_COMPANY_PARENT = "parent:(id:0)"


@dataclass(frozen=True, slots=True)
class SnCall:
    resource: str
    entity_id: str | None
    finder: str | None
    method: str
    params: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class SearchFilter:
    type: str
    ids: tuple[str, ...] = ()
    texts: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None


class SnMoney(TypedDict):
    amount: str
    currencyCode: str
    unit: NotRequired[str]


class SearchPaging(TypedDict):
    page: int
    start: int
    count: int
    total: int
    resultCap: int
    capped: bool
    more: bool


class Withheld(TypedDict):
    count: int
    reason: str


class AccountCard(TypedDict, total=False):
    id: str
    name: str
    industry: str
    employeeCountRange: str
    employeeDisplayCount: str
    description: str
    saved: bool
    listCount: int
    salesNavigatorUrl: str


class Position(TypedDict, total=False):
    title: str
    companyName: str
    companyId: str
    tenureAtPosition: dict[str, Any]
    tenureAtCompany: dict[str, Any]
    startedOn: dict[str, Any]
    current: bool


class Seniority(TypedDict):
    id: str
    name: str


class Lead(TypedDict, total=False):
    id: str
    memberUrn: str
    fullName: str
    firstName: str
    lastName: str
    geoRegion: str
    degree: int
    premium: bool
    openLink: bool
    seniority: list[Seniority]
    currentPositions: list[Position]
    summary: str


class Headquarters(TypedDict, total=False):
    country: str
    geographicArea: str
    city: str
    postalCode: str
    line1: str


class RevenueRange(TypedDict, total=False):
    min: SnMoney
    max: SnMoney


class EmployeeGrowth(TypedDict):
    timespan: str
    percentage: int | float


class Account(TypedDict, total=False):
    id: str
    name: str
    description: str
    industry: str
    type: str
    specialties: list[str]
    website: str
    yearFounded: int
    headquarters: Headquarters
    revenueRange: RevenueRange
    employeeCount: int
    employeeCountRange: str
    employeeDisplayCount: str
    employeeGrowth: list[EmployeeGrowth]
    flagshipCompanyUrl: str
    salesNavigatorUrl: str
    saved: bool
    listCount: int


class AccountList(TypedDict, total=False):
    id: str
    name: str
    description: str
    listType: str
    listSource: str
    entityCount: int
    role: str
    lastModifiedAt: str


class ListedAccount(TypedDict, total=False):
    id: str
    name: str
    industry: str
    employeeCount: int
    employeeDisplayCount: str
    buyerIntentLevel: str
    saved: bool


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _id_text(value: Any) -> str | None:
    """A provider id as text: a non-empty string or a non-negative int."""
    number = as_int(value)
    if number is not None:
        return str(number) if number >= 0 else None
    return as_str(value)


def _count_text(value: Any) -> str | None:
    """A display count, which SN sends as text or as a number."""
    return str(value) if as_int(value) is not None else as_str(value)


def _str_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    return [item for item in value if isinstance(item, str) and item]


def _copy_present(
    target: dict[str, Any], source: Mapping[str, Any], checks: Mapping[str, Any]
) -> None:
    for key, check in checks.items():
        converted = check(source.get(key))
        if converted is not None:
            target[key] = converted


def sales_company_id(urn: Any) -> str | None:
    """The numeric id of ``urn:li:fs_salesCompany:<n>``."""
    if not isinstance(urn, str) or not urn.startswith(_SALES_COMPANY_URN):
        return None
    tail = urn[len(_SALES_COMPANY_URN) :]
    return tail if tail.isdigit() else None


def sales_profile_id(urn: Any) -> str | None:
    """The profile id from ``urn:li:fs_salesProfile:(<id>,<authType>,<token>)``."""
    if not isinstance(urn, str) or not urn.startswith(_SALES_PROFILE_URN):
        return None
    if not urn.endswith(")"):
        return None
    parts = urn[len(_SALES_PROFILE_URN) : -1].split(",")
    if len(parts) != 3 or not parts[0]:
        return None
    return parts[0]


def parse_sn_call(url: str, method: str) -> SnCall | None:
    """Describe a Sales Navigator API request, or ``None`` for any other URL.

    Query values stay raw (still percent-encoded) so a Rest.li tuple can be
    compared with the one that was sent.
    """
    parsed = urlparse(url)
    if not parsed.path.startswith(SN_API_PREFIX):
        return None
    segments = parsed.path[len(SN_API_PREFIX) :].split("/")
    resource = segments[0]
    if not resource:
        return None
    entity_id = segments[1] if len(segments) > 1 and segments[1] else None
    params: dict[str, str] = {}
    for pair in parsed.query.split("&"):
        if not pair:
            continue
        key, _, value = pair.partition("=")
        params.setdefault(key, value)
    return SnCall(
        resource=resource,
        entity_id=entity_id,
        finder=params.get("q"),
        method=method.upper(),
        params=params,
    )


def restli_value(text: str) -> str:
    return quote(text, safe="")


def _filter_clause(search_filter: SearchFilter) -> str | None:
    values = [f"(id:{restli_value(v)},{_INCLUDED})" for v in search_filter.ids]
    if search_filter.type == CURRENT_COMPANY:
        values = [value[:-1] + f",{_COMPANY_PARENT})" for value in values]
    values.extend(f"(text:{restli_value(v)},{_INCLUDED})" for v in search_filter.texts)
    bounds = [
        f"{name}:{bound}"
        for name, bound in (
            ("min", search_filter.minimum),
            ("max", search_filter.maximum),
        )
        if bound is not None
    ]
    parts = []
    if values:
        parts.append(f"values:List({','.join(values)})")
    if bounds:
        parts.append(f"rangeValue:({','.join(bounds)})")
    if not parts:
        return None
    return f"(type:{search_filter.type},{','.join(parts)})"


def build_search_query(keywords: str | None, filters: Sequence[SearchFilter]) -> str:
    """The Rest.li ``query`` tuple for a search page and its API call.

    Never carries ``recentSearchParam``: that field writes the search into the
    member's Recent searches.
    """
    parts: list[str] = []
    if keywords:
        parts.append("spellCorrectionEnabled:true")
        parts.append(f"keywords:{restli_value(keywords)}")
    clauses = [clause for f in filters if (clause := _filter_clause(f)) is not None]
    if clauses:
        parts.append(f"filters:List({','.join(clauses)})")
    return f"({','.join(parts)})"


def search_page_url(kind: Literal["company", "people"], query: str, page: int) -> str:
    url = f"https://www.linkedin.com/sales/search/{kind}?query=" + quote(
        query, safe="(),"
    )
    return url + f"&page={page}" if page > 1 else url


def _split_top(text: str) -> list[str]:
    items: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            items.append("".join(current))
            current = []
        else:
            current.append(char)
    items.append("".join(current))
    return [item for item in items if item]


def _record(text: str) -> dict[str, str]:
    """Fields of a Rest.li record ``(a:x,b:(c:y))``; ``{}`` for anything else."""
    if not (text.startswith("(") and text.endswith(")")):
        return {}
    fields: dict[str, str] = {}
    for item in _split_top(text[1:-1]):
        key, sep, value = item.partition(":")
        if sep:
            fields.setdefault(key, value)
    return fields


def missing_echoes(
    keywords: str | None, filters: Sequence[SearchFilter], captured_query: str
) -> list[str]:
    """Criteria the captured API ``query`` does not carry; ``[]`` = all echoed.

    The response does not echo applied filters and LinkedIn silently ignores an
    unknown filter, so the request the page sent is the only evidence.
    """
    captured = _record(captured_query)
    missing: list[str] = []
    if keywords and captured.get("keywords") != restli_value(keywords):
        missing.append("keywords")
    clauses = [_record(item) for item in restli_list(captured.get("filters", ""))]
    for search_filter in filters:
        if _filter_clause(search_filter) is None:
            continue
        same_type = [c for c in clauses if c.get("type") == search_filter.type]
        values = [
            _record(item)
            for clause in same_type
            for item in restli_list(clause.get("values", ""))
        ]
        ids = {v.get("id") for v in values if v.get("selectionType") == "INCLUDED"}
        texts = {v.get("text") for v in values if v.get("selectionType") == "INCLUDED"}
        ranges = [_record(clause.get("rangeValue", "")) for clause in same_type]
        wanted_range = {
            name: str(bound)
            for name, bound in (
                ("min", search_filter.minimum),
                ("max", search_filter.maximum),
            )
            if bound is not None
        }
        echoed = (
            all(restli_value(v) in ids for v in search_filter.ids)
            and all(restli_value(v) in texts for v in search_filter.texts)
            and (not wanted_range or wanted_range in ranges)
        )
        if not echoed and search_filter.type not in missing:
            missing.append(search_filter.type)
    return missing


def build_paging(payload: Any, page: int) -> SearchPaging | None:
    record = as_mapping(payload)
    paging = as_mapping(record.get("paging")) if record is not None else None
    if paging is None:
        return None
    start = as_int(paging.get("start"))
    count = as_int(paging.get("count"))
    total = as_int(paging.get("total"))
    if start is None or count is None or total is None:
        return None
    return {
        "page": page,
        "start": start,
        "count": count,
        "total": total,
        "resultCap": RESULT_CAP,
        "capped": total > RESULT_CAP,
        "more": start + count < min(total, RESULT_CAP),
    }


def build_account_card(element: Any) -> AccountCard | None:
    record = as_mapping(element)
    company_id = sales_company_id(record.get("entityUrn")) if record else None
    if record is None or company_id is None:
        return None
    card: dict[str, Any] = {"id": company_id}
    name = as_str(record.get("companyName"))
    if name is not None:
        card["name"] = name
    _copy_present(
        card,
        record,
        {
            "industry": as_str,
            "employeeCountRange": as_str,
            "employeeDisplayCount": _count_text,
            "description": as_str,
            "saved": _bool,
            "listCount": as_int,
        },
    )
    card["salesNavigatorUrl"] = sales_company_url(company_id)
    return cast("AccountCard", card)


def is_withheld(element: Any) -> bool:
    record = as_mapping(element)
    return record is not None and record.get("blockThirdPartyDataSharing") is True


def _position(entry: dict[str, Any]) -> Position:
    position: dict[str, Any] = {}
    _copy_present(
        position,
        entry,
        {
            "title": as_str,
            "companyName": as_str,
            "tenureAtPosition": as_mapping,
            "tenureAtCompany": as_mapping,
            "startedOn": as_mapping,
            "current": _bool,
        },
    )
    company_id = sales_company_id(entry.get("companyUrn"))
    if company_id is not None:
        position["companyId"] = company_id
    return cast("Position", position)


def build_lead(element: Any) -> Lead | None:
    """A lead card; ``None`` on a wrong shape or a withheld card."""
    record = as_mapping(element)
    profile_id = sales_profile_id(record.get("entityUrn")) if record else None
    if record is None or profile_id is None or is_withheld(record):
        return None
    lead: dict[str, Any] = {"id": profile_id}
    member_urn = as_str(record.get("objectUrn"))
    if member_urn is not None:
        lead["memberUrn"] = member_urn
    _copy_present(
        lead,
        record,
        {
            "fullName": as_str,
            "firstName": as_str,
            "lastName": as_str,
            "geoRegion": as_str,
            "degree": as_int,
            "premium": _bool,
            "openLink": _bool,
        },
    )
    seniority: list[Seniority] = []
    for entry in as_dicts(record.get("seniorityV2s")):
        seniority_id = _id_text(entry.get("id"))
        name = as_str(entry.get("displayName"))
        if seniority_id is not None and name is not None:
            seniority.append({"id": seniority_id, "name": name})
    lead["seniority"] = seniority
    lead["currentPositions"] = [
        _position(entry) for entry in as_dicts(record.get("currentPositions"))
    ]
    summary = as_str(record.get("summary"))
    if summary is not None:
        lead["summary"] = summary
    return cast("Lead", lead)


def build_leads(elements: Iterable[Any]) -> tuple[list[Lead], int] | None:
    """Leads from search cards and the count withheld for the sharing block.

    A withheld card is dropped before any of its fields is read; ``None`` if
    any other card has a wrong shape.
    """
    leads: list[Lead] = []
    withheld = 0
    for element in elements:
        if is_withheld(element):
            withheld += 1
            continue
        lead = build_lead(element)
        if lead is None:
            return None
        leads.append(lead)
    return leads, withheld


def build_withheld(count: int) -> Withheld | None:
    return {"count": count, "reason": WITHHELD_REASON} if count > 0 else None


def _money(value: Any) -> SnMoney | None:
    record = as_mapping(value)
    if record is None:
        return None
    amount = record.get("amount")
    currency = as_str(record.get("currencyCode"))
    if _number(amount) is not None:
        amount = str(amount)
    if not isinstance(amount, str) or not amount or currency is None:
        return None
    money: SnMoney = {"amount": amount, "currencyCode": currency}
    unit = as_str(record.get("unit"))
    if unit is not None:
        money["unit"] = unit
    return money


def _record_company_id(record: Mapping[str, Any]) -> str | None:
    urn_id = sales_company_id(record.get("entityUrn"))
    return urn_id if urn_id is not None else _id_text(record.get("id"))


def build_account(records: Sequence[dict[str, Any]], company_id: str) -> Account | None:
    """Merge the account page's decorations field-wise into one account.

    ``None`` unless at least one record names ``company_id`` and none names
    another company. Employee lists and pictures are never copied.
    """
    mappings = [as_mapping(record) for record in records]
    if not mappings or any(record is None for record in mappings):
        return None
    ids = [_record_company_id(record) for record in mappings if record is not None]
    if company_id not in ids or any(i is not None and i != company_id for i in ids):
        return None
    account: dict[str, Any] = {"id": company_id}
    for record in mappings:
        if record is None:
            continue
        _copy_present(
            account,
            record,
            {
                "name": as_str,
                "description": as_str,
                "industry": as_str,
                "type": as_str,
                "specialties": _str_list,
                "website": as_str,
                "yearFounded": as_int,
                "employeeCount": as_int,
                "employeeCountRange": as_str,
                "employeeDisplayCount": _count_text,
                "flagshipCompanyUrl": as_str,
            },
        )
        headquarters = as_mapping(record.get("headquarters"))
        if headquarters is not None:
            hq: dict[str, Any] = {}
            _copy_present(
                hq,
                headquarters,
                {
                    "country": as_str,
                    "geographicArea": as_str,
                    "city": as_str,
                    "postalCode": as_str,
                    "line1": as_str,
                },
            )
            if hq:
                account["headquarters"] = hq
        revenue = as_mapping(record.get("revenueRange"))
        if revenue is not None:
            bounds: dict[str, Any] = {}
            low = _money(revenue.get("estimatedMinRevenue"))
            high = _money(revenue.get("estimatedMaxRevenue"))
            if low is not None:
                bounds["min"] = low
            if high is not None:
                bounds["max"] = high
            if bounds:
                account["revenueRange"] = bounds
        growth: list[EmployeeGrowth] = []
        for entry in as_dicts(record.get("employeeGrowthPercentages")):
            timespan = as_str(entry.get("timespan"))
            percentage = _number(entry.get("percentage"))
            if timespan is not None and percentage is not None:
                growth.append({"timespan": timespan, "percentage": percentage})
        if growth:
            account["employeeGrowth"] = growth
        member_account = as_mapping(record.get("account"))
        if member_account is not None:
            _copy_present(
                account, member_account, {"saved": _bool, "listCount": as_int}
            )
    account["salesNavigatorUrl"] = sales_company_url(company_id)
    return cast("Account", account)


def build_account_list(element: Any) -> AccountList | None:
    record = as_mapping(element)
    list_id = _id_text(record.get("id")) if record is not None else None
    if record is None or list_id is None:
        return None
    account_list: dict[str, Any] = {"id": list_id}
    _copy_present(
        account_list,
        record,
        {
            "name": as_str,
            "description": as_str,
            "listType": as_str,
            "listSource": as_str,
            "entityCount": as_int,
            "role": as_str,
            "lastModifiedAt": iso_from_ms,
        },
    )
    return cast("AccountList", account_list)


def build_listed_account(row: Any) -> ListedAccount | None:
    record = as_mapping(row)
    info = as_mapping(record.get("companyInfo")) if record is not None else None
    company_id = sales_company_id(info.get("entityUrn")) if info else None
    if info is None or company_id is None:
        return None
    listed: dict[str, Any] = {"id": company_id}
    _copy_present(
        listed,
        info,
        {
            "name": as_str,
            "industry": as_str,
            "employeeCount": as_int,
            "employeeDisplayCount": _count_text,
            "buyerIntentLevel": as_str,
        },
    )
    member_account = as_mapping(info.get("account"))
    if member_account is not None:
        saved = _bool(member_account.get("saved"))
        if saved is not None:
            listed["saved"] = saved
    return cast("ListedAccount", listed)


def build_elements(payload: Any, builder: Callable[[Any], T | None]) -> list[T] | None:
    """``builder`` over a flat collection; ``None`` if any element is malformed."""
    elements = plain_elements(payload)
    if elements is None:
        return None
    built: list[T] = []
    for element in elements:
        item = builder(element)
        if item is None:
            return None
        built.append(item)
    return built


def _line(*parts: str | None) -> str:
    return ", ".join(part for part in parts if part)


def _entity_line(name: Any, entity_id: Any, details: str) -> str:
    head = f"{name} (id {entity_id})" if name else f"id {entity_id}"
    return f"{head}: {details}" if details else head


def render_account_cards(cards: Iterable[AccountCard]) -> str:
    return "\n".join(
        _entity_line(
            card.get("name"),
            card.get("id"),
            _line(card.get("industry"), card.get("employeeCountRange")),
        )
        for card in cards
    )


def _lead_role(lead: Lead) -> str | None:
    positions = lead.get("currentPositions") or []
    if not positions:
        return None
    first = positions[0]
    return _line(first.get("title"), first.get("companyName")) or None


def render_leads(leads: Iterable[Lead]) -> str:
    return "\n".join(
        _entity_line(
            lead.get("fullName"),
            lead.get("id"),
            _line(_lead_role(lead), lead.get("geoRegion")),
        )
        for lead in leads
    )


def render_account(account: Account) -> str:
    hq = account.get("headquarters") or {}
    return _entity_line(
        account.get("name"),
        account.get("id"),
        _line(
            account.get("industry"),
            account.get("employeeDisplayCount") or account.get("employeeCountRange"),
            _line(hq.get("city"), hq.get("country")) or None,
            account.get("website"),
        ),
    )


def render_account_lists(lists: Iterable[AccountList]) -> str:
    return "\n".join(
        _entity_line(
            item.get("name"),
            item.get("id"),
            _line(
                f"entityCount {item['entityCount']}" if "entityCount" in item else None,
                item.get("listSource"),
            ),
        )
        for item in lists
    )


def render_listed_accounts(accounts: Iterable[ListedAccount]) -> str:
    return "\n".join(
        _entity_line(
            account.get("name"),
            account.get("id"),
            _line(account.get("industry"), account.get("employeeDisplayCount")),
        )
        for account in accounts
    )
