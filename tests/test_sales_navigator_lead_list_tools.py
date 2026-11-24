"""Sales Navigator lead-list tools and the lead-search ICP filters at the tool layer."""

from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError, ValidationError
from fastmcp.tools import FunctionTool

from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.tools.sales_navigator import register_sales_navigator_tools

TOOLS = (
    "search_sales_navigator_leads",
    "list_sales_navigator_lead_lists",
    "get_sales_navigator_lead_list",
)
RESULT = {"url": "https://www.linkedin.com/sales/lists/people", "sections": {}}


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


@pytest.mark.parametrize(
    "name", ["list_sales_navigator_lead_lists", "get_sales_navigator_lead_list"]
)
async def test_lead_list_tools_are_read_only_and_tagged(name):
    mcp = FastMCP("test")
    register_sales_navigator_tools(mcp)
    tool = await mcp.get_tool(name)
    assert tool is not None
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is True
    assert tool.tags == {"sales_navigator", "scraping"}


async def test_list_lead_lists_passes_the_start_page(mock_context):
    extractor = _extractor()
    fn = await _fn("list_sales_navigator_lead_lists")
    assert await fn(mock_context, page=3, extractor=extractor) == RESULT
    assert extractor.list_sales_navigator_lead_lists.call_args.args == (3,)


async def test_get_lead_list_normalizes_a_list_url(mock_context):
    extractor = _extractor()
    fn = await _fn("get_sales_navigator_lead_list")
    await fn(
        "https://www.linkedin.com/sales/lists/people/100000402?sortOrder=DESCENDING",
        mock_context,
        page=2,
        extractor=extractor,
    )
    assert extractor.get_sales_navigator_lead_list.call_args.args == ("100000402", 2)


async def test_lead_search_normalizes_the_icp_filters(mock_context):
    extractor = _extractor()
    fn = await _fn("search_sales_navigator_leads")
    await fn(
        mock_context,
        network=["F"],
        function=["Sales"],
        company_size=["11-50"],
        years_in_current_role=["less_than_1"],
        years_at_current_company=["more_than_10"],
        account_list=["https://www.linkedin.com/sales/lists/company?listId=100000401"],
        lead_list=["https://www.linkedin.com/sales/lists/people/100000402"],
        exclude_lead_list=["100000403"],
        extractor=extractor,
    )
    kwargs = extractor.search_sales_navigator_leads.call_args.kwargs
    assert kwargs["network"] == ["F"]
    assert kwargs["function"] == ["25"]
    assert kwargs["company_size"] == ["C"]
    assert kwargs["years_in_current_role"] == ["less_than_1"]
    assert kwargs["years_at_current_company"] == ["more_than_10"]
    assert kwargs["account_list"] == ["100000401"]
    assert kwargs["lead_list"] == ["100000402"]
    assert kwargs["exclude_lead_list"] == ["100000403"]


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("search_sales_navigator_leads", {"network": ["1st"]}),
        ("search_sales_navigator_leads", {"function": ["astrology"]}),
        ("search_sales_navigator_leads", {"company_size": ["huge"]}),
        ("search_sales_navigator_leads", {"years_in_current_role": ["forever"]}),
        ("search_sales_navigator_leads", {"account_list": ["my list"]}),
        ("search_sales_navigator_leads", {"exclude_lead_list": ["abc"]}),
        ("get_sales_navigator_lead_list", {"list_id": "abc"}),
        ("get_sales_navigator_lead_list", {"list_id": "1", "page": 41}),
        ("list_sales_navigator_lead_lists", {"page": 0}),
    ],
)
async def test_bad_input_never_starts_the_browser(ready, name, args):
    mcp = FastMCP("test")
    register_sales_navigator_tools(mcp)
    with pytest.raises((ToolError, ValidationError)):
        await mcp.call_tool(name, args)
    ready.assert_not_awaited()


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("list_sales_navigator_lead_lists", {}),
        ("get_sales_navigator_lead_list", {"list_id": "100000402"}),
    ],
)
async def test_auth_error_triggers_relogin(name, args):
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
        patch("linkedin_mcp_server.tools.sales_navigator.handle_auth_error", relogin),
        pytest.raises(ToolError),
    ):
        await mcp.call_tool(name, args)
    relogin.assert_awaited_once()
