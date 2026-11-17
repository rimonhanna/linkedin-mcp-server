"""Locale-independent parsing for Campaign Manager's passive API payloads.

Campaign Manager answers from ``/campaign-manager-api/`` in two flavours: plain
Rest.li (``{elements, paging}``) and normalized (``{data, included}``). Long
finders are tunneled as ``POST`` with ``X-HTTP-Method-Override: GET`` and the
query in the form body. Everything here is pure: validators return ``None`` on
a wrong shape and never raise.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal, NotRequired, TypedDict, cast
from urllib.parse import parse_qs, urlparse

CM_API_PREFIX = "/campaign-manager-api/"

_UI_LABELS = {
    "ad_account": "Ad account",
    "campaign_group": "Campaign",
    "campaign": "Ad set",
    "creative": "Ad",
}

_DATE_RANGE_RE = re.compile(
    r"^\(start:\(day:(\d+),month:(\d+),year:(\d+)\),"
    r"end:\(day:(\d+),month:(\d+),year:(\d+)\)\)$"
)

_PIVOT_KINDS = {
    "ACCOUNT": ("ad_account", "urn:li:sponsoredAccount:"),
    "CAMPAIGN_GROUP": ("campaign_group", "urn:li:sponsoredCampaignGroup:"),
    "CAMPAIGN": ("campaign", "urn:li:sponsoredCampaign:"),
    "CREATIVE": ("creative", "urn:li:sponsoredCreative:"),
}
# Only the job function facet was observed; the others share the generic
# "urn:li:" prefix until a live capture pins their facet names.
_DEMOGRAPHIC_URN_PREFIXES = {"MEMBER_JOB_FUNCTION": "urn:li:function:"}
_DEMOGRAPHIC_FALLBACK_PREFIX = "urn:li:"

_BASE_METRICS = (
    "impressions",
    "clicks",
    "costInLocalCurrency",
    "clickThroughRate",
    "costPerClick",
    "costPerThousandImpressions",
)
_OBJECTIVE_METRICS = ("keyResults", "costPerKeyResult")
_CONVERSION_METRICS = ("externalWebsiteConversions", "costPerConversion")
_LEAD_METRICS = ("oneClickLeads",)


@dataclass(frozen=True, slots=True)
class CmCall:
    resource: str
    entity_id: str | None
    finder: str | None
    params: Mapping[str, str]


class Money(TypedDict):
    amount: str
    currencyCode: str


class RunSchedule(TypedDict):
    start: int
    startIso: str
    end: NotRequired[int]
    endIso: NotRequired[str]


class TargetingSegment(TypedDict):
    urn: str
    name: str


class TargetingFacet(TypedDict):
    urn: str
    name: str
    segments: list[TargetingSegment]


class TargetingInclude(TypedDict):
    anyOf: list[TargetingFacet]


class Targeting(TypedDict):
    include: list[TargetingInclude]
    exclude: list[TargetingFacet]


class AdAccount(TypedDict, total=False):
    kind: Literal["ad_account"]
    uiLabel: str
    id: int
    name: str
    status: str
    type: str
    currencyCode: str
    servingStatuses: list[str]
    currentUserRole: str
    testAccount: bool
    organizationUrn: str
    boost: bool


class CampaignGroup(TypedDict, total=False):
    kind: Literal["campaign_group"]
    uiLabel: str
    id: int
    name: str
    status: str
    servingStatuses: list[str]
    objectiveType: str
    startsAt: int
    startsAtIso: str
    endsAt: int
    endsAtIso: str
    totalBudget: Money
    isDefault: bool
    buyingType: str


class Campaign(TypedDict, total=False):
    kind: Literal["campaign"]
    uiLabel: str
    id: int
    accountId: int
    campaignGroupId: int
    name: str
    status: str
    servingStatuses: list[str]
    type: str
    objectiveType: str
    optimizationTargetType: str
    costType: str
    format: str
    adFormats: list[str]
    dailyBudget: Money
    totalBudget: Money
    unitCost: Money
    runSchedule: RunSchedule
    locale: dict[str, str]
    pacingStrategy: str
    audienceExpansionEnabled: bool
    offsiteDeliveryEnabled: bool
    targeting: Targeting


class Creative(TypedDict, total=False):
    kind: Literal["creative"]
    uiLabel: str
    id: int
    campaignId: int
    name: str
    type: str
    status: str
    title: str
    destinationUrl: str


class AnalyticsEntity(TypedDict):
    kind: str
    id: int | str
    urn: str
    name: NotRequired[str]


class AnalyticsRow(TypedDict, total=False):
    entity: AnalyticsEntity
    impressions: int | float
    clicks: int | float
    costInLocalCurrency: int | float
    clickThroughRate: int | float
    costPerClick: int | float
    costPerThousandImpressions: int | float
    keyResults: int | float
    costPerKeyResult: int | float
    externalWebsiteConversions: int | float
    costPerConversion: int | float
    oneClickLeads: int | float


class DateWindow(TypedDict):
    start: str
    end: str
    source: Literal["page", "requested"]


def _mapping(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _str_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    return [item for item in value if isinstance(item, str)]


def _id_from(value: Any, urn_prefix: str) -> int | None:
    """Accept a numeric id or ``urn:li:<type>:<id>``."""
    number = _int(value)
    if number is not None:
        return number
    if isinstance(value, str) and value.startswith(urn_prefix):
        tail = value[len(urn_prefix) :]
        if tail.isdigit():
            return int(tail)
    return None


def _iso_from_ms(value: Any) -> str | None:
    millis = _int(value)
    if millis is None:
        return None
    try:
        moment = datetime.fromtimestamp(millis / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _money(value: Any) -> Money | None:
    record = _mapping(value)
    if record is None:
        return None
    amount = record.get("amount")
    currency = record.get("currencyCode")
    if not isinstance(amount, str) or not amount or not isinstance(currency, str):
        return None
    return {"amount": amount, "currencyCode": currency}


def _copy_present(
    target: dict[str, Any],
    source: Mapping[str, Any],
    checks: Mapping[str, Any],
) -> None:
    """Copy ``source[key]`` into ``target`` when ``checks[key](value)`` is not None."""
    for key, check in checks.items():
        converted = check(source.get(key))
        if converted is not None:
            target[key] = converted


def _timestamp_fields(
    target: dict[str, Any], source: Mapping[str, Any], start_key: str, end_key: str
) -> None:
    for key in (start_key, end_key):
        millis = _int(source.get(key))
        iso = _iso_from_ms(millis)
        if millis is not None and iso is not None:
            target[key] = millis
            target[f"{key}Iso"] = iso


def parse_cm_call(
    url: str,
    method: str,
    headers: Mapping[str, str],
    post_data: str | None,
) -> CmCall | None:
    """Describe a Campaign Manager API request, or ``None`` for any other URL."""
    parsed = urlparse(url)
    if not parsed.path.startswith(CM_API_PREFIX):
        return None
    segments = parsed.path[len(CM_API_PREFIX) :].split("/")
    resource = segments[0]
    if not resource:
        return None
    entity_id = segments[1] if len(segments) > 1 and segments[1].isdigit() else None

    params: dict[str, str] = {}
    for key, values in parse_qs(parsed.query, keep_blank_values=True).items():
        params[key] = values[0]
    lowered = {name.lower(): value for name, value in headers.items()}
    tunneled = (
        method.upper() == "POST"
        and lowered.get("x-http-method-override", "").upper() == "GET"
    )
    if tunneled and post_data:
        for key, values in parse_qs(post_data, keep_blank_values=True).items():
            params[key] = values[0]
    return CmCall(
        resource=resource,
        entity_id=entity_id,
        finder=params.get("q"),
        params=params,
    )


def restli_date_range(value: str) -> tuple[date, date] | None:
    match = _DATE_RANGE_RE.match(value) if isinstance(value, str) else None
    if match is None:
        return None
    day_s, month_s, year_s, day_e, month_e, year_e = (int(g) for g in match.groups())
    try:
        return date(year_s, month_s, day_s), date(year_e, month_e, day_e)
    except ValueError:
        return None


def format_restli_date_range(start: date, end: date) -> str:
    return (
        f"(start:(day:{start.day},month:{start.month},year:{start.year}),"
        f"end:(day:{end.day},month:{end.month},year:{end.year}))"
    )


def restli_list(value: str) -> list[str]:
    """Split ``List(a,b)`` on top-level commas; anything else gives ``[]``."""
    if not isinstance(value, str):
        return []
    if not (value.startswith("List(") and value.endswith(")")):
        return []
    inner = value[len("List(") : -1]
    items: list[str] = []
    depth = 0
    current: list[str] = []
    for char in inner:
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


def plain_elements(payload: Any) -> list[dict[str, Any]] | None:
    """``elements`` of a plain Rest.li collection; ``[]`` is a valid empty page."""
    record = _mapping(payload)
    if record is None:
        return None
    elements = record.get("elements")
    if not isinstance(elements, list) or not all(
        isinstance(item, dict) for item in elements
    ):
        return None
    return [item for item in elements if isinstance(item, dict)]


def normalized_record(payload: Any) -> dict[str, Any] | None:
    record = _mapping(payload)
    if record is None:
        return None
    return _mapping(record.get("data"))


def normalized_elements(payload: Any) -> list[dict[str, Any]] | None:
    """Entities of a normalized collection, resolving URN refs against ``included``."""
    record = _mapping(payload)
    data = _mapping(record.get("data")) if record is not None else None
    if record is None or data is None:
        return None
    elements = data.get("elements")
    if not isinstance(elements, list):
        return None
    included: dict[str, dict[str, Any]] = {}
    for entity in _dicts(record.get("included")):
        urn = entity.get("entityUrn")
        if isinstance(urn, str):
            included[urn] = entity
    resolved: list[dict[str, Any]] = []
    for element in elements:
        if isinstance(element, dict):
            resolved.append(element)
        elif isinstance(element, str) and element in included:
            resolved.append(included[element])
        else:
            return None
    return resolved


def build_ad_account(element: Any) -> AdAccount | None:
    record = _mapping(element)
    account_id = _int(record.get("id")) if record is not None else None
    if record is None or account_id is None:
        return None
    account: dict[str, Any] = {
        "kind": "ad_account",
        "uiLabel": _UI_LABELS["ad_account"],
        "id": account_id,
    }
    _copy_present(
        account,
        record,
        {
            "name": _str,
            "status": _str,
            "type": _str,
            "currencyCode": _str,
            "servingStatuses": _str_list,
            "currentUserRole": _str,
            "testAccount": lambda v: v if isinstance(v, bool) else None,
        },
    )
    organization = _str(record.get("organizationReference")) or _str(
        record.get("reference")
    )
    if organization is not None:
        account["organizationUrn"] = organization
    return cast("AdAccount", account)


def build_boost_account(element: Any) -> AdAccount | None:
    """A member's personal boost account (``q=memberBoostAccounts``).

    Its ``reference`` is the member, not an organization, so it is not kept.
    """
    account = build_ad_account(element)
    if account is None:
        return None
    if not account.get("organizationUrn", "").startswith(
        ("urn:li:organization:", "urn:li:company:")
    ):
        account.pop("organizationUrn", None)
    account["boost"] = True
    return account


def build_campaign_group(element: Any) -> CampaignGroup | None:
    record = _mapping(element)
    group_id = _int(record.get("id")) if record is not None else None
    if record is None or group_id is None:
        return None
    group: dict[str, Any] = {
        "kind": "campaign_group",
        "uiLabel": _UI_LABELS["campaign_group"],
        "id": group_id,
    }
    _copy_present(
        group,
        record,
        {
            "name": _str,
            "status": _str,
            "servingStatuses": _str_list,
            "objectiveType": _str,
            "buyingType": _str,
            "totalBudget": _money,
        },
    )
    default = record.get("defaultCampaignGroup")
    if isinstance(default, bool):
        group["isDefault"] = default
    schedule = _mapping(record.get("runSchedule"))
    _timestamp_fields(
        group,
        {
            "startsAt": record.get("startsAt", (schedule or {}).get("start")),
            "endsAt": record.get("endsAt", (schedule or {}).get("end")),
        },
        "startsAt",
        "endsAt",
    )
    return cast("CampaignGroup", group)


def _targeting_facet(entry: Any) -> TargetingFacet | None:
    record = _mapping(entry)
    facet = _mapping(record.get("facet")) if record is not None else None
    if record is None or facet is None:
        return None
    urn = _str(facet.get("urn"))
    if urn is None:
        return None
    segments: list[TargetingSegment] = []
    for segment in _dicts(record.get("segments")):
        segment_urn = _str(segment.get("urn"))
        if segment_urn is not None:
            segments.append(
                {"urn": segment_urn, "name": _str(segment.get("name")) or ""}
            )
    return {"urn": urn, "name": _str(facet.get("name")) or "", "segments": segments}


def build_targeting(criteria: Any) -> Targeting | None:
    """Flatten CM's decorated ``targetingCriteria`` (include AND of ORs, exclude OR)."""
    record = _mapping(criteria)
    if record is None:
        return None
    include_block = _mapping(record.get("include"))
    exclude_block = _mapping(record.get("exclude"))
    if include_block is None and exclude_block is None:
        return None
    include: list[TargetingInclude] = []
    for group in _dicts((include_block or {}).get("and")):
        facets = [
            facet
            for entry in (group.get("or") or [])
            if (facet := _targeting_facet(entry)) is not None
        ]
        if facets:
            include.append({"anyOf": facets})
    exclude = [
        facet
        for entry in ((exclude_block or {}).get("or") or [])
        if (facet := _targeting_facet(entry)) is not None
    ]
    return {"include": include, "exclude": exclude}


def build_campaign(record: Any) -> Campaign | None:
    """Build from a Reporting element or the full normalized record."""
    source = _mapping(record)
    campaign_id = _int(source.get("id")) if source is not None else None
    if source is None or campaign_id is None:
        return None
    campaign: dict[str, Any] = {
        "kind": "campaign",
        "uiLabel": _UI_LABELS["campaign"],
        "id": campaign_id,
    }

    def boolean(value: Any) -> bool | None:
        return value if isinstance(value, bool) else None

    def locale(value: Any) -> dict[str, str] | None:
        mapping = _mapping(value)
        if mapping is None:
            return None
        result = {k: v for k, v in mapping.items() if k in ("country", "language")}
        return (
            result
            if result and all(isinstance(v, str) for v in result.values())
            else None
        )

    _copy_present(
        campaign,
        source,
        {
            "accountId": _int,
            "campaignGroupId": _int,
            "name": _str,
            "status": _str,
            "servingStatuses": _str_list,
            "type": _str,
            "objectiveType": _str,
            "optimizationTargetType": _str,
            "costType": _str,
            "format": _str,
            "adFormats": _str_list,
            "dailyBudget": _money,
            "totalBudget": _money,
            "unitCost": _money,
            "locale": locale,
            "pacingStrategy": _str,
            "audienceExpansionEnabled": boolean,
            "offsiteDeliveryEnabled": boolean,
        },
    )
    schedule = _mapping(source.get("runSchedule")) or {}
    start = _int(schedule.get("start", source.get("startsAt")))
    end = _int(schedule.get("end", source.get("endsAt")))
    start_iso = _iso_from_ms(start)
    if start is not None and start_iso is not None:
        run: dict[str, Any] = {"start": start, "startIso": start_iso}
        end_iso = _iso_from_ms(end)
        if end is not None and end_iso is not None:
            run["end"] = end
            run["endIso"] = end_iso
        campaign["runSchedule"] = run
    targeting = build_targeting(source.get("targetingCriteria"))
    if targeting is not None:
        campaign["targeting"] = targeting
    return cast("Campaign", campaign)


def build_creative(element: Any) -> Creative | None:
    """Defensive: the details-page creative entity was never observed."""
    record = _mapping(element)
    if record is None:
        return None
    creative_id = _id_from(record.get("id"), "urn:li:sponsoredCreative:")
    if creative_id is None:
        creative_id = _id_from(record.get("entityUrn"), "urn:li:sponsoredCreative:")
    if creative_id is None:
        return None
    creative: dict[str, Any] = {
        "kind": "creative",
        "uiLabel": _UI_LABELS["creative"],
        "id": creative_id,
    }
    campaign_id = _id_from(record.get("campaignId"), "urn:li:sponsoredCampaign:")
    if campaign_id is None:
        campaign_id = _id_from(record.get("campaign"), "urn:li:sponsoredCampaign:")
    if campaign_id is not None:
        creative["campaignId"] = campaign_id
    status = _str(record.get("status"))
    if status is None:
        status = _str((_mapping(record.get("statusV2")) or {}).get("status"))
    if status is not None:
        creative["status"] = status
    name = record.get("name")
    if isinstance(name, int) and not isinstance(name, bool):
        name = str(name)
    if _str(name) is not None:
        creative["name"] = name
    _copy_present(
        creative,
        record,
        {"type": _str, "title": _str, "destinationUrl": _str},
    )
    return cast("Creative", creative)


def _pivot_kind(pivot: str) -> tuple[str, str]:
    if pivot in _PIVOT_KINDS:
        return _PIVOT_KINDS[pivot]
    return pivot, _DEMOGRAPHIC_URN_PREFIXES.get(pivot, _DEMOGRAPHIC_FALLBACK_PREFIX)


def build_analytics_rows(payload: Any, pivot: str) -> list[AnalyticsRow] | None:
    """Rows of a ``campaignManagerAdAnalytics`` reply, or ``None`` on a URN mismatch."""
    elements = plain_elements(payload)
    if elements is None:
        return None
    kind, prefix = _pivot_kind(pivot)
    rows: list[AnalyticsRow] = []
    for element in elements:
        values = element.get("pivotValues")
        first = _mapping(values[0]) if isinstance(values, list) and values else None
        urn = _str(first.get("id")) if first is not None else None
        if first is None or urn is None or not urn.startswith(prefix):
            return None
        tail = urn[len(prefix) :]
        entity: dict[str, Any] = {
            "kind": kind,
            "id": int(tail) if tail.isdigit() else tail,
            "urn": urn,
        }
        name = _str(first.get("displayText"))
        if name is not None:
            entity["name"] = name
        row: dict[str, Any] = {"entity": entity}
        for group, keys in (
            ("baseMetrics", _BASE_METRICS),
            ("objectiveMetrics", _OBJECTIVE_METRICS),
            ("conversionMetrics", _CONVERSION_METRICS),
            ("leadMetrics", _LEAD_METRICS),
        ):
            metrics = _mapping(element.get(group)) or {}
            for key in keys:
                number = _number(metrics.get(key))
                if number is not None:
                    row[key] = number
        rows.append(cast("AnalyticsRow", row))
    return rows


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def merge_by_id(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Field-wise merge by ``id``: a later non-empty value wins, first-seen order."""
    merged: dict[Any, dict[str, Any]] = {}
    for record in records:
        record_id = record.get("id")
        if record_id is None:
            continue
        target = merged.setdefault(record_id, {})
        for key, value in record.items():
            if not _is_empty(value):
                target[key] = value
    return list(merged.values())


def _money_text(money: Money | None) -> str:
    return f"{money['amount']} {money['currencyCode']}" if money else ""


def _line(*parts: str | None) -> str:
    return ", ".join(part for part in parts if part)


def _entity_line(name: Any, entity_id: Any, details: str) -> str:
    head = f"{name} (id {entity_id})" if name else f"id {entity_id}"
    return f"{head}: {details}" if details else head


def render_accounts(accounts: Iterable[AdAccount]) -> str:
    lines = [
        _entity_line(
            account.get("name"),
            account.get("id"),
            _line(
                account.get("status"), account.get("type"), account.get("currencyCode")
            ),
        )
        for account in accounts
    ]
    return "\n".join(lines)


def _campaign_line(campaign: Campaign) -> str:
    budget = campaign.get("dailyBudget")
    budget_text = f"daily {_money_text(budget)}" if budget else None
    if budget is None and campaign.get("totalBudget"):
        budget_text = f"total {_money_text(campaign.get('totalBudget'))}"
    return _entity_line(
        campaign.get("name"),
        campaign.get("id"),
        _line(campaign.get("status"), campaign.get("objectiveType"), budget_text),
    )


def _group_line(group: CampaignGroup) -> str:
    budget = group.get("totalBudget")
    return _entity_line(
        group.get("name"),
        group.get("id"),
        _line(group.get("status"), f"total {_money_text(budget)}" if budget else None),
    )


def render_campaigns(
    groups: Iterable[CampaignGroup], campaigns: Iterable[Campaign]
) -> str:
    lines = [f"campaign_group {_group_line(group)}" for group in groups]
    lines.extend(f"campaign {_campaign_line(campaign)}" for campaign in campaigns)
    return "\n".join(lines)


def render_campaign(
    campaign: Campaign,
    group: CampaignGroup | None,
    creatives: Iterable[Creative],
) -> str:
    lines = [f"campaign {_campaign_line(campaign)}"]
    if group is not None:
        lines.append(f"campaign_group {_group_line(group)}")
    for creative in creatives:
        lines.append(
            "creative "
            + _entity_line(
                creative.get("name"),
                creative.get("id"),
                _line(creative.get("status"), creative.get("type")),
            )
        )
    return "\n".join(lines)


def render_analytics(
    rows: Iterable[AnalyticsRow], window: DateWindow, currency: str | None
) -> str:
    lines = [f"window {window['start']} to {window['end']} ({window['source']})"]
    for row in rows:
        entity = row["entity"]
        metrics = [
            f"{key} {row[key]}"  # type: ignore[literal-required]
            for key in (
                "impressions",
                "clicks",
                "clickThroughRate",
                "costInLocalCurrency",
                "keyResults",
                "externalWebsiteConversions",
                "oneClickLeads",
            )
            if key in row
        ]
        if "costInLocalCurrency" in row and currency:
            metrics = [
                m + f" {currency}" if m.startswith("costInLocalCurrency") else m
                for m in metrics
            ]
        lines.append(
            f"{entity['kind']} {entity.get('name', entity['urn'])} (id {entity['id']}): "
            + ", ".join(metrics)
        )
    return "\n".join(lines)
