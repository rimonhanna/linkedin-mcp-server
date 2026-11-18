"""list_sponsorable_posts over the content library (synthetic values, live shape)."""

from __future__ import annotations

from typing import Any

import pytest

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.scraping.campaign_manager_content import (
    CampaignContentEditor,
)
from linkedin_mcp_server.scraping.campaign_manager_entities_payload import (
    content_library_entries,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

from scraping.test_campaign_manager_content import (
    ACCOUNT,
    CAMPAIGN,
    LIBRARY_PATH,
    OTHER,
    POST,
    _App,
    _emit,
    _response,
)
from scraping.test_campaign_manager_content_library_live_shape import _live_library


def _library(*, count: int = 100, total: int | None = None) -> dict[str, Any]:
    payload = _live_library()
    blocked = {
        **payload["included"][0],
        "entityUrn": OTHER,
        "sponsorable": False,
        "nonSponsorableReasons": ["EXAMPLE_REASON"],
        "userGeneratedAdContent": {"contentType": "IMAGE"},
    }
    payload["data"]["elements"].append({"content": {"*sponsoredUpdateContent": OTHER}})
    payload["included"].append(blocked)
    payload["data"]["paging"]["count"] = count
    if total is not None:
        payload["data"]["paging"]["total"] = total
    return payload


class _ListApp(_App):
    def __init__(self, page: Any, payload: dict[str, Any], count: int = 100) -> None:
        super().__init__(page)
        self.payload = payload
        self.page_size = count

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if "linkedin-mcp:cm-route" not in script:
            return await super().evaluate(script, arg)
        self.navigated.append(arg)
        self.page.url = "https://www.linkedin.com" + arg
        path = f"{LIBRARY_PATH}&count={self.page_size}&start=0"
        _emit(self.page, [_response(path, self.payload)])
        return True


async def _list(page: Any, app: _App) -> dict[str, Any]:
    session = ScrapingSession(page)
    editor = CampaignContentEditor(session, PageNavigator(session))
    with app.running():
        return await editor.list_sponsorable_posts(ACCOUNT, CAMPAIGN)


async def test_every_library_post_is_listed_with_its_urn(mock_page):
    app = _ListApp(mock_page, _library())

    result = await _list(mock_page, app)

    posts = result["sponsorable_posts"]
    assert [post["urn"] for post in posts] == [POST, OTHER]
    assert posts[0]["sponsorable"] is True
    assert posts[0]["nonSponsorableReasons"] == []
    assert posts[0]["excerpt"] == "Example"
    assert posts[0]["lastModifiedAt"] == 1700000000000
    assert posts[1]["nonSponsorableReasons"] == ["EXAMPLE_REASON"]
    assert "not sponsorable (EXAMPLE_REASON)" in result["sections"]["sponsorable_posts"]
    assert result["target"]["id"] == int(CAMPAIGN)
    assert "section_errors" not in result
    assert app.clicks == []
    assert app.sent == []


async def test_a_full_first_page_says_more_may_exist(mock_page):
    app = _ListApp(mock_page, _library(count=2), count=2)

    result = await _list(mock_page, app)

    error = result["section_errors"]["sponsorable_posts"]
    assert error["error_type"] == "page_limit"
    assert "more may exist" in error["error_message"]


async def test_a_reported_total_beyond_the_page_is_named(mock_page):
    app = _ListApp(mock_page, _library(total=7))

    result = await _list(mock_page, app)

    error = result["section_errors"]["sponsorable_posts"]
    assert "reported 7 company posts and returned 2" in error["error_message"]


async def test_an_unknown_library_shape_raises(mock_page):
    payload = _library()
    payload["included"][0]["sponsorable"] = None
    app = _ListApp(mock_page, payload)

    with pytest.raises(LinkedInScraperException, match="does not recognise"):
        await _list(mock_page, app)


def test_a_long_excerpt_is_shortened_to_one_line():
    payload = _live_library()
    text = "First line\n\n" + "word " * 100
    payload["included"][0]["userGeneratedAdContent"]["introductoryText"]["text"] = text

    (entry,) = content_library_entries(payload) or []

    assert "\n" not in entry["excerpt"]
    assert entry["excerpt"].startswith("First line word")
    assert entry["excerpt"].endswith("...")
    assert len(entry["excerpt"]) <= 160
