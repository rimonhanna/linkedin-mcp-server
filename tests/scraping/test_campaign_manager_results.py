"""Tests for the Campaign Manager write result builder."""

from dataclasses import dataclass, field

import pytest

from linkedin_mcp_server.scraping.campaign_manager_payload import CmCall
from linkedin_mcp_server.scraping.campaign_manager_results import (
    STATUSES,
    SUCCEEDED,
    campaign_action_result,
    retry_safe_from,
)

URL = "https://www.linkedin.com/campaignmanager/accounts/100000001"


@dataclass(frozen=True)
class Req:
    call: CmCall
    method: str = "POST"
    restli_method: str | None = None
    body: object = None
    url: str = ""


@dataclass
class Window:
    admitted: list = field(default_factory=list)
    refused: list = field(default_factory=list)


def test_result_shape_and_refused_requests():
    req = Req(CmCall("billingConfiguration", None, None, {}))
    result = campaign_action_result(
        "update_campaign",
        URL,
        "change_blocked",
        "Campaign Manager tried to save something that was not planned.",
        retry_safe=True,
        target={"kind": "campaign", "id": 100000002},
        planned={"dailyBudget": "12.5"},
        refused=[(req, "not a planned write")],
    )
    assert result == {
        "url": URL,
        "status": "change_blocked",
        "message": "Campaign Manager tried to save something that was not planned.",
        "retry_safe": True,
        "target": {"kind": "campaign", "id": 100000002},
        "planned": {"dailyBudget": "12.5"},
        "refused": [
            {
                "method": "POST",
                "resource": "billingConfiguration",
                "reason": "not a planned write",
            }
        ],
    }


def test_result_omits_absent_fields_and_keeps_created_id():
    result = campaign_action_result(
        "create_campaign_draft",
        URL,
        "created",
        "Draft created.",
        retry_safe=False,
        created_id="100000004",
        refused=[],
    )
    assert result == {
        "url": URL,
        "status": "created",
        "message": "Draft created.",
        "retry_safe": False,
        "created_id": "100000004",
    }


def test_result_rejects_unknown_status_and_tool():
    with pytest.raises(ValueError):
        campaign_action_result("update_campaign", URL, "paused", "", retry_safe=True)
    with pytest.raises(ValueError):
        campaign_action_result("unknown_tool", URL, "no_change", "", retry_safe=True)


def test_vocabulary():
    assert set(STATUSES) == {
        "update_campaign",
        "set_campaign_status",
        "update_campaign_targeting",
        "create_campaign_draft",
        "add_post_to_campaign",
        "create_company_list_audience",
        "create_saved_audience",
    }
    assert "campaign_active" in STATUSES["add_post_to_campaign"]
    assert "deadline" in STATUSES["update_campaign_targeting"]
    for tool, succeeded in SUCCEEDED.items():
        assert succeeded <= STATUSES[tool]
        assert {"confirmation_required", "no_change"} <= succeeded
        assert not succeeded & {
            "save_unverified",
            "partially_applied",
            "change_blocked",
        }
    assert SUCCEEDED["set_campaign_status"] == {
        "confirmation_required",
        "no_change",
        "paused",
        "resumed",
    }


def test_retry_safe_only_when_nothing_admitted():
    req = Req(CmCall("campaignManagerCampaigns", "100000002", None, {}))
    assert retry_safe_from(Window()) is True
    assert retry_safe_from(Window(refused=[(req, "x")])) is True
    assert retry_safe_from(Window(admitted=[req])) is False
