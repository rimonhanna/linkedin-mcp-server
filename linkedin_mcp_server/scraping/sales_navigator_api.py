"""Passive capture of the Sales Navigator API responses one page makes."""

from __future__ import annotations

from typing import Any

from linkedin_mcp_server.scraping.api_capture import (
    ApiResponseCapture,
    CapturedResponse,
)
from linkedin_mcp_server.scraping.sales_navigator_payload import (
    SnCall,
    parse_sn_call,
)

SnResponse = CapturedResponse[SnCall]


class SalesNavigatorCapture(ApiResponseCapture[SnCall]):
    """Collect Sales Navigator API responses emitted by one page.

    Only GET bodies of ``resources`` are read (Playwright decodes the Blob the
    app's XHR asks for); every other call is kept with its status alone, which
    is what the seat check needs.
    """

    label = "Sales Navigator"

    def __init__(self, page: Any, *, resources: frozenset[str]):
        self._resources = resources
        super().__init__(page)
        self.structure.want(resources)

    def _parse(self, response: Any) -> SnCall | None:
        return parse_sn_call(response.url, response.request.method)

    def _wants_body(self, call: SnCall) -> bool:
        return call.method == "GET" and call.resource in self._resources
