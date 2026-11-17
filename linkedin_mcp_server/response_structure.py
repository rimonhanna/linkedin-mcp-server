"""Value-free records of the API responses a capture saw, for failure notes.

A read that refuses a response shape or times out says nothing about what
LinkedIn actually sent. These records keep exactly that, minus every value:
strings, numbers and booleans become their type name, a list becomes its length
and the skeleton of its first element, and a key that is not a plain API field
name (an id, URN, email or number used as a map key) is dropped and counted.
Bounded per record and per capture, so a large page cannot bloat the notes.
"""

from __future__ import annotations

from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import re

MAX_RECORDS = 30
MAX_DEPTH = 4
MAX_KEYS = 25
#: Skeleton nodes per record: bounds the work and the size together.
MAX_NODES = 150
_MAX_HEADER = 80

# Field names: letters first, optionally behind Rest.li's ``$`` or ``*``.
_FIELD_NAME = re.compile(r"[$*]?[A-Za-z_][A-Za-z0-9_.\-]{0,63}")
_DIGIT_RUN = re.compile(r"\d{3,}")
_HEX_RUN = re.compile(r"[0-9a-fA-F\-]{12,}")
_ID_KEYS = "<id-like key>"
_TRUNCATED = "..."


def is_field_name(key: object) -> bool:
    """Whether ``key`` reads as an API field name, not as data used as a key."""
    if not isinstance(key, str) or not _FIELD_NAME.fullmatch(key):
        return False
    return not (_DIGIT_RUN.search(key) or _HEX_RUN.search(key))


def skeleton(value: Any) -> Any:
    """``value`` with every scalar replaced by its type name, bounded."""
    budget = [MAX_NODES]
    return _skeleton(value, 0, budget)


def _skeleton(value: Any, depth: int, budget: list[int]) -> Any:
    budget[0] -= 1
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, dict):
        if depth >= MAX_DEPTH or budget[0] <= 0:
            return f"dict({len(value)} keys)"
        out: dict[str, Any] = {}
        dropped = 0
        first_dropped: Any = None
        for key, item in value.items():
            if not is_field_name(key):
                if dropped == 0:
                    first_dropped = item
                dropped += 1
                continue
            if len(out) >= MAX_KEYS or budget[0] <= 0:
                out[_TRUNCATED] = f"{len(value)} keys in all"
                break
            out[key] = _skeleton(item, depth + 1, budget)
        if dropped:
            out[f"{_ID_KEYS} x{dropped}"] = _skeleton(first_dropped, depth + 1, budget)
        return out
    if isinstance(value, (list, tuple)):
        if not value:
            return "list[0]"
        if depth >= MAX_DEPTH or budget[0] <= 0:
            return f"list[{len(value)}]"
        return {f"list[{len(value)}]": _skeleton(value[0], depth + 1, budget)}
    return type(value).__name__


def _header(headers: Any, name: str) -> str | None:
    if not isinstance(headers, dict):
        return None
    for key, value in headers.items():
        if isinstance(key, str) and key.casefold() == name and isinstance(value, str):
            return value[:_MAX_HEADER]
    return None


def response_record(
    call: Any, response: Any, *, body: str, payload: Any = None
) -> dict[str, Any]:
    """One response's record. ``body`` is ``json``, ``not json`` or ``not read``."""
    request = getattr(response, "request", None)
    record: dict[str, Any] = {
        "resource": getattr(call, "resource", None),
        "finder": getattr(call, "finder", None),
        "entity_id": getattr(call, "entity_id", None) is not None,
        "method": getattr(request, "method", None),
        "status": getattr(response, "status", None),
        "content_type": _header(getattr(response, "headers", None), "content-type"),
        "accept": _header(getattr(request, "headers", None), "accept"),
        "body": body,
    }
    if body == "json":
        record["skeleton"] = skeleton(payload)
    return record


@dataclass(slots=True)
class ResponseLog:
    """The most recent records of one capture, and how many it saw in all."""

    label: str
    records: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=MAX_RECORDS)
    )
    seen: int = 0

    def add(self, record: dict[str, Any]) -> None:
        self.records.append(record)
        self.seen += 1


# Set by the capture on entry, in the task that runs the tool, and read by
# the failure notes built in that same task; the latest capture wins.
_current: ContextVar[ResponseLog | None] = ContextVar(
    "api_response_structure", default=None
)


def track(log: ResponseLog) -> None:
    _current.set(log)


def current_log() -> ResponseLog | None:
    return _current.get()
