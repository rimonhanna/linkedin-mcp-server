"""Pure planning and admission rules for Campaign Manager writes.

A write tool computes its whole change here before the browser is touched, and
the guard asks the admission rules here whether a request the app is about to
send matches that plan. Nothing here reads a page: every input is a record or a
request body the caller already holds.

``record`` is always a campaign as ``normalized_record`` returns it from
``campaignManagerCampaigns/{id}``: raw field names, money as
``{amount, currencyCode}``, ``runSchedule`` in epoch milliseconds and the
decorated ``targetingCriteria``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.scraping.campaign_manager_payload import CmCall
from linkedin_mcp_server.scraping.identifiers import normalize_targeting_urn

Refusal = tuple[str, str]
"""``(status, message)``: why a plan was not made, in the tool's vocabulary."""

TargetingKey = tuple[str, str, str]
"""``(section, facetUrn, segmentUrn)``; sections name the UI blocks."""

DAY_MS = 86_400_000
EDITABLE_STATUSES = frozenset({"DRAFT", "ACTIVE", "PAUSED"})
SAVED_AUDIENCE_NAME_MAX = 25
LIST_AUDIENCE_NAME_MAX = 200

_BUDGET_RE = re.compile(r"^\d{1,7}(\.\d{1,2})?$")
_OBJECTIVE_RE = re.compile(r"^[A-Z][A-Z_]{2,40}$")
_MONEY_FIELDS = frozenset({"dailyBudget", "totalBudget", "unitCost"})
# The app adds this to a save on its own; it carries no campaign setting.
_BOOKKEEPING_KEYS = frozenset({"contentTrackingId"})
_LOCATION_FACETS = frozenset(
    {
        "urn:li:adTargetingFacet:locations",
        "urn:li:adTargetingFacet:profileLocations",
    }
)
_LANGUAGE_FACET = "urn:li:adTargetingFacet:interfaceLocales"
_MAX_TARGETING_ITEMS = 10
_ACCOUNT_URN = "urn:li:sponsoredAccount:"
_CAMPAIGN_URN = "urn:li:sponsoredCampaign:"
_CAMPAIGN_GROUP_URN = "urn:li:sponsoredCampaignGroup:"


class WriteRequest(Protocol):
    """What an admission rule reads of a request (the guard's ``CmWriteRequest``)."""

    @property
    def call(self) -> CmCall: ...

    @property
    def method(self) -> str: ...

    @property
    def restli_method(self) -> str | None: ...

    @property
    def body(self) -> Any: ...


Admit = Callable[[WriteRequest], "str | None"]


# Budgets


def parse_budget(value: str) -> Decimal:
    """A budget as typed by the caller: a plain dot-decimal, at most 2 places."""
    text = value.strip()
    if not _BUDGET_RE.fullmatch(text) or Decimal(text) <= 0:
        raise ValueError(
            f"Budget {value!r} is not a positive amount. Use a plain number with "
            "a dot, e.g. 12.5, without currency signs or thousands separators."
        )
    return Decimal(text)


def format_budget(amount: Decimal) -> str:
    """The text typed into the budget input: plain dot-decimal, no grouping.

    A comma would be read as a decimal separator in some locales, so only this
    form parses the same everywhere.
    """
    return format(amount.normalize(), "f")


# Dates


def ms_to_date(ms: int) -> date:
    """The calendar day Campaign Manager shows for an epoch-ms value: its UTC date.

    Observed live (D7): the shown date is the UTC date in every case, starts
    can be creation timestamps and ends sit at 23:45 UTC of the shown day.
    """
    return datetime.fromtimestamp(ms / 1000, UTC).date()


def planned_end_ms(record: Mapping[str, Any], day: date) -> int:
    """The epoch-ms end for ``day``, at the record's own end time of day.

    Records keep their end at 23:45 UTC, so a new end keeps that time; a
    record without an end gives UTC midnight.
    """
    _, old_end = _record_schedule(record)
    midnight = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())
    return midnight * 1000 + (old_end % DAY_MS if old_end is not None else 0)


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _ms(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _record_schedule(record: Mapping[str, Any]) -> tuple[int | None, int | None]:
    schedule = _mapping(record.get("runSchedule")) or {}
    return _ms(schedule.get("start")), _ms(schedule.get("end"))


# Campaign edits


@dataclass(frozen=True)
class CampaignEditPlan:
    """The fields that change; ``order`` is the order to apply them in."""

    daily: Decimal | None
    lifetime: Decimal | None
    start: date | None
    end: date | None
    order: tuple[str, ...]


def _money_value(value: Any) -> tuple[Decimal, str] | None:
    money = _mapping(value)
    if money is None:
        return None
    amount = money.get("amount")
    currency = money.get("currencyCode")
    if isinstance(amount, bool) or not isinstance(currency, str) or not currency:
        return None
    if not isinstance(amount, (str, int, float)):
        return None
    try:
        number = Decimal(str(amount))
    except InvalidOperation:
        return None
    return (number, currency) if number.is_finite() else None


def plan_campaign_edit(
    record: Mapping[str, Any],
    *,
    daily: Decimal | None,
    lifetime: Decimal | None,
    start: date | None,
    end: date | None,
    today: date,
) -> CampaignEditPlan | Refusal:
    """The budget and schedule change against ``record``, or why there is none.

    Budget and schedule types never switch (D3): a daily budget is set only
    where the record has one, a lifetime budget likewise, and an end date only
    where the record already ends.
    """
    status = record.get("status")
    if status not in EDITABLE_STATUSES:
        return (
            "not_editable",
            f"This ad set is {status or 'in an unknown state'}; only draft, "
            "active or paused ad sets can be edited.",
        )
    old_daily = _money_value(record.get("dailyBudget"))
    old_lifetime = _money_value(record.get("totalBudget"))
    if daily is not None and old_daily is None:
        return (
            "budget_type_mismatch",
            "This ad set has no daily budget, so a daily budget cannot be set. "
            "Changing the budget type is not supported.",
        )
    if lifetime is not None and old_lifetime is None:
        return (
            "budget_type_mismatch",
            "This ad set has no lifetime budget, so a lifetime budget cannot be "
            "set. Changing the budget type is not supported.",
        )
    old_start_ms, old_end_ms = _record_schedule(record)
    if (start is not None or end is not None) and old_start_ms is None:
        return ("invalid_change", "This ad set has no schedule to change.")
    if end is not None and old_end_ms is None:
        return (
            "invalid_change",
            "This ad set runs continuously with no end date. Switching it to a "
            "fixed schedule is not supported.",
        )
    old_start = ms_to_date(old_start_ms) if old_start_ms is not None else None
    old_end = ms_to_date(old_end_ms) if old_end_ms is not None else None

    if old_daily is not None and daily == old_daily[0]:
        daily = None
    if old_lifetime is not None and lifetime == old_lifetime[0]:
        lifetime = None
    if start == old_start:
        start = None
    if end == old_end:
        end = None
    if daily is None and lifetime is None and start is None and end is None:
        return ("no_change", "The ad set already has these values.")

    if start is not None:
        if status != "DRAFT" and old_start is not None and old_start <= today:
            return (
                "schedule_locked",
                "This ad set has already started, so its start date cannot change.",
            )
        if start < today:
            return ("invalid_change", "The start date is in the past.")
    if end is not None and end < today:
        return ("invalid_change", "The end date is in the past.")
    new_start = start or old_start
    new_end = end or old_end
    if new_start is not None and new_end is not None and new_start >= new_end:
        return ("invalid_change", "The start date must be before the end date.")

    order = [
        name
        for name, value in (("daily", daily), ("lifetime", lifetime))
        if value is not None
    ]
    dates = [
        name for name, value in (("start", start), ("end", end)) if value is not None
    ]
    # Every intermediate schedule must stay valid: moving both dates past the
    # old end needs the end to move first.
    if (
        len(dates) == 2
        and start is not None
        and old_end is not None
        and start >= old_end
    ):
        dates.reverse()
    return CampaignEditPlan(daily, lifetime, start, end, tuple(order + dates))


def refuse_invalid_campaign_edit(
    *,
    daily_budget: str | None,
    lifetime_budget: str | None,
    start_date: date | None,
    end_date: date | None,
    today: date,
) -> str | None:
    """Why the caller's edit cannot be valid for any ad set, or None."""
    if all(
        value is None for value in (daily_budget, lifetime_budget, start_date, end_date)
    ):
        return (
            "Nothing to change. Pass daily_budget, lifetime_budget, start_date "
            "or end_date."
        )
    for value in (daily_budget, lifetime_budget):
        if value is not None:
            try:
                parse_budget(value)
            except ValueError as exc:
                return str(exc)
    if start_date is not None and start_date < today:
        return "start_date is in the past."
    if end_date is not None and end_date < today:
        return "end_date is in the past."
    if start_date is not None and end_date is not None and start_date >= end_date:
        return "start_date must be before end_date."
    return None


# Targeting


def _urn_of(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    record = _mapping(value)
    urn = record.get("urn") if record is not None else None
    return urn if isinstance(urn, str) and urn else None


def _facets(or_value: Any) -> list[tuple[str, list[str]]] | None:
    """``(facetUrn, segmentUrns)`` of one OR, decorated or undecorated."""
    if isinstance(or_value, Mapping):
        if "facet" in or_value:
            or_value = [or_value]
        else:
            # Undecorated: ``{facetUrn: [segmentUrn, ...]}``.
            facets: list[tuple[str, list[str]]] = []
            for facet, segments in or_value.items():
                if not isinstance(segments, list):
                    return None
                urns = [_urn_of(segment) for segment in segments]
                if any(urn is None for urn in urns):
                    return None
                facets.append((facet, [urn for urn in urns if urn is not None]))
            return facets
    if not isinstance(or_value, list):
        return None
    facets = []
    for entry in or_value:
        record = _mapping(entry)
        facet = _urn_of(record.get("facet")) if record is not None else None
        segments = record.get("segments") if record is not None else None
        if facet is None or not isinstance(segments, list):
            return None
        urns = [_urn_of(segment) for segment in segments]
        if any(urn is None for urn in urns):
            return None
        facets.append((facet, [urn for urn in urns if urn is not None]))
    return facets


def targeting_keys(criteria: Any) -> list[TargetingKey] | None:
    """Every targeted value as a key, in record order, or None on a wrong shape.

    Include ANDs map to the UI's blocks: the locations block, the profile
    language and, in order, the OR blocks ``include_or:<k>``. A block that
    mixes locations with other facets is not a shape the UI makes, so it is
    refused rather than guessed.
    """
    record = _mapping(criteria)
    if record is None:
        return None
    include = record.get("include")
    exclude = record.get("exclude")
    include_record = _mapping(include) if include is not None else {}
    exclude_record = _mapping(exclude) if exclude is not None else {}
    if include_record is None or exclude_record is None:
        return None
    ands = include_record.get("and", [])
    if not isinstance(ands, list):
        return None
    keys: list[TargetingKey] = []
    or_index = 0
    for block in ands:
        block_record = _mapping(block)
        facets = _facets(block_record.get("or")) if block_record is not None else None
        if facets is None:
            return None
        kinds = {
            "locations"
            if facet in _LOCATION_FACETS
            else "language"
            if facet == _LANGUAGE_FACET
            else "or"
            for facet, _ in facets
        }
        if len(kinds) > 1:
            return None
        if kinds == {"locations"}:
            section = "include_locations"
        elif kinds == {"language"}:
            section = "include_language"
        else:
            section = f"include_or:{or_index}"
            or_index += 1
        keys.extend((section, facet, urn) for facet, urns in facets for urn in urns)
    excluded = _facets(exclude_record.get("or", []))
    if excluded is None:
        return None
    for facet, urns in excluded:
        section = "exclude_locations" if facet in _LOCATION_FACETS else "exclude_facets"
        keys.extend((section, facet, urn) for urn in urns)
    return keys


def pill_index(keys: Sequence[TargetingKey], section: str, urn: str) -> int:
    """Position of ``urn`` among its section's pills, which follow record order."""
    in_section = [key[2] for key in keys if key[0] == section]
    if urn not in in_section:
        raise ValueError(f"{urn} is not in {section}.")
    return in_section.index(urn)


@dataclass(frozen=True)
class TargetingPlan:
    """``removes`` run first, each block from its last pill down; then ``adds``."""

    original: tuple[TargetingKey, ...]
    target: frozenset[TargetingKey]
    removes: tuple[TargetingKey, ...]
    adds: tuple[TargetingKey, ...]


def _is_include(section: str) -> bool:
    return section.startswith("include")


def _renumbered(keys: Iterable[TargetingKey]) -> frozenset[TargetingKey]:
    """Close the gaps an emptied OR block leaves, as the saved record does."""
    keys = list(keys)
    used = sorted(
        {int(key[0].split(":")[1]) for key in keys if key[0].startswith("include_or:")}
    )
    position = {old: new for new, old in enumerate(used)}
    return frozenset(
        (f"include_or:{position[int(key[0].split(':')[1])]}", key[1], key[2])
        if key[0].startswith("include_or:")
        else key
        for key in keys
    )


def plan_targeting(
    record_tc: Any,
    *,
    add: Sequence[tuple[str, str, bool]],
    remove: Sequence[str],
) -> TargetingPlan | Refusal:
    """The targeting change, from ``(urn, facetUrn, exclude)`` adds and URN removes.

    An add goes to the OR block that already holds its facet, else the first
    one; locations go to their own include or exclude block.
    """
    original = targeting_keys(record_tc)
    if original is None:
        return ("not_editable", "The ad set's targeting could not be read.")
    if len(set(remove)) != len(remove):
        return ("invalid_change", "A URN is listed twice in remove.")
    removes: list[TargetingKey] = []
    for urn in remove:
        found = [key for key in original if key[2] == urn]
        if not found:
            return (
                "value_not_present",
                f"{urn} is not in this ad set's targeting, so it cannot be removed.",
            )
        if len(found) > 1:
            return (
                "invalid_change",
                f"{urn} is both included and excluded; remove it in Campaign Manager.",
            )
        removes.append(found[0])
    removed = {key[2] for key in removes}
    adds: list[TargetingKey] = []
    for urn, facet, exclude in add:
        if urn in removed:
            return ("invalid_change", f"{urn} is both added and removed.")
        if facet == _LANGUAGE_FACET:
            return (
                "invalid_change",
                "Profile language is not changed through targeting values.",
            )
        present = [key for key in original if key[2] == urn]
        if any(_is_include(key[0]) == (not exclude) for key in present):
            continue
        if present:
            return (
                "invalid_change",
                f"{urn} is already {'included' if exclude else 'excluded'}; "
                "remove it first.",
            )
        if facet in _LOCATION_FACETS:
            section = "exclude_locations" if exclude else "include_locations"
        elif exclude:
            section = "exclude_facets"
        else:
            section = next(
                (
                    key[0]
                    for key in original
                    if key[0].startswith("include_or:") and key[1] == facet
                ),
                "include_or:0",
            )
        key = (section, facet, urn)
        if key not in adds:
            adds.append(key)
    if not removes and not adds:
        return ("no_change", "The ad set's targeting already matches.")
    removes.sort(key=lambda key: (key[0], -pill_index(original, key[0], key[2])))
    target = _renumbered([key for key in original if key not in removes] + adds)
    return TargetingPlan(tuple(original), target, tuple(removes), tuple(adds))


def _invalid_text(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def refuse_invalid_targeting_change(
    add: Sequence[Mapping[str, Any]] | None, remove: Sequence[str] | None
) -> str | None:
    """Why ``add``/``remove`` cannot be a targeting change, or None."""
    add = add or []
    remove = remove or []
    total = len(add) + len(remove)
    if not 1 <= total <= _MAX_TARGETING_ITEMS:
        return (
            f"Pass between 1 and {_MAX_TARGETING_ITEMS} values in add and remove "
            f"together; got {total}."
        )
    for item in add:
        query = item.get("query")
        if not isinstance(query, str) or not 1 <= len(query) <= 100:
            return "Every add needs the query, 1 to 100 characters, that found it."
        if _invalid_text(query) or not query.strip():
            return "A query must be plain text without control characters."
        exclude = item.get("exclude", False)
        if not isinstance(exclude, bool):
            return "exclude must be true or false."
    urns = [item.get("urn") for item in add] + list(remove)
    for urn in urns:
        try:
            normalize_targeting_urn(urn if isinstance(urn, str) else "")
        except InvalidReferenceError as exc:
            return str(exc)
    return None


# Audiences and drafts


def refuse_invalid_audience_name(name: str, *, saved: bool) -> str | None:
    """Saved audience names are capped at 25 characters (the form's maxlength)."""
    limit = SAVED_AUDIENCE_NAME_MAX if saved else LIST_AUDIENCE_NAME_MAX
    if not name.strip() or len(name) > limit:
        return f"The audience name must be 1 to {limit} characters."
    if _invalid_text(name):
        return "The audience name must not contain control characters."
    return None


def refuse_invalid_draft(objective: str, name: str) -> str | None:
    if not _OBJECTIVE_RE.fullmatch(objective):
        return (
            "objective must be a Campaign Manager objective such as WEBSITE_VISIT, "
            "in capitals."
        )
    if not name.strip() or len(name) > 255:
        return "The ad set name must be 1 to 255 characters."
    if _invalid_text(name):
        return "The ad set name must not contain control characters."
    return None


# Admission rules: None admits the request, a string is why it was refused.


class _Unreadable(Exception):
    pass


def _plain(value: Any) -> Any:
    """``value`` without the normalized response's ``$type`` annotations."""
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items() if k != "$type"}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _schedule_dates(schedule: Any) -> tuple[date, date | None]:
    record = _mapping(schedule)
    start = _ms(record.get("start")) if record is not None else None
    if record is None or start is None:
        raise _Unreadable
    end = record.get("end")
    if end is not None and _ms(end) is None:
        raise _Unreadable
    return ms_to_date(start), None if end is None else ms_to_date(end)


@dataclass(frozen=True)
class _Comparer:
    record: Mapping[str, Any]

    def sent(self, field: str, value: Any) -> Any:
        """A request value in the form it is compared in."""
        if value is None:
            return None
        if field in _MONEY_FIELDS:
            money = _money_value(value)
            if money is None:
                raise _Unreadable
            return money
        if field == "runSchedule":
            return _schedule_dates(value)
        if field == "targetingCriteria":
            keys = targeting_keys(value)
            if keys is None:
                raise _Unreadable
            return frozenset(keys)
        return _plain(value)

    def original(self, field: str) -> Any:
        value = self.record.get(field)
        if value is None:
            return None
        if field == "targetingCriteria":
            keys = targeting_keys(value)
            return frozenset(keys) if keys is not None else None
        try:
            return self.sent(field, value)
        except _Unreadable:
            return None

    def planned(self, field: str, value: Any) -> Any:
        """A plan value (Decimal, ``{start, end}`` dates, keys) in compared form."""
        if value is None:
            return None
        if field in _MONEY_FIELDS:
            currency = (_money_value(self.record.get(field)) or (None, None))[1]
            return (Decimal(value), currency)
        if field == "runSchedule":
            return (value["start"], value.get("end"))
        if field == "targetingCriteria":
            return frozenset(value)
        return _plain(value)


def _on_path(field: str, sent: Any, original: Any, target: Any) -> bool:
    """Whether ``sent`` lies between the original and the target value."""
    if field == "runSchedule":
        if original is None:
            return False
        start, end = sent
        if start not in (original[0], target[0]) or end not in (original[1], target[1]):
            return False
        return end is None or start < end
    if field == "targetingCriteria":
        if original is None:
            return False
        return original & target <= sent <= original | target
    return sent in (original, target)


def _same_id(value: Any, expected: str, urn_prefix: str) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return str(value) == expected
    return isinstance(value, str) and value in (expected, urn_prefix + expected)


def _restli(req: WriteRequest) -> str:
    return (req.restli_method or "").lower()


def _refuse_target(
    req: WriteRequest, resource: str, entity_id: str | None, restli: str | None
) -> str | None:
    if req.method.upper() != "POST" or req.call.resource != resource:
        return f"not a planned write ({req.method} {req.call.resource})"
    if req.call.entity_id != entity_id:
        return f"{resource} write for another entity"
    if restli is not None and _restli(req) != restli:
        return f"{resource} {_restli(req) or 'request'} is not the planned {restli}"
    return None


def _effective_schedule(patch: Mapping[str, Any], record: Mapping[str, Any]) -> Any:
    """The ``runSchedule`` a patch leaves, from either form the app sends."""
    nested = patch.get("runSchedule")
    in_set = "runSchedule" in (patch.get("$set") or {})
    if nested is None:
        return patch["$set"]["runSchedule"] if in_set else None
    nested = _mapping(nested)
    if nested is None or in_set or set(nested) - {"$set", "$delete"}:
        raise _Unreadable
    schedule = dict(_mapping(record.get("runSchedule")) or {})
    updates = _mapping(nested.get("$set", {}))
    deletes = nested.get("$delete", [])
    if updates is None or not isinstance(deletes, list):
        raise _Unreadable
    if set(updates) - {"start", "end"} or set(deletes) - {"end"}:
        raise _Unreadable
    schedule.update(updates)
    for key in deletes:
        schedule.pop(key, None)
    return schedule


def admit_campaign_patch(
    req: WriteRequest,
    *,
    campaign_id: str,
    record: Mapping[str, Any],
    allowed: Mapping[str, Any],
    path_fields: Collection[str] = (),
) -> str | None:
    """Admit the app's partial update of ``campaign_id`` only if it is on plan.

    ``allowed`` maps each planned field to its target: a ``Decimal`` for a
    budget (the record's currency), ``{"start": date, "end": date | None}``
    for ``runSchedule`` (compared as UTC dates), a collection of ``TargetingKey`` for
    ``targetingCriteria``, the plain value otherwise, ``None`` to delete.
    Every other field the app sends must equal the record (a no-op). A field
    in ``path_fields`` may also hold a state between original and target, so
    an autosave between two steps of one change is admitted.
    """
    refusal = _refuse_target(
        req, "campaignManagerCampaigns", campaign_id, "partial_update"
    )
    if refusal is not None:
        return refusal
    body = _mapping(req.body)
    patch = _mapping(body.get("patch")) if body is not None else None
    if body is None or patch is None or set(body) != {"patch"}:
        return "campaign update is not a Rest.li patch"
    if set(patch) - {"$set", "$delete", "runSchedule"}:
        return "campaign update changes nested fields that were not planned"
    updates = _mapping(patch.get("$set", {}))
    deletes = patch.get("$delete", [])
    if updates is None or not isinstance(deletes, list):
        return "campaign update is not a Rest.li patch"
    compare = _Comparer(record)
    try:
        fields = {key: value for key, value in updates.items() if key != "runSchedule"}
        if "runSchedule" in updates or "runSchedule" in patch:
            fields["runSchedule"] = _effective_schedule(patch, record)
        for field, value in fields.items():
            if field in _BOOKKEEPING_KEYS:
                continue
            if field == "status":
                return "status change inside a campaign edit"
            sent = compare.sent(field, value)
            original = compare.original(field)
            if sent == original:
                continue
            if field not in allowed:
                return f"unplanned change to {field}"
            target = compare.planned(field, allowed[field])
            if sent == target:
                continue
            if field in path_fields and _on_path(field, sent, original, target):
                continue
            return f"{field} is not the planned value"
        for field in deletes:
            if not isinstance(field, str):
                return "campaign update is not a Rest.li patch"
            if record.get(field) is None or (
                field in allowed and allowed[field] is None
            ):
                continue
            return f"unplanned removal of {field}"
    except (_Unreadable, ValueError, InvalidOperation, KeyError, TypeError):
        return "campaign update holds a value that could not be read"
    return None


def admit_status_patch(
    req: WriteRequest, *, campaign_id: str, from_status: str, to_status: str
) -> str | None:
    """Admit only the planned pause or resume; never a launch from draft."""
    if (from_status, to_status) not in {("ACTIVE", "PAUSED"), ("PAUSED", "ACTIVE")}:
        return f"status change {from_status} to {to_status} is not allowed"
    refusal = _refuse_target(
        req, "campaignManagerCampaigns", campaign_id, "partial_update"
    )
    if refusal is not None:
        return refusal
    if req.body != {"patch": {"$set": {"status": to_status}}}:
        return "status update changes more than the planned status"
    return None


def admit_campaign_create(*, account_id: str, group_id: str, objective: str) -> Admit:
    """An admission for one DRAFT create in the planned group with the objective.

    Stateful like ``admit_list_upload``: a second create would be a second
    draft, so only the first matching create passes.
    """
    created = False

    def admit(req: WriteRequest) -> str | None:
        nonlocal created
        refusal = _refuse_target(req, "campaignManagerCampaigns", None, "create")
        if refusal is not None:
            return refusal
        if created:
            return "second campaignManagerCampaigns create"
        body = _mapping(req.body) or {}
        if body.get("status") != "DRAFT":
            return "campaign create is not a draft"
        if not _same_id(body.get("accountId"), account_id, _ACCOUNT_URN):
            return "campaign create for another ad account"
        if not _same_id(body.get("campaignGroupId"), group_id, _CAMPAIGN_GROUP_URN):
            return "campaign create in another campaign group"
        if body.get("objectiveType") != objective:
            return "campaign create with another objective"
        created = True
        return None

    return admit


def admit_creative_create(
    req: WriteRequest, *, campaign_id: str, reference: str
) -> str | None:
    """Admit the app's creative for ``reference`` on ``campaign_id`` only."""
    refusal = _refuse_target(req, "campaignManagerCreatives", None, "create")
    if refusal is not None:
        return refusal
    body = _mapping(req.body) or {}
    if not _same_id(body.get("campaignId"), campaign_id, _CAMPAIGN_URN):
        return "creative create for another ad set"
    if body.get("reference") != reference:
        return "creative create for another post"
    return None


def admit_list_upload(*, account_id: str, name: str) -> Admit:
    """An admission for one company list upload: one signed URL, one segment.

    Returns a stateful rule because the second of either request would be a
    second upload or a second audience.
    """
    owner = _ACCOUNT_URN + account_id
    seen: set[str] = set()

    def admit(req: WriteRequest) -> str | None:
        resource = req.call.resource
        if resource not in {
            "campaignManagerAmbrySignedUrl",
            "campaignManagerDmpSegments",
        }:
            return f"not a planned write ({req.method} {resource})"
        refusal = _refuse_target(req, resource, None, None)
        if refusal is not None:
            return refusal
        if resource in seen:
            return f"second {resource} request"
        body = _mapping(req.body) or {}
        if resource == "campaignManagerAmbrySignedUrl":
            if (
                body.get("owner") != owner
                or body.get("uploadType") != "MATCHED_AUDIENCE"
                or body.get("requestType") != "UPLOAD"
            ):
                return "upload URL request is not this ad account's list upload"
        elif (
            body.get("account") != owner
            or body.get("name") != name
            or body.get("sourcePlatform") != "LIST_UPLOAD"
            or body.get("segmentType") != "COMPANY"
            or "sourceType" in body
        ):
            return "audience create is not the planned company list"
        seen.add(resource)
        return None

    return admit


def admit_saved_audience(
    req: WriteRequest,
    *,
    account_id: str,
    name: str,
    target_keys: Collection[TargetingKey],
) -> str | None:
    """Admit the saved audience create only with the ad set's own targeting."""
    refusal = _refuse_target(req, "campaignManagerTargetingCriteria", None, None)
    if refusal is not None:
        return refusal
    body = _mapping(req.body) or {}
    if body.get("account") != _ACCOUNT_URN + account_id:
        return "saved audience for another ad account"
    if body.get("name") != name or body.get("ownership") != "ACCOUNT":
        return "saved audience is not the planned one"
    keys = targeting_keys(
        {"include": body.get("include"), "exclude": body.get("exclude")}
    )
    if keys is None or frozenset(keys) != frozenset(target_keys):
        return "saved audience targeting differs from the ad set's"
    return None
