"""add_post_to_campaign with an activity URN (synthetic data only).

Company post links and get_company_posts carry ``urn:li:activity:<n>``. The
content library lists each post's share or ugcPost URN with its ``activityId``
(the bundle's post-link matcher reads it as a number, a digit string or the
activity URN); the activity resolves to that post exactly, or not at all.
"""

from __future__ import annotations

from typing import Any

import pytest

from linkedin_mcp_server.scraping.campaign_manager_content import (
    CampaignContentEditor,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

from scraping.test_campaign_manager_content import (
    ACCOUNT,
    CAMPAIGN,
    CREATIVE,
    OTHER,
    POST,
    _App,
    _emit,
    _library,
    _response,
)

ACTIVITY = "urn:li:activity:100000801"
OTHER_ACTIVITY = "urn:li:activity:100000802"


class _ActivityApp(_App):
    """The library answer carries an ``activityId`` per post."""

    def __init__(self, page: Any, activities: dict[str, Any]) -> None:
        super().__init__(page)
        self.activities = activities

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if "linkedin-mcp:cm-route" not in script:
            return await super().evaluate(script, arg)
        self.navigated.append(arg)
        self.page.url = "https://www.linkedin.com" + arg
        path, payload = _library(self.posts, count=self.count)
        for entity in payload["included"]:
            if entity["entityUrn"] in self.activities:
                entity["activityId"] = self.activities[entity["entityUrn"]]
        _emit(self.page, [_response(path, payload)])
        return True


async def _run(page: Any, app: _App, post: str, *, confirm: bool) -> dict:
    session = ScrapingSession(page)
    editor = CampaignContentEditor(session, PageNavigator(session))
    with app.running():
        return await editor.add_post_to_campaign(
            ACCOUNT, CAMPAIGN, post, confirm_send=confirm
        )


@pytest.mark.parametrize(
    "activity_id",
    [100000801, "100000801", ACTIVITY],
    ids=["number", "digits", "urn"],
)
async def test_an_activity_urn_resolves_to_the_listed_post(mock_page, activity_id):
    app = _ActivityApp(mock_page, {OTHER: 100000802, POST: activity_id})

    result = await _run(mock_page, app, ACTIVITY, confirm=False)

    assert result["status"] == "confirmation_required", result["message"]
    assert result["planned"]["postUrn"] == POST
    assert result["planned"]["requestedUrn"] == ACTIVITY
    assert app.clicks == []


async def test_a_feed_update_activity_link_adds_the_share(mock_page):
    app = _ActivityApp(mock_page, {OTHER: 100000802, POST: 100000801})
    link = f"https://www.linkedin.com/feed/update/{ACTIVITY}/"

    result = await _run(mock_page, app, link, confirm=True)

    assert result["status"] == "added", result["message"]
    assert result["applied"]["creativeId"] == CREATIVE
    assert result["applied"]["postUrn"] == POST
    assert app.sent == ["fallback"]


async def test_an_activity_not_in_the_library_is_refused(mock_page):
    app = _ActivityApp(mock_page, {OTHER: 100000802, POST: 100000803})

    result = await _run(mock_page, app, ACTIVITY, confirm=True)

    assert result["status"] == "post_not_found"
    assert result["retry_safe"] is True
    assert app.clicks == []


async def test_a_library_without_activity_ids_does_not_match_by_number(mock_page):
    # The share and the activity share no id space; no fallback match.
    app = _ActivityApp(mock_page, {})

    result = await _run(mock_page, app, "urn:li:activity:100000701", confirm=True)

    assert result["status"] == "post_not_found"
    assert app.clicks == []


async def test_an_activity_on_two_posts_is_refused(mock_page):
    app = _ActivityApp(mock_page, {OTHER: 100000801, POST: 100000801})

    result = await _run(mock_page, app, ACTIVITY, confirm=True)

    assert result["status"] == "post_not_found"
    assert "More than one" in result["message"]
    assert app.clicks == []


async def test_a_share_urn_still_matches_by_its_own_urn(mock_page):
    app = _ActivityApp(mock_page, {POST: OTHER_ACTIVITY})

    result = await _run(mock_page, app, POST, confirm=False)

    assert result["status"] == "confirmation_required", result["message"]
    assert "requestedUrn" not in result["planned"]
