"""The Sales Navigator account-list write tools at the tool layer."""

from datetime import datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP
from fastmcp.tools import FunctionTool

from linkedin_mcp_server.tools import sales_navigator_lists as tools
from linkedin_mcp_server.tools.sales_navigator_lists import (
    register_sales_navigator_list_tools,
)

TOOLS = (
    "create_sales_navigator_account_list",
    "add_accounts_to_sales_navigator_list",
    "delete_sales_navigator_account_list",
)
RESULT = {"url": "https://www.linkedin.com/sales/lists/company", "status": "dry_run"}


def _extractor() -> MagicMock:
    mock = MagicMock()
    for name in TOOLS:
        setattr(mock, name, AsyncMock(return_value=RESULT))
    return mock


async def _fn(name: str):
    mcp = FastMCP("test")
    register_sales_navigator_list_tools(mcp)
    tool = await mcp.get_tool(name)
    assert tool is not None
    return cast(FunctionTool, tool).fn


@pytest.fixture
def refuse():
    with patch.object(tools, "refuse_action") as mock:
        yield mock


@pytest.mark.parametrize("name", TOOLS)
async def test_the_list_writes_claim_destructive_and_never_read_only(name):
    mcp = FastMCP("test")
    register_sales_navigator_list_tools(mcp)
    tool = await mcp.get_tool(name)
    assert tool is not None
    assert tool.annotations is not None
    assert tool.annotations.destructiveHint is True
    assert not tool.annotations.readOnlyHint
    assert tool.tags == {"sales_navigator", "actions"}


async def test_a_dry_run_owes_the_cap_nothing(mock_context, refuse):
    extractor = _extractor()
    fn = await _fn("create_sales_navigator_account_list")
    result = await fn(
        "Example list", False, mock_context, accounts=["1"], extractor=extractor
    )
    assert result == RESULT
    refuse.assert_not_called()
    extractor.create_sales_navigator_account_list.assert_awaited_once_with(
        "Example list", description=None, accounts=["1"], confirm_send=False
    )


async def test_a_real_write_is_refused_by_the_cap_before_the_browser(
    mock_context, refuse
):
    from linkedin_mcp_server.exceptions import ActionLimitError

    refuse.side_effect = ActionLimitError(
        "sales_list_writes",
        limit=30,
        window="24 h",
        resume_at=datetime(2099, 1, 1),
    )
    extractor = _extractor()
    fn = await _fn("add_accounts_to_sales_navigator_list")
    with pytest.raises(Exception):
        await fn("100000301", ["1"], True, mock_context, extractor=extractor)
    assert refuse.call_args.args[1] == "sales_list_writes"
    extractor.add_accounts_to_sales_navigator_list.assert_not_called()


async def test_accounts_are_normalized_and_deduplicated_before_delegation(
    mock_context, refuse
):
    extractor = _extractor()
    fn = await _fn("add_accounts_to_sales_navigator_list")
    await fn(
        "https://www.linkedin.com/sales/accounts/dashboard?listId=100000301",
        ["100000001", "urn:li:organization:100000001", "100000002"],
        True,
        mock_context,
        extractor=extractor,
    )
    extractor.add_accounts_to_sales_navigator_list.assert_awaited_once_with(
        "100000301", ["100000001", "100000002"], confirm_send=True
    )


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("create_sales_navigator_account_list", ("  ", True)),
        ("create_sales_navigator_account_list", ("x" * 81, True)),
        ("create_sales_navigator_account_list", ("two\nlines", True)),
        ("add_accounts_to_sales_navigator_list", ("100000301", [], True)),
        ("add_accounts_to_sales_navigator_list", ("100000301", ["acme"], True)),
        (
            "add_accounts_to_sales_navigator_list",
            ("100000301", [str(100000001 + i) for i in range(26)], True),
        ),
        ("delete_sales_navigator_account_list", ("100000301", " ", True)),
    ],
)
async def test_invalid_input_is_refused_before_the_browser(
    mock_context, refuse, name, args
):
    extractor = _extractor()
    fn = await _fn(name)
    result = await fn(*args, mock_context, extractor=extractor)
    assert result["status"] == "invalid_request"
    assert result["retry_safe"] is True
    refuse.assert_not_called()
    getattr(extractor, name).assert_not_called()


async def test_delete_passes_the_exact_id_and_expected_name(mock_context, refuse):
    extractor = _extractor()
    fn = await _fn("delete_sales_navigator_account_list")
    await fn("100000301", "Example list", True, mock_context, extractor=extractor)
    extractor.delete_sales_navigator_account_list.assert_awaited_once_with(
        "100000301", expected_name="Example list", confirm_send=True
    )
    assert refuse.call_args.args[1] == "sales_list_writes"
