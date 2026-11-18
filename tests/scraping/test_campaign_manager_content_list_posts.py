"""list_sponsorable_posts over the content library (synthetic values, live shape)."""

from __future__ import annotations

from typing import Any

import pytest

from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
    LinkedInScraperException,
)
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
    def __init__(
        self, page: Any, payload: dict[str, Any], count: int | None = 100
    ) -> None:
        super().__init__(page)
        self.payload = payload
        self.page_size = count
        # A write the app tries while the library loads.
        self.write_on_load = False

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if "linkedin-mcp:cm-route" not in script:
            return await super().evaluate(script, arg)
        self.navigated.append(arg)
        self.page.url = "https://www.linkedin.com" + arg
        if self.write_on_load:
            await self.send("campaignManagerCreatives", {"name": "x"}, "create")
        size = f"&count={self.page_size}" if self.page_size is not None else ""
        _emit(self.page, [_response(f"{LIBRARY_PATH}{size}&start=0", self.payload)])
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


async def test_without_a_count_param_the_answer_page_size_decides(mock_page):
    # paging.count 2 and two posts listed: a full page, so more may exist.
    app = _ListApp(mock_page, _library(count=2), count=None)

    result = await _list(mock_page, app)

    error = result["section_errors"]["sponsorable_posts"]
    assert "full first page of 2" in error["error_message"]


async def test_a_short_page_by_the_answer_page_size_is_complete(mock_page):
    app = _ListApp(mock_page, _library(count=10), count=None)

    result = await _list(mock_page, app)

    assert "section_errors" not in result


async def test_no_total_and_no_page_size_is_never_called_complete(mock_page):
    payload = _library()
    del payload["data"]["paging"]
    app = _ListApp(mock_page, payload, count=None)

    result = await _list(mock_page, app)

    error = result["section_errors"]["sponsorable_posts"]
    assert error["error_type"] == "page_limit"
    assert "without saying how many" in error["error_message"]


async def test_a_write_the_app_tries_while_the_library_loads_is_aborted(mock_page):
    app = _ListApp(mock_page, _library())
    app.write_on_load = True

    result = await _list(mock_page, app)

    assert app.sent == ["abort"]
    assert [post["urn"] for post in result["sponsorable_posts"]] == [POST, OTHER]


async def test_an_ad_set_campaign_manager_refuses_is_a_bad_reference(mock_page):
    app = _ListApp(mock_page, _library())
    app.page.url = "https://example.invalid/"
    app.navigate = _setup_redirect(app)  # type: ignore[method-assign]

    with pytest.raises(InvalidReferenceError, match="setup page"):
        await _list(mock_page, app)


def _setup_redirect(app: _App) -> Any:
    async def navigate(url: str) -> None:
        app.navigated.append(url)
        app.page.url = (
            f"https://www.linkedin.com/campaignmanager/accounts/{ACCOUNT}"
            "/campaigns/new/setup"
        )

    return navigate
