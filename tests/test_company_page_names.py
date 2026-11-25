"""invite_to_follow_page's names: one per person, passed on in step with people."""

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import FunctionTool

from linkedin_mcp_server.tools.company_page import register_company_page_tools

# Synthetic: shaped like profile ids, name no one.
FIRST = "ACoAAExampleFakeProfileId0001"
SECOND = "ACoAAExampleFakeProfileId0002"


async def _call(mock_context, people, names) -> MagicMock:
    mcp = FastMCP("test")
    register_company_page_tools(mcp)
    fn = cast(FunctionTool, await mcp.get_tool("invite_to_follow_page")).fn
    extractor = MagicMock()
    extractor.invite_to_follow_page = AsyncMock(
        return_value={"status": "dry_run", "people": []}
    )
    await fn("100000701", people, False, mock_context, names, extractor=extractor)
    return extractor


async def test_names_follow_their_people_through_the_dedupe(mock_context):
    extractor = await _call(
        mock_context, [FIRST, f" {FIRST} ", SECOND], ["Example One", "Other", ""]
    )
    args, kwargs = extractor.invite_to_follow_page.call_args
    assert args == ("100000701", [FIRST, SECOND])
    assert kwargs["names"] == ["Example One", None]


async def test_no_names_passes_none(mock_context):
    extractor = await _call(mock_context, [FIRST], None)
    assert extractor.invite_to_follow_page.call_args.kwargs["names"] is None


@pytest.mark.parametrize(
    ("names", "message"),
    [
        (["Example One"], "one name per person"),
        (["Example One", "x" * 101], r"names\[1\] must be plain text"),
    ],
)
async def test_unusable_names_are_refused_before_the_browser(
    mock_context, names, message
):
    mcp = FastMCP("test")
    register_company_page_tools(mcp)
    fn = cast(FunctionTool, await mcp.get_tool("invite_to_follow_page")).fn
    extractor = MagicMock()
    extractor.invite_to_follow_page = AsyncMock()
    with pytest.raises(ToolError, match=message):
        await fn(
            "100000701",
            [FIRST, SECOND],
            False,
            mock_context,
            names,
            extractor=extractor,
        )
    extractor.invite_to_follow_page.assert_not_awaited()
