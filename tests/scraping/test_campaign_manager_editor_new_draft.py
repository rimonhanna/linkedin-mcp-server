"""create_campaign_draft in the classic editor (synthetic data only).

In the classic editor choosing the objective on the setup page sends nothing:
Next only carries it to the new ad set's details page, and the draft is
created by one create that its footer's Save and exit sends, the name already
in it. A tool that waited for a save after choosing the objective timed out
with nothing sent.
"""

from __future__ import annotations

from typing import Any

from scraping.test_campaign_manager_editor import (
    ACCOUNT,
    APP,
    GROUP,
    NEW,
    SETUP,
    _App,
    _campaign,
    _create,
)

from linkedin_mcp_server.response_structure import current_log
from linkedin_mcp_server.scraping import campaign_manager_write as write_module

NEW_DETAILS = (
    f"{APP}/campaigns/new/details?campaignGroupId={GROUP}"
    "&initialObjectiveType=WEBSITE_VISIT"
)
CAMPAIGNS = f"{APP}/campaigns"


class _NewDraftApp(_App):
    """The classic setup flow: Save and exit on the new ad set's page creates it."""

    def __init__(self, page: Any) -> None:
        super().__init__(page, _campaign())
        self.classic = True
        # What the new ad set's footer does: "save", "disabled" or "layout".
        self.footer = "save"
        self.create_extra: dict[str, Any] = {}
        self.create_restli: str | None = "create"
        self.programs[write_module._NEW_FOOTER_JS] = "new_footer"
        self.programs[write_module._STRUCTURE_JS] = "structure"

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        name = self.programs[script]
        if name == "structure":
            self.calls.append((name, arg))
            return {"anchors": {}, "loading": False}
        if name != "new_footer":
            return await super().evaluate(script, arg)
        self.calls.append((name, arg))
        if self.footer == "disabled":
            return {"count": 2, "clicked": False, "disabled": True}
        if self.footer == "layout":
            return {"count": 0, "clicked": False}
        body = {
            "status": "DRAFT",
            "accountId": f"urn:li:sponsoredAccount:{ACCOUNT}",
            "campaignGroupId": f"urn:li:sponsoredCampaignGroup:{GROUP}",
            "objectiveType": "WEBSITE_VISIT",
            "name": self.typed[-1] if self.typed else "Untitled",
            **self.create_extra,
        }
        sent = await self.send(
            "campaignManagerCampaigns", body, self.create_restli, created_id=NEW
        )
        if sent and 200 <= self.save_status < 300:
            self.records[NEW] = _campaign(
                id=int(NEW),
                name=body["name"],
                **(
                    {"politicalIntent": body["politicalIntent"]}
                    if "politicalIntent" in body
                    else {}
                ),
            )
            self._move(CAMPAIGNS)
        return {"count": 2, "clicked": True}


async def test_save_and_exit_sends_the_one_named_create(mock_page):
    app = _NewDraftApp(mock_page)

    result = await _create(mock_page, app)

    assert result["status"] == "created", result["message"]
    assert result["created_id"] == NEW
    assert result["retry_safe"] is False
    assert result["applied"] == {
        "status": "DRAFT",
        "name": "Example draft",
        "campaignGroupId": int(GROUP),
        "objectiveType": "WEBSITE_VISIT",
    }
    assert app.navigated[:2] == [SETUP, NEW_DETAILS]
    assert app.typed == ["Example draft"]
    assert app.sent == [("campaignManagerCampaigns", "fallback")]


async def test_a_create_without_the_typed_name_is_refused(mock_page):
    app = _NewDraftApp(mock_page)
    app.create_extra = {"name": "Untitled"}

    result = await _create(mock_page, app)

    assert result["status"] == "change_blocked"
    assert result["retry_safe"] is True
    assert app.sent == [("campaignManagerCampaigns", "abort")]
    assert [item["reason"] for item in result["refused"]] == [
        "campaign create with another name"
    ]


async def test_a_disabled_save_and_exit_creates_nothing(mock_page):
    app = _NewDraftApp(mock_page)
    app.footer = "disabled"

    result = await _create(mock_page, app)
    log = current_log()

    assert result["status"] == "control_unavailable"
    assert "stayed disabled" in result["message"]
    assert result["retry_safe"] is True
    assert app.sent == []
    assert app.navigated[-1] == CAMPAIGNS
    assert log is not None
    assert log.writes[-1]["step"] == "create"
    assert log.pages[-1]["step"] == "create"


async def test_the_newer_editor_is_not_driven(mock_page):
    app = _NewDraftApp(mock_page)
    app.classic = False

    result = await _create(mock_page, app)

    assert result["status"] == "control_unavailable"
    assert "newer editor" in result["message"]
    assert result["retry_safe"] is True
    assert app.typed == []
    assert [name for name, _ in app.calls].count("new_footer") == 0


async def test_an_undeclared_political_fill_creates_nothing(mock_page):
    app = _NewDraftApp(mock_page)
    app.create_extra = {"politicalIntent": "NOT_POLITICAL"}

    result = await _create(mock_page, app)

    assert result["status"] == "declaration_required"
    assert result["retry_safe"] is True
    assert app.sent == [("campaignManagerCampaigns", "abort")]


async def test_the_declared_political_fill_is_created_and_reported(mock_page):
    app = _NewDraftApp(mock_page)
    app.create_extra = {"politicalIntent": "NOT_POLITICAL"}

    result = await _create(mock_page, app, political_ad=False)

    assert result["status"] == "created", result["message"]
    assert result["applied"]["politicalIntent"] == "NOT_POLITICAL"
    assert result["message"].endswith("The political-ad declaration was recorded.")
    assert app.sent == [("campaignManagerCampaigns", "fallback")]


# The page's own defaults a live classic Save and exit sent with the planned
# fields (key names observed 2026-11-18; the values are synthetic).
_PAGE_DEFAULTS = {
    "accountProductType": "EXAMPLE",
    "adFormats": ["EXAMPLE"],
    "audienceExpansionEnabled": False,
    "contentTrackingId": "example",
    "costType": "EXAMPLE",
    "creativeSelection": "EXAMPLE",
    "dailyBudget": {"amount": "10", "currencyCode": "EUR"},
    "lifetimePacingStrategy": "EXAMPLE",
    "locale": {"country": "US", "language": "en"},
    "myNetworkDeliveryEnabled": False,
    "offsiteDeliveryEnabled": False,
    "offsitePreferences": {},
    "optimizationTargetType": "EXAMPLE",
    "pacingStrategy": "EXAMPLE",
    "politicalIntent": "NOT_POLITICAL",
    "runSchedule": {"start": 1},
    "targetingCriteria": {"include": {"and": []}},
    "unitCost": {"amount": "1", "currencyCode": "EUR"},
}


async def test_the_live_create_without_a_restli_header_is_admitted(mock_page):
    app = _NewDraftApp(mock_page)
    app.create_restli = None
    app.create_extra = dict(_PAGE_DEFAULTS)

    result = await _create(mock_page, app, political_ad=False)

    assert result["status"] == "created", result["message"]
    assert result["created_id"] == NEW
    assert app.sent == [("campaignManagerCampaigns", "fallback")]
    assert "refused_structure" not in result


async def test_a_field_beyond_the_page_defaults_is_refused_with_its_structure(
    mock_page,
):
    app = _NewDraftApp(mock_page)
    app.create_restli = None
    app.create_extra = {**_PAGE_DEFAULTS, "billingReferenceId": "urn:li:example:1"}

    result = await _create(mock_page, app, political_ad=False)

    assert result["status"] == "change_blocked", result["message"]
    assert result["retry_safe"] is True
    assert app.sent == [("campaignManagerCampaigns", "abort")]
    [structure] = result["refused_structure"]
    assert structure["reason"] == (
        "campaign create sets fields beyond the page's defaults: billingReferenceId"
    )
    assert "billingReferenceId" in structure["body"]
    assert "urn:li:example:1" not in str(structure)
