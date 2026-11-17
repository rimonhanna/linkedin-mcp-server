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


def normalized_elements(payload: Any) -> list[dict[str, Any]] | None:
    """Entities of a normalized collection, resolving URN refs against ``included``."""
    record = as_mapping(payload)
    data = as_mapping(record.get("data")) if record is not None else None
    if record is None or data is None:
        return None
    elements = data.get("elements")
    if not isinstance(elements, list):
        return None
    included: dict[str, dict[str, Any]] = {}
    for entity in as_dicts(record.get("included")):
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
