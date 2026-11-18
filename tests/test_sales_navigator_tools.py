"""Sales Navigator read tools: argument handling and delegation."""

from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError, ValidationError
from fastmcp.tools import FunctionTool

from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.scraping.sales_navigator_payload import SENIORITY_IDS
from linkedin_mcp_server.tools.sales_navigator import (
    register_sales_navigator_tools,
)

TOOLS = (
    "search_sales_navigator_accounts",
    "search_sales_navigator_leads",
    "get_sales_navigator_account",
    "list_sales_navigator_account_lists",
    "get_sales_navigator_account_list",
)
RESULT = {"url": "https://www.linkedin.com/sales/search/company", "sections": {}}


def _extractor() -> MagicMock:
    mock = MagicMock()
    for name in TOOLS:
        setattr(mock, name, AsyncMock(return_value=RESULT))
    return mock


async def _fn(name: str):
    mcp = FastMCP("test")
    register_sales_navigator_tools(mcp)
    tool = await mcp.get_tool(name)
    assert tool is not None
    return cast(FunctionTool, tool).fn


@pytest.fixture
def ready():
    mock = AsyncMock(return_value=_extractor())
    with patch("linkedin_mcp_server.tools.sales_navigator.get_ready_extractor", mock):
        yield mock


class TestRegistration:
    @pytest.mark.parametrize("name", TOOLS)
    async def test_tool_is_read_only_and_tagged(self, name):
        mcp = FastMCP("test")
        register_sales_navigator_tools(mcp)
        tool = await mcp.get_tool(name)
        assert tool is not None
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.tags == {"sales_navigator", "scraping"}

    @pytest.mark.parametrize(
        "name",
        ["get_sales_navigator_account_list", "list_sales_navigator_account_lists"],
    )
    async def test_list_tools_say_how_far_one_call_reads(self, name):
        mcp = FastMCP("test")
        register_sales_navigator_tools(mcp)
        tool = await mcp.get_tool(name)
        assert tool is not None
        description = " ".join((tool.description or "").split())
        assert "reads up to 1,000 entries per call" in description
        assert "pass next_page to continue" in description
        assert "first 25" not in description

    async def test_seniority_values_are_the_payload_levels(self):
        """The tool offers exactly the levels the reader can map to ids."""
        mcp = FastMCP("test")
        register_sales_navigator_tools(mcp)
        tool = await mcp.get_tool("search_sales_navigator_leads")
        assert tool is not None
        seniority = tool.parameters["properties"]["seniority"]
        enum = seniority["anyOf"][0]["items"]["enum"]
        assert enum == list(SENIORITY_IDS)
        for level in SENIORITY_IDS:
            assert level in seniority["description"]

    @pytest.mark.parametrize(
        "name", ("search_sales_navigator_accounts", "search_sales_navigator_leads")
    )
    async def test_search_docstrings_state_the_cap_and_the_seat(self, name):
        mcp = FastMCP("test")
        register_sales_navigator_tools(mcp)
        tool = await mcp.get_tool(name)
        assert tool is not None
        assert "1,000 results" in (tool.description or "")
        assert "Sales Navigator seat" in (tool.description or "")


class TestDelegation:
    async def test_account_search_converts_names_before_delegating(self, mock_context):
        extractor = _extractor()
        fn = await _fn("search_sales_navigator_accounts")
        await fn(
            mock_context,
            keywords="cloud",
            industry=["106"],
            hq_location=["102890719"],
            size=["B"],
            headcount_growth_min=5,
            headcount_growth_max=10,
            page=2,
            extractor=extractor,
        )
        extractor.search_sales_navigator_accounts.assert_awaited_once_with(
            keywords="cloud",
            industry=["106"],
            hq_location=["102890719"],
            size=["B"],
            headcount_growth_min=5,
            headcount_growth_max=10,
            page=2,
        )

    async def test_account_search_with_only_keywords(self, mock_context):
        extractor = _extractor()
        fn = await _fn("search_sales_navigator_accounts")
        await fn(mock_context, keywords="cloud", extractor=extractor)
        extractor.search_sales_navigator_accounts.assert_awaited_once_with(
            keywords="cloud",
            industry=None,
            hq_location=None,
            size=None,
            headcount_growth_min=None,
            headcount_growth_max=None,
            page=1,
        )

    async def test_lead_search_delegates(self, mock_context):
        extractor = _extractor()
        fn = await _fn("search_sales_navigator_leads")
        await fn(
            mock_context,
            title=["Example title"],
            seniority=["cxo", "director"],
            current_company=["100000001"],
            location=["102890719"],
            industry=["106"],
            page=3,
            extractor=extractor,
        )
        extractor.search_sales_navigator_leads.assert_awaited_once_with(
            keywords=None,
            title=["Example title"],
            seniority=["cxo", "director"],
            current_company=["100000001"],
            location=["102890719"],
            industry=["106"],
            page=3,
        )

    @pytest.mark.parametrize(
        "value",
        [
            "100000001",
            "urn:li:fs_salesCompany:100000001",
            "urn:li:organization:100000001",
            "https://www.linkedin.com/sales/company/100000001",
        ],
    )
    async def test_get_account_normalizes_the_company_id(self, mock_context, value):
        extractor = _extractor()
        fn = await _fn("get_sales_navigator_account")
        await fn(value, mock_context, extractor=extractor)
        extractor.get_sales_navigator_account.assert_awaited_once_with("100000001")

    async def test_list_account_lists(self, mock_context):
        extractor = _extractor()
        fn = await _fn("list_sales_navigator_account_lists")
        assert await fn(mock_context, extractor=extractor) == RESULT
        extractor.list_sales_navigator_account_lists.assert_awaited_once()
        assert extractor.list_sales_navigator_account_lists.call_args.args == (1,)

    async def test_list_account_lists_passes_the_start_page(self, mock_context):
        extractor = _extractor()
        fn = await _fn("list_sales_navigator_account_lists")
        await fn(mock_context, page=41, extractor=extractor)
        assert extractor.list_sales_navigator_account_lists.call_args.args == (41,)

    @pytest.mark.parametrize(
        "value",
        [
            "100000001",
            "https://www.linkedin.com/sales/accounts/dashboard"
            "?listGroup=CUSTOM_LISTS&listId=100000001",
            "https://www.linkedin.com/sales/lists/company?listId=100000001",
        ],
    )
    async def test_get_account_list_normalizes_the_list_id(self, mock_context, value):
        extractor = _extractor()
        fn = await _fn("get_sales_navigator_account_list")
        await fn(value, mock_context, extractor=extractor)
        extractor.get_sales_navigator_account_list.assert_awaited_once()
        assert extractor.get_sales_navigator_account_list.call_args.args == (
            "100000001",
            1,
        )

    async def test_get_account_list_passes_the_start_page(self, mock_context):
        extractor = _extractor()
        fn = await _fn("get_sales_navigator_account_list")
        await fn("100000001", mock_context, page=41, extractor=extractor)
        assert extractor.get_sales_navigator_account_list.call_args.args == (
            "100000001",
            41,
        )


class TestBadInput:
    @pytest.mark.parametrize(
        ("name", "args"),
        [
            ("get_sales_navigator_account", {"company_id": "acme"}),
            (
                "get_sales_navigator_account",
                {"company_id": "https://www.linkedin.com/company/acme/"},
            ),
            ("get_sales_navigator_account_list", {"list_id": "abc"}),
            (
                "get_sales_navigator_account_list",
                {"list_id": "https://www.linkedin.com/sales/lists/company?listId=abc"},
            ),
            ("get_sales_navigator_account_list", {"list_id": "1", "page": 0}),
            ("list_sales_navigator_account_lists", {"page": 0}),
            ("search_sales_navigator_accounts", {}),
            ("search_sales_navigator_accounts", {"keywords": "x", "page": 0}),
            ("search_sales_navigator_accounts", {"keywords": "x", "page": 41}),
            (
                "search_sales_navigator_accounts",
                {"headcount_growth_min": 10, "headcount_growth_max": 5},
            ),
            (
                "search_sales_navigator_accounts",
                {"industry": ["no such industry name"]},
            ),
            ("search_sales_navigator_accounts", {"size": ["no such size"]}),
            ("search_sales_navigator_leads", {}),
            ("search_sales_navigator_leads", {"keywords": "x", "page": 41}),
            (
                "search_sales_navigator_leads",
                {"keywords": "x", "seniority": ["boss"]},
            ),
            (
                "search_sales_navigator_leads",
                {"title": ["x"], "industry": ["no such industry name"]},
            ),
        ],
    )
    async def test_bad_input_never_starts_the_browser(self, ready, name, args):
        mcp = FastMCP("test")
        register_sales_navigator_tools(mcp)
        with pytest.raises((ToolError, ValidationError)):
            await mcp.call_tool(name, args)
        ready.assert_not_awaited()


class TestAuthError:
    @pytest.mark.parametrize(
        ("name", "args"),
        [
            ("search_sales_navigator_accounts", {"keywords": "x"}),
            ("search_sales_navigator_leads", {"keywords": "x"}),
            ("get_sales_navigator_account", {"company_id": "100000001"}),
            ("list_sales_navigator_account_lists", {}),
            ("get_sales_navigator_account_list", {"list_id": "100000001"}),
        ],
    )
    async def test_auth_error_triggers_relogin(self, name, args):
        mcp = FastMCP("test")
        register_sales_navigator_tools(mcp)
        extractor = _extractor()
        for tool in TOOLS:
            getattr(extractor, tool).side_effect = AuthenticationError("expired")
        relogin = AsyncMock(side_effect=RuntimeError("login window opened"))
        with (
            patch(
                "linkedin_mcp_server.tools.sales_navigator.get_ready_extractor",
                AsyncMock(return_value=extractor),
            ),
            patch(
                "linkedin_mcp_server.tools.sales_navigator.handle_auth_error", relogin
            ),
            pytest.raises(ToolError),
        ):
            await mcp.call_tool(name, args)
        relogin.assert_awaited_once()
