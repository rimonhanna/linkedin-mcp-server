"""Campaign Manager read tools: argument handling and delegation."""

from datetime import date
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError, ValidationError
from fastmcp.tools import FunctionTool

from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.tools.campaign_manager import (
    register_campaign_manager_tools,
)

TOOLS = (
    "list_ad_accounts",
    "list_campaigns",
    "get_campaign",
    "get_campaign_analytics",
    "search_targeting_entities",
    "list_audiences",
    "list_sponsorable_posts",
)
RESULT = {"url": "https://www.linkedin.com/campaignmanager/accounts", "sections": {}}


def _extractor() -> MagicMock:
    mock = MagicMock()
    for name in TOOLS:
        setattr(mock, name, AsyncMock(return_value=RESULT))
    return mock


async def _fn(name: str):
    mcp = FastMCP("test")
    register_campaign_manager_tools(mcp)
    tool = await mcp.get_tool(name)
    assert tool is not None
    return cast(FunctionTool, tool).fn


@pytest.fixture
def ready():
    mock = AsyncMock(return_value=_extractor())
    with patch("linkedin_mcp_server.tools.campaign_manager.get_ready_extractor", mock):
        yield mock


async def _call(mcp: FastMCP, name: str, args: dict):
    return await mcp.call_tool(name, args)


class TestRegistration:
    @pytest.mark.parametrize("name", TOOLS)
    async def test_tool_is_read_only_and_tagged(self, name):
        mcp = FastMCP("test")
        register_campaign_manager_tools(mcp)
        tool = await mcp.get_tool(name)
        assert tool is not None
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.tags == {"campaign_manager", "scraping"}

    @pytest.mark.parametrize("name", TOOLS)
    async def test_docstring_opens_with_terminology_mapping(self, name):
        mcp = FastMCP("test")
        register_campaign_manager_tools(mcp)
        tool = await mcp.get_tool(name)
        assert tool is not None
        assert (
            (tool.description or "")
            .lstrip()
            .startswith("Terminology: API campaign group")
        )


class TestDelegation:
    async def test_list_ad_accounts(self, mock_context):
        extractor = _extractor()
        fn = await _fn("list_ad_accounts")
        assert await fn(mock_context, extractor=extractor) == RESULT
        extractor.list_ad_accounts.assert_awaited_once_with()

    async def test_list_campaigns_normalizes_an_account_urn(self, mock_context):
        extractor = _extractor()
        fn = await _fn("list_campaigns")
        await fn("urn:li:sponsoredAccount:100000001", mock_context, extractor=extractor)
        extractor.list_campaigns.assert_awaited_once_with("100000001")

    async def test_get_campaign_normalizes_urls(self, mock_context):
        extractor = _extractor()
        fn = await _fn("get_campaign")
        await fn(
            "https://www.linkedin.com/campaignmanager/accounts/100000001/campaigns",
            "https://www.linkedin.com/campaignmanager/accounts/100000001/campaigns/200000002/details",
            mock_context,
            extractor=extractor,
        )
        extractor.get_campaign.assert_awaited_once_with("100000001", "200000002")

    async def test_analytics_defaults(self, mock_context):
        extractor = _extractor()
        fn = await _fn("get_campaign_analytics")
        await fn("100000001", mock_context, extractor=extractor)
        extractor.get_campaign_analytics.assert_awaited_once_with(
            "100000001",
            level="campaign",
            start_date=None,
            end_date=None,
            demographics=None,
        )

    async def test_analytics_forwards_range_level_and_pivots(self, mock_context):
        extractor = _extractor()
        fn = await _fn("get_campaign_analytics")
        await fn(
            "100000001",
            mock_context,
            level="creative",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            demographics=["MEMBER_JOB_FUNCTION"],
            extractor=extractor,
        )
        extractor.get_campaign_analytics.assert_awaited_once_with(
            "100000001",
            level="creative",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            demographics=["MEMBER_JOB_FUNCTION"],
        )

    async def test_search_targeting_entities_normalizes_ids(self, mock_context):
        extractor = _extractor()
        fn = await _fn("search_targeting_entities")
        await fn(
            "urn:li:sponsoredAccount:100000001",
            "urn:li:sponsoredCampaign:200000002",
            "engineer",
            mock_context,
            extractor=extractor,
        )
        extractor.search_targeting_entities.assert_awaited_once_with(
            "100000001", "200000002", "engineer"
        )

    async def test_list_audiences_normalizes_an_account_urn(self, mock_context):
        extractor = _extractor()
        fn = await _fn("list_audiences")
        await fn("urn:li:sponsoredAccount:100000001", mock_context, extractor=extractor)
        extractor.list_audiences.assert_awaited_once_with("100000001")


class TestRefusedBeforeTheBrowser:
    @pytest.mark.parametrize(
        ("name", "args"),
        [
            ("list_campaigns", {"ad_account_id": "not-an-id"}),
            ("get_campaign", {"ad_account_id": "abc", "campaign_id": "200000002"}),
            ("get_campaign", {"ad_account_id": "100000001", "campaign_id": "../1"}),
            ("get_campaign_analytics", {"ad_account_id": "https://example.com/1"}),
            (
                "get_campaign_analytics",
                {
                    "ad_account_id": "100000001",
                    "start_date": "2026-02-01",
                    "end_date": "2026-01-01",
                },
            ),
            (
                "get_campaign_analytics",
                {"ad_account_id": "100000001", "start_date": "2026-02-01"},
            ),
            (
                "get_campaign_analytics",
                {"ad_account_id": "100000001", "end_date": "2026-02-01"},
            ),
            (
                "get_campaign_analytics",
                {"ad_account_id": "100000001", "demographics": ["NOT_A_PIVOT"]},
            ),
            (
                "get_campaign_analytics",
                {"ad_account_id": "100000001", "level": "account"},
            ),
            ("list_audiences", {"ad_account_id": "not-an-id"}),
            (
                "search_targeting_entities",
                {"ad_account_id": "100000001", "campaign_id": "x", "query": "a"},
            ),
            (
                "search_targeting_entities",
                {"ad_account_id": "1", "campaign_id": "2", "query": "   "},
            ),
            (
                "search_targeting_entities",
                {"ad_account_id": "1", "campaign_id": "2", "query": "a" * 101},
            ),
        ],
    )
    async def test_bad_input_never_starts_the_browser(self, ready, name, args):
        mcp = FastMCP("test")
        register_campaign_manager_tools(mcp)
        with pytest.raises((ToolError, ValidationError)):
            await mcp.call_tool(name, args)
        ready.assert_not_awaited()


class TestAuthError:
    @pytest.mark.parametrize(
        ("name", "args"),
        [
            ("list_ad_accounts", {}),
            ("list_campaigns", {"ad_account_id": "100000001"}),
            ("get_campaign", {"ad_account_id": "1", "campaign_id": "2"}),
            ("get_campaign_analytics", {"ad_account_id": "100000001"}),
            (
                "search_targeting_entities",
                {"ad_account_id": "1", "campaign_id": "2", "query": "engineer"},
            ),
            ("list_audiences", {"ad_account_id": "100000001"}),
            ("list_sponsorable_posts", {"ad_account_id": "1", "campaign_id": "2"}),
        ],
    )
    async def test_auth_error_triggers_relogin(self, name, args):
        mcp = FastMCP("test")
        register_campaign_manager_tools(mcp)
        extractor = _extractor()
        for tool in TOOLS:
            getattr(extractor, tool).side_effect = AuthenticationError("expired")
        relogin = AsyncMock(side_effect=RuntimeError("login window opened"))
        with (
            patch(
                "linkedin_mcp_server.tools.campaign_manager.get_ready_extractor",
                AsyncMock(return_value=extractor),
            ),
            patch(
                "linkedin_mcp_server.tools.campaign_manager.handle_auth_error", relogin
            ),
            pytest.raises(ToolError),
        ):
            await mcp.call_tool(name, args)
        relogin.assert_awaited_once()
