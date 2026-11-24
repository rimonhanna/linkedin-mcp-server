"""Passive capture of the responses the "Invite to follow" dialog receives."""

from __future__ import annotations

from typing import Any

from linkedin_mcp_server.scraping.api_capture import (
    ApiResponseCapture,
    CapturedResponse,
)
from linkedin_mcp_server.scraping.page_invite_payload import (
    CREDITS,
    STATUSES,
    SUGGESTIONS,
    TYPEAHEAD,
    InviteCall,
    parse_invite_call,
)

InviteResponse = CapturedResponse[InviteCall]


class PageInviteCapture(ApiResponseCapture[InviteCall]):
    """Collect the dialog's candidate, status, credit and invitation responses."""

    label = "LinkedIn page admin"

    def __init__(self, page: Any):
        super().__init__(page)
        self.structure.want((SUGGESTIONS, STATUSES, CREDITS, TYPEAHEAD))

    def _parse(self, response: Any) -> InviteCall | None:
        return parse_invite_call(response.url, response.request.method)
