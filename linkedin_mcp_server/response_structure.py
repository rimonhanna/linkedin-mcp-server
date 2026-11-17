"""Value-free records of the API responses a capture saw, for failure notes.

A read that refuses a response shape or times out says nothing about what
LinkedIn actually sent. These records keep exactly that, minus every value:
strings, numbers and booleans become their type name, a list becomes its length
and the skeleton of its first element, and a key that is not a plain API field
name (an id, URN, email or number used as a map key) is dropped and counted.
Bounded per record and per capture, so a large page cannot bloat the notes.

The notes show the most recent records plus every kept record of the
resources the failing read was waiting for, so the response a read refused or
never matched is shown however many others came after it. Request parameters
are kept as structure too: Rest.li syntax with every digit run masked, or just
the value's type.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import re

MAX_RECORDS = 30
#: Older records of the awaited resources shown on top of the recent ones.
MAX_WANTED = 40
#: Records a capture keeps to pick the awaited ones from.
MAX_KEPT = 400
MAX_DEPTH = 4
MAX_KEYS = 25
#: Skeleton nodes per record: bounds the work and the size together.
MAX_NODES = 150
#: Page structure summaries kept per capture.
MAX_PAGES = 5
_MAX_HEADER = 80

# Field names: letters first, optionally behind Rest.li's ``$`` or ``*``.
_FIELD_NAME = re.compile(r"[$*]?[A-Za-z_][A-Za-z0-9_.\-]{0,63}")
_DIGIT_RUN = re.compile(r"\d{3,}")
_HEX_RUN = re.compile(r"[0-9a-fA-F\-]{12,}")
_ID_KEYS = "<id-like key>"
_TRUNCATED = "..."
# Parameters whose value is Rest.li structure (finder, pivots, projection,
# date range, scope). Any other one (search text, keywords) shows its type only.
_STRUCTURE_PARAMS = frozenset(
    {
        "q",
        "pivot",
        "pivots",
        "timeGranularity",
        "fields",
        "projection",
        "dateRange",
        "accountIds",
        "campaignGroupIds",
        "campaignIds",
        "creativeIds",
        "start",
        "count",
        "decoration",
        "decorationId",
    }
)
_DIGITS = re.compile(r"\d+")
_MAX_PARAM = 200


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


def params_shape(params: Any) -> dict[str, str] | None:
    """A request's parameters with every value masked or reduced to its type."""
    if not isinstance(params, Mapping):
        return None
    shape: dict[str, str] = {}
    for key, value in params.items():
        if not is_field_name(key):
            continue
        if key in _STRUCTURE_PARAMS and isinstance(value, str):
            masked = _DIGITS.sub("N", _HEX_RUN.sub("H", value))
            shape[key] = masked[:_MAX_PARAM]
        else:
            shape[key] = type(value).__name__
    return shape


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
    params = params_shape(getattr(call, "params", None))
    if params is not None:
        record["params"] = params
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
    #: Resources a read of this capture waited for.
    wanted: set[str] = field(default_factory=set)
    _kept: deque[tuple[int, dict[str, Any]]] = field(
        default_factory=lambda: deque(maxlen=MAX_KEPT)
    )
    #: Which known page anchors matched when a control did not render.
    pages: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=MAX_PAGES)
    )

    def add(self, record: dict[str, Any]) -> None:
        self._kept.append((self.seen, record))
        self.records.append(record)
        self.seen += 1

    def add_page(self, summary: dict[str, Any]) -> None:
        self.pages.append(summary)

    def want(self, resources: Iterable[str]) -> None:
        self.wanted.update(resources)

    def shown(self) -> list[dict[str, Any]]:
        """Older records of the wanted resources, then the recent ones, in order."""
        recent_from = self.seen - len(self.records)
        older = [
            record
            for index, record in self._kept
            if index < recent_from and record.get("resource") in self.wanted
        ]
        return older[-MAX_WANTED:] + list(self.records)


# Set by the capture on entry, in the task that runs the tool, and read by
# the failure notes built in that same task; the latest capture wins.
_current: ContextVar[ResponseLog | None] = ContextVar(
    "api_response_structure", default=None
)


def track(log: ResponseLog) -> None:
    _current.set(log)


def current_log() -> ResponseLog | None:
    return _current.get()
