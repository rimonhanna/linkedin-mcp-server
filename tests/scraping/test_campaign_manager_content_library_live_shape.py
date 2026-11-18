"""add_post_to_campaign over the content library shape seen live (synthetic values).

Live 2026-11-18, ``organicCompanyContent`` answers normalized: elements hold a
``content`` ref, the posts are ``included`` entities, a sponsorable post sends
``nonSponsorableReasons: null`` and no entity carries an ``activityId``.
"""

from __future__ import annotations

from typing import Any

from linkedin_mcp_server.scraping.campaign_manager_content import (
    CampaignContentEditor,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

from scraping.test_campaign_manager_content import (
    LIBRARY_PATH,
    POST,
    ACCOUNT,
    CAMPAIGN,
    _App,
    _emit,
    _response,
)


def _live_library() -> dict[str, Any]:
    entity = {
        "$type": "com.example.SponsoredUpdateContent",
        "accountId": None,
        "adDscStatus": None,
        "authorDetails": None,
        "authorUrn": "urn:li:organization:100000901",
        "companyId": 100000901,
        "entityUrn": POST,
        "lastModifiedAt": 1700000000000,
        "livePreview": None,
        "nonSponsorableReasons": None,
        "organicStoryItem": None,
        "sponsorable": True,
        "state": {"$type": "com.example.State", "lifecycleState": "PUBLISHED"},
        "userGeneratedAdContent": {
            "$type": "com.example.UserGeneratedAdContent",
            "contentType": "TEXT",
            "entities": [{}],
            "introductoryText": {"text": "Example", "attributes": [], "x": None},
            "referenceUrn": "urn:li:digitalmediaAsset:EXAMPLE",
        },
        "visibility": "ORGANIC",
    }
    return {
        "data": {
            "$type": "com.example.Collection",
            "elements": [
                {
                    "$type": "com.example.ContentLibraryEntity",
                    "content": {"*sponsoredUpdateContent": POST},
                }
            ],
            "entityUrn": "urn:li:collection:example",
            "paging": {"count": 100, "links": [], "start": 0},
        },
        "included": [entity],
    }


class _LiveApp(_App):
    def __init__(self, page: Any) -> None:
        super().__init__(page)
        self.posts = [(POST, True, [])]

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if "linkedin-mcp:cm-route" not in script:
            return await super().evaluate(script, arg)
        self.navigated.append(arg)
        self.page.url = "https://www.linkedin.com" + arg
        path = f"{LIBRARY_PATH}&count=100&start=0"
        _emit(self.page, [_response(path, _live_library())])
        return True


async def _run(page: Any, app: _App, post: str) -> dict:
    session = ScrapingSession(page)
    editor = CampaignContentEditor(session, PageNavigator(session))
    with app.running():
        return await editor.add_post_to_campaign(
            ACCOUNT, CAMPAIGN, post, confirm_send=False
        )


async def test_the_live_library_shape_is_read(mock_page):
    app = _LiveApp(mock_page)

    result = await _run(mock_page, app, POST)

    assert result["status"] == "confirmation_required", result["message"]
    assert result["planned"]["postUrn"] == POST
    assert app.clicks == []


async def test_an_activity_without_library_activity_ids_is_refused_plainly(mock_page):
    app = _LiveApp(mock_page)

    result = await _run(mock_page, app, "urn:li:activity:100000801")

    assert result["status"] == "post_not_found"
    assert result["retry_safe"] is True
    assert "urn:li:share or urn:li:ugcPost" in result["message"]
    assert app.clicks == []
