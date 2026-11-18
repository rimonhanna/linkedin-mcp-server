"""Only the Rest.li forms that filter by status mark a reporting list as narrowed.

Synthetic query params only.
"""

from __future__ import annotations

import pytest

from linkedin_mcp_server.scraping.campaign_manager import _status_filtered
from linkedin_mcp_server.scraping.campaign_manager_payload import CmCall


def _call(**params: str) -> CmCall:
    return CmCall("campaignManagerReportingCampaigns", None, "search", params)


@pytest.mark.parametrize(
    "params",
    [
        {"search": "(status:(values:List(ACTIVE,PAUSED)))"},
        {"search": "(account:(values:List(x)),status:(values:List(ACTIVE)))"},
        {"search": "(statusV2:(values:List(ACTIVE)))"},
        {"statuses": "List(ACTIVE)"},
        {"status": "ACTIVE"},
        {"search.status.values[0]": "ACTIVE"},
    ],
)
def test_a_status_filter_form_is_filtered(params):
    assert _status_filtered(_call(**params)) is True, params


@pytest.mark.parametrize(
    "params",
    [
        {"fields": "id,name,status"},
        {"sortBy": "status"},
        {"search": "(name:(values:List(status)))"},
        {"projection": "(id,status)"},
        {"q": "search", "count": "25"},
    ],
)
def test_a_param_that_only_names_status_is_not_filtered(params):
    assert _status_filtered(_call(**params)) is False, params
