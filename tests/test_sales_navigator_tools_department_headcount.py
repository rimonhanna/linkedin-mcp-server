"""search_sales_navigator_accounts: the department_headcount parameter."""

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from linkedin_mcp_server.tools.sales_navigator import register_sales_navigator_tools

from test_sales_navigator_tools import _extractor, _fn

ENGINEERING = {"department": "Engineering", "min": 50, "max": 500}


async def test_the_schema_offers_department_with_min_and_max():
    mcp = FastMCP("test")
    register_sales_navigator_tools(mcp)
    tool = await mcp.get_tool("search_sales_navigator_accounts")
    assert tool is not None
    schema = tool.parameters
    param = schema["properties"]["department_headcount"]
    ref = param["anyOf"][0]["$ref"].rsplit("/", 1)[-1]
    shape = schema["$defs"][ref]
    assert set(shape["properties"]) == {"department", "min", "max"}
    assert shape["required"] == ["department"]
    assert "department_headcount" not in schema.get("required", [])
    assert "information_technology" in param["description"]


async def test_department_headcount_alone_is_a_criterion_and_is_passed_on(
    mock_context,
):
    extractor = _extractor()
    fn = await _fn("search_sales_navigator_accounts")
    await fn(mock_context, department_headcount=ENGINEERING, extractor=extractor)
    call = extractor.search_sales_navigator_accounts.await_args
    assert call.kwargs["department_headcount"] == ENGINEERING


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"department": "Engineering", "min": 500, "max": 50}, "not be greater"),
        ({"department": "Engineering", "min": -1}, "at least 0"),
        ({"department": "Astrology", "min": 1}, "Unknown function"),
    ],
)
async def test_a_bad_department_headcount_is_refused_before_the_browser(
    mock_context, value, message
):
    extractor = _extractor()
    fn = await _fn("search_sales_navigator_accounts")
    with pytest.raises(ToolError, match=message):
        await fn(mock_context, department_headcount=value, extractor=extractor)
    extractor.search_sales_navigator_accounts.assert_not_awaited()
