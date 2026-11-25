"""Passive capture of the responses the "Invite to follow" dialog receives."""

from __future__ import annotations

from typing import Any

from linkedin_mcp_server.scraping.api_capture import (
    ApiResponseCapture,
    CapturedResponse,
)
from linkedin_mcp_server.scraping.page_invite_payload import (
    CREDITS,
    INVITATIONS,
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

    def _on_response(self, response: Any) -> None:
        super()._on_response(response)
        # The base capture keeps no 429 (it only notes the throttle); a send
        # answered 429 is kept, unread, so the invite reports a rate limit
        # instead of waiting out the send timeout.
        if getattr(response, "status", 0) != 429:
            return
        call = self._parse(response)
        if call is not None and call.resource == INVITATIONS and call.method == "POST":
            self.responses.append(CapturedResponse(call, 429, None))
            self._event.set()
