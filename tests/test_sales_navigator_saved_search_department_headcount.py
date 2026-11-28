"""save_sales_navigator_search: the department_headcount parameter."""

import pytest
from fastmcp import FastMCP

from linkedin_mcp_server.tools.sales_navigator_saved_search import (
    register_sales_navigator_saved_search_tools,
)

from test_sales_navigator_saved_search_tools import TOOL, _extractor, _fn

ENGINEERING = {"department": "Engineering", "min": 50, "max": 500}


async def test_the_schema_offers_department_headcount():
    mcp = FastMCP("test")
    register_sales_navigator_saved_search_tools(mcp)
    tool = await mcp.get_tool(TOOL)
    assert tool is not None
    schema = tool.parameters
    param = schema["properties"]["department_headcount"]
    ref = param["anyOf"][0]["$ref"].rsplit("/", 1)[-1]
    assert set(schema["$defs"][ref]["properties"]) == {"department", "min", "max"}
    assert "department_headcount" not in schema.get("required", [])


async def test_an_account_save_passes_department_headcount_on(mock_context):
    extractor = _extractor()
    fn = await _fn()

    await fn(
        kind="accounts",
        name="Example",
        confirm_send=False,
        ctx=mock_context,
        department_headcount=ENGINEERING,
        extractor=extractor,
    )

    assert extractor.save_sales_navigator_search.await_args.kwargs == {
        "criteria": {"department_headcount": ENGINEERING},
        "confirm_send": False,
    }


@pytest.mark.parametrize(
    ("kind", "value", "message"),
    [
        (
            "leads",
            ENGINEERING,
            "department_headcount is a filter of the accounts search",
        ),
        (
            "accounts",
            {"department": "Underwater Basketry", "min": 1},
            "Unknown function",
        ),
        ("accounts", {"department": "engineering"}, "needs a min or a max"),
        (
            "accounts",
            {"department": "engineering", "min": 500, "max": 50},
            "min must not be greater than its max",
        ),
    ],
)
async def test_a_bad_department_headcount_is_refused_before_the_browser(
    mock_context, kind, value, message
):
    extractor = _extractor()
    fn = await _fn()

    result = await fn(
        kind=kind,
        name="Example",
        confirm_send=False,
        ctx=mock_context,
        department_headcount=value,
        extractor=extractor,
    )

    assert result["status"] == "invalid_request"
    assert message in result["message"]
    extractor.save_sales_navigator_search.assert_not_awaited()
