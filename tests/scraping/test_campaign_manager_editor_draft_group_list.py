"""create_campaign_draft finds its group in the account's group list (synthetic data).

The setup page loads ``campaignManagerReportingCampaignGroups?q=accountId``,
the list list_campaigns reads. LinkedIn answers it normalized, and its
elements carry no ``accountId``: the request's ``accountId`` ties them to the
account.
"""

from __future__ import annotations

from typing import Any

from scraping.test_campaign_manager_editor import (
    ACCOUNT,
    GROUP,
    SETUP,
    _App,
    _campaign,
    _create,
    _emit,
    _response,
)

OTHER_ACCOUNT = "100000002"


def _group_list(account: str, elements: list[Any], included=()) -> Any:
    return _response(
        f"campaignManagerReportingCampaignGroups?q=accountId&accountId={account}",
        {
            "data": {
                "$type": "com.example.Collection",
                "elements": elements,
                "paging": {"count": 10, "start": 0, "links": [], "total": 1},
            },
            "included": list(included),
        },
    )


def _group(**overrides: Any) -> dict[str, Any]:
    # The live element shape: no accountId.
    return {
        "id": int(GROUP),
        "name": "Example group",
        "status": "ACTIVE",
        "defaultCampaignGroup": False,
        **overrides,
    }


class _ListApp(_App):
    def __init__(self, page: Any, listing: Any) -> None:
        super().__init__(page, _campaign())
        self.listing = listing

    async def navigate(self, url: str) -> None:
        if url != SETUP:
            await super().navigate(url)
            return
        self.navigated.append(url)
        self._move(url)
        _emit(self.page, [self.listing])


async def test_a_group_in_the_account_list_is_found(mock_page):
    app = _ListApp(mock_page, _group_list(ACCOUNT, [_group()]))

    result = await _create(mock_page, app, confirm_send=False)

    assert result["status"] == "confirmation_required", result["message"]
    assert result["target"] == {
        "kind": "campaign_group",
        "uiLabel": "Campaign",
        "id": int(GROUP),
        "accountId": int(ACCOUNT),
        "name": "Example group",
        "status": "ACTIVE",
    }


async def test_a_group_ref_is_resolved_through_included(mock_page):
    urn = f"urn:li:sponsoredCampaignGroup:{GROUP}"
    listing = _group_list(ACCOUNT, [urn], included=[{**_group(), "entityUrn": urn}])
    app = _ListApp(mock_page, listing)

    result = await _create(mock_page, app, confirm_send=False)

    assert result["status"] == "confirmation_required", result["message"]
    assert result["target"]["id"] == int(GROUP)


async def test_the_list_of_another_account_does_not_count(mock_page):
    app = _ListApp(mock_page, _group_list(OTHER_ACCOUNT, [_group()]))

    result = await _create(mock_page, app, confirm_send=False)

    assert result["status"] == "no_campaign_group"
    assert result["retry_safe"] is True


async def test_a_group_missing_from_the_list_is_no_campaign_group(mock_page):
    listing = _group_list(ACCOUNT, [_group(id=int(GROUP) + 1)])
    app = _ListApp(mock_page, listing)

    result = await _create(mock_page, app, confirm_send=False)

    assert result["status"] == "no_campaign_group"
