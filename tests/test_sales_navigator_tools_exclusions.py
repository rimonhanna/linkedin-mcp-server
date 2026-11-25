"""Sales Navigator search tools: the exclusion parameters at the tool layer."""

from typing import cast
from unittest.mock import AsyncMock, MagicMock

from fastmcp import FastMCP
from fastmcp.tools import FunctionTool

from linkedin_mcp_server.tools.sales_navigator import register_sales_navigator_tools

RESULT = {"url": "https://www.linkedin.com/sales/search/company", "sections": {}}


def _extractor() -> MagicMock:
    mock = MagicMock()
    mock.search_sales_navigator_accounts = AsyncMock(return_value=RESULT)
    mock.search_sales_navigator_leads = AsyncMock(return_value=RESULT)
    return mock


async def _fn(name: str):
    mcp = FastMCP("test")
    register_sales_navigator_tools(mcp)
    tool = await mcp.get_tool(name)
    assert tool is not None
    return cast(FunctionTool, tool).fn


async def test_account_search_takes_an_excluded_industry_alone(mock_context):
    extractor = _extractor()
    fn = await _fn("search_sales_navigator_accounts")

    await fn(
        mock_context, exclude_industry=["Software Development"], extractor=extractor
    )

    kwargs = extractor.search_sales_navigator_accounts.await_args.kwargs
    assert kwargs["exclude_industry"] == ["4"]
    assert kwargs["industry"] is None


async def test_lead_search_takes_excluded_industries_and_companies(mock_context):
    extractor = _extractor()
    fn = await _fn("search_sales_navigator_leads")

    await fn(
        mock_context,
        exclude_industry=["4"],
        exclude_current_company=["urn:li:organization:1033"],
        extractor=extractor,
    )

    kwargs = extractor.search_sales_navigator_leads.await_args.kwargs
    assert kwargs["exclude_industry"] == ["4"]
    assert kwargs["exclude_current_company"] == ["urn:li:organization:1033"]
    assert kwargs["industry"] is None
    assert kwargs["current_company"] is None
