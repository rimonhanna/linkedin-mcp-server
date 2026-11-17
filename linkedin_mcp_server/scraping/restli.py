"""Pure helpers for LinkedIn's Rest.li payloads, plain and normalized.

A plain collection is ``{elements, paging}``; a normalized one is
``{data, included}`` with URN references into ``included``. Everything here
returns ``None`` (or an empty value) on a wrong shape and never raises.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def as_mapping(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def as_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def iso_from_ms(value: Any) -> str | None:
    millis = as_int(value)
    if millis is None:
        return None
    try:
        moment = datetime.fromtimestamp(millis / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


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
    record = as_mapping(payload)
    if record is None:
        return None
    elements = record.get("elements")
    if not isinstance(elements, list) or not all(
        isinstance(item, dict) for item in elements
    ):
        return None
    return [item for item in elements if isinstance(item, dict)]


def paging_total(payload: Any) -> int | None:
    """``paging.total`` of a plain Rest.li collection."""
    record = as_mapping(payload)
    paging = as_mapping(record.get("paging")) if record is not None else None
    return as_int(paging.get("total")) if paging is not None else None


def normalized_record(payload: Any) -> dict[str, Any] | None:
    record = as_mapping(payload)
    if record is None:
        return None
    return as_mapping(record.get("data"))


def included_by_urn(payload: Any) -> dict[str, dict[str, Any]]:
    """The ``included`` entities of a normalized payload, by ``entityUrn``."""
    record = as_mapping(payload)
    included: dict[str, dict[str, Any]] = {}
    for entity in as_dicts(record.get("included") if record is not None else None):
        urn = entity.get("entityUrn")
        if isinstance(urn, str):
            included[urn] = entity
    return included


def normalized_elements(payload: Any) -> list[dict[str, Any]] | None:
    """Entities of a normalized collection, resolving URN refs against ``included``.

    Elements are inline objects or URN references (``elements`` or
    ``*elements``); one reference that ``included`` does not hold refuses all.
    """
    record = as_mapping(payload)
    data = as_mapping(record.get("data")) if record is not None else None
    if data is None:
        return None
    elements = data.get("elements", data.get("*elements"))
    if not isinstance(elements, list):
        return None
    included = included_by_urn(record)
    resolved: list[dict[str, Any]] = []
    for element in elements:
        if isinstance(element, dict):
            resolved.append(element)
        elif isinstance(element, str) and element in included:
            resolved.append(included[element])
        else:
            return None
    return resolved


def _is_normalized(record: dict[str, Any]) -> bool:
    return "data" in record and "elements" not in record


def envelope_elements(payload: Any) -> list[dict[str, Any]] | None:
    """A collection's entities, plain ``{elements}`` or normalized ``{data, included}``.

    LinkedIn picks the envelope per resource (by the request's ``Accept``), so
    every collection reader goes through here.
    """
    record = as_mapping(payload)
    if record is None:
        return None
    if _is_normalized(record):
        return normalized_elements(record)
    return plain_elements(record)


def envelope_paging(payload: Any) -> dict[str, Any] | None:
    """A collection's ``paging``, from either envelope."""
    record = as_mapping(payload)
    if record is None:
        return None
    if _is_normalized(record):
        data = as_mapping(record.get("data"))
        return as_mapping(data.get("paging")) if data is not None else None
    return as_mapping(record.get("paging"))


def envelope_total(payload: Any) -> int | None:
    """``paging.total`` of a collection in either envelope."""
    paging = envelope_paging(payload)
    return as_int(paging.get("total")) if paging is not None else None


def envelope_record(payload: Any) -> dict[str, Any] | None:
    """A single entity: normalized ``{data: {...}}``, a ``data`` URN resolved
    through ``included``, or a plain object. A collection is not a record.
    """
    record = as_mapping(payload)
    if record is None or "elements" in record:
        return None
    if "data" not in record:
        return record
    data = record.get("data")
    if isinstance(data, str):
        return included_by_urn(record).get(data)
    data = as_mapping(data)
    if data is None or "elements" in data or "*elements" in data:
        return None
    return data


def entity_id(value: Any) -> int | None:
    """An entity id sent as a number or as a string of digits; nothing else."""
    number = as_int(value)
    if number is not None:
        return number
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def same_id(value: Any, ref: str) -> bool:
    """Whether ``value`` is the numeric id ``ref`` names."""
    number = entity_id(value)
    return number is not None and ref.isdigit() and number == int(ref)
