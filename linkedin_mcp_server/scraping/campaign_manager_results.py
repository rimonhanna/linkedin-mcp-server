"""The result every Campaign Manager write tool returns, and its status vocabulary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from linkedin_mcp_server.scraping.campaign_manager_changes import WriteRequest

# The political-ad declaration stops of the two tools that save by Save and exit.
_DECLARATION = frozenset(
    {
        "declaration_required",
        "declaration_would_clear",
        "declaration_cleared_by_linkedin",
        "declaration_not_offered",
    }
)

_COMMON = frozenset(
    {
        "confirmation_required",
        "invalid_change",
        "target_not_found",
        "not_editable",
        "no_change",
        "control_unavailable",
        "change_blocked",
        "save_failed",
        "save_unverified",
        "partially_applied",
    }
)

STATUSES: Mapping[str, frozenset[str]] = {
    "update_campaign": _COMMON
    | _DECLARATION
    | {"updated", "budget_type_mismatch", "schedule_locked"},
    "set_campaign_status": _COMMON
    | {"paused", "resumed", "reactivation_needs_changes"},
    "update_campaign_targeting": _COMMON
    | _DECLARATION
    | {
        "updated",
        "entity_not_found",
        "entity_not_addable",
        "value_not_present",
        "deadline",
    },
    "create_campaign_draft": _COMMON
    | {"created", "no_campaign_group", "objective_unavailable"},
    "add_post_to_campaign": _COMMON
    | {
        "added",
        "post_not_found",
        "post_not_sponsorable",
        "campaign_active",
        "needs_more_details",
    },
    "create_company_list_audience": _COMMON
    | {"audience_created", "upload_form_unavailable"},
    "create_saved_audience": _COMMON | {"audience_created", "audience_too_small"},
}

_SUCCESS = frozenset(
    {
        "confirmation_required",
        "no_change",
        "updated",
        "paused",
        "resumed",
        "created",
        "added",
        "audience_created",
    }
)
# The statuses that keep no debug trace.
SUCCEEDED: Mapping[str, frozenset[str]] = {
    tool: statuses & _SUCCESS for tool, statuses in STATUSES.items()
}


class _Window(Protocol):
    @property
    def admitted(self) -> Sequence[object]: ...


def retry_safe_from(window: _Window) -> bool:
    """True only when nothing left the browser: no write was admitted."""
    return not window.admitted


def campaign_action_result(
    tool: str,
    url: str,
    status: str,
    message: str,
    *,
    retry_safe: bool,
    target: Mapping[str, Any] | None = None,
    planned: Mapping[str, Any] | None = None,
    applied: Mapping[str, Any] | None = None,
    created_id: str | None = None,
    refused: Sequence[tuple[WriteRequest, str]] | None = None,
) -> dict[str, Any]:
    """Build a Campaign Manager write tool's result.

    ``message`` is our own sentence; LinkedIn's localized text never decides
    anything. ``refused`` is the write window's list of aborted requests,
    reported as ``{method, resource, reason}``. A status outside the tool's
    vocabulary is a programming error and raises ``ValueError``.
    """
    if status not in STATUSES.get(tool, ()):
        raise ValueError(f"{status!r} is not a status of {tool!r}.")
    result: dict[str, Any] = {
        "url": url,
        "status": status,
        "message": message,
        "retry_safe": retry_safe,
    }
    for key, value in (
        ("target", target),
        ("planned", planned),
        ("applied", applied),
        ("created_id", created_id),
    ):
        if value is not None:
            result[key] = dict(value) if isinstance(value, Mapping) else value
    if refused:
        result["refused"] = [
            {"method": req.method, "resource": req.call.resource, "reason": reason}
            for req, reason in refused
        ]
    return result
