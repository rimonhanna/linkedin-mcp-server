"""add_post_to_campaign with an activity URN (synthetic data only).

Company post links and get_company_posts carry ``urn:li:activity:<n>``. The
activity is resolved to its post's share or ugcPost URN first (its feed update
page), and only that URN is then matched, exactly, against the content library.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from linkedin_mcp_server.scraping import campaign_manager_content as content_module
from linkedin_mcp_server.scraping.campaign_manager_content import (
    CampaignContentEditor,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

from scraping.test_campaign_manager_content import (
    ACCOUNT,
    CAMPAIGN,
    CREATIVE,
    POST,
    _App,
)

ACTIVITY = "urn:li:activity:100000801"


async def _run(page: Any, app: _App, post: str, resolved: Any, *, confirm: bool):
    session = ScrapingSession(page)
    editor = CampaignContentEditor(session, PageNavigator(session))
    resolver = AsyncMock(return_value=resolved)
    with app.running(), patch.object(content_module, "resolve_activity_post", resolver):
        result = await editor.add_post_to_campaign(
            ACCOUNT, CAMPAIGN, post, confirm_send=confirm
        )
    return result, resolver


async def test_an_activity_urn_is_added_as_its_resolved_post(mock_page):
    app = _App(mock_page)

    result, resolver = await _run(mock_page, app, ACTIVITY, POST, confirm=True)

    assert result["status"] == "added", result["message"]
    assert result["planned"]["postUrn"] == POST
    assert result["planned"]["requestedUrn"] == ACTIVITY
    assert result["applied"]["creativeId"] == CREATIVE
    assert result["applied"]["postUrn"] == POST
    assert resolver.await_args.args[2] == ACTIVITY


async def test_a_feed_update_activity_link_is_resolved(mock_page):
    app = _App(mock_page)
    link = f"https://www.linkedin.com/feed/update/{ACTIVITY}/"

    result, resolver = await _run(mock_page, app, link, POST, confirm=False)

    assert result["status"] == "confirmation_required", result["message"]
    assert resolver.await_args.args[2] == ACTIVITY


async def test_a_resolved_post_outside_the_library_is_refused(mock_page):
    app = _App(mock_page)

    result, _ = await _run(
        mock_page, app, ACTIVITY, "urn:li:ugcPost:100000799", confirm=True
    )

    assert result["status"] == "post_not_found"
    assert result["retry_safe"] is True
    assert app.clicks == []


async def test_an_unreadable_activity_stops_before_campaign_manager(mock_page):
    app = _App(mock_page)
    refusal = (
        "control_unavailable",
        "The post id could not be read. Nothing was changed.",
    )

    result, _ = await _run(mock_page, app, ACTIVITY, refusal, confirm=True)

    assert result["status"] == "control_unavailable"
    assert result["planned"] == {"requestedUrn": ACTIVITY}
    assert result["retry_safe"] is True
    assert app.navigated == []


async def test_a_share_urn_is_not_resolved(mock_page):
    app = _App(mock_page)

    result, resolver = await _run(mock_page, app, POST, None, confirm=False)

    assert result["status"] == "confirmation_required", result["message"]
    assert "requestedUrn" not in result["planned"]
    resolver.assert_not_awaited()
