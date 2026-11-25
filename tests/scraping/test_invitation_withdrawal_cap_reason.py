"""A withdrawal run stopped by a cap says action_limit, not the schedule.

Withdrawals have no daily cap of their own today (``kind_caps``), only the
schedule and the hourly cap, so the cap here is a refusal of that shape: what
the run reports if one is ever added.
"""

from __future__ import annotations

from datetime import datetime, timezone

from linkedin_mcp_server.exceptions import ActionLimitError
from linkedin_mcp_server.pacing import WITHDRAWALS

from .test_invitation_withdrawal import ROWS, _withdrawer
from .test_invitation_withdrawal import hooks  # noqa: F401  (autouse fixture)


async def test_a_spent_daily_cap_stops_as_action_limit(hooks):  # noqa: F811
    hooks.admit.side_effect = ActionLimitError(
        WITHDRAWALS,
        limit=20,
        window="24 h",
        resume_at=datetime(2026, 8, 6, 10, 0, tzinfo=timezone.utc),
    )

    withdrawer, _, clicks = _withdrawer(ROWS)
    result = await withdrawer.withdraw_invitations(7, dry_run=False)

    assert clicks == []
    assert result["stopped_reason"].startswith(
        "action_limit: limit_exceeded: withdrawals"
    )
