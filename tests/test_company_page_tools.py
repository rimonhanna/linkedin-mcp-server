"""Company page invite tools: argument handling, pacing and delegation."""

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import FunctionTool

from linkedin_mcp_server.exceptions import ActionLimitError
from linkedin_mcp_server.pacing import PAGE_INVITES
from linkedin_mcp_server.sequential_tool_middleware import (
    SequentialToolExecutionMiddleware,
)
from linkedin_mcp_server.tools import company_page
from linkedin_mcp_server.tools.company_page import register_company_page_tools

RESULT = {"status": "dry_run", "people": []}
PROFILE = "ACoAAAAAEr4Bk7u-3pIa8wI55GOCgj6psu4_Jeo"


def _extractor() -> MagicMock:
    mock = MagicMock()
    mock.list_page_follow_candidates = AsyncMock(return_value={"candidates": []})
    mock.invite_to_follow_page = AsyncMock(return_value=RESULT)
    return mock


async def _tool(name: str) -> FunctionTool:
    mcp = FastMCP("test")
    register_company_page_tools(mcp)
    tool = await mcp.get_tool(name)
    assert tool is not None
    return cast(FunctionTool, tool)


async def test_the_list_is_read_only_and_the_invite_is_a_write():
    listing = await _tool("list_page_follow_candidates")
    invite = await _tool("invite_to_follow_page")
    assert listing.annotations and listing.annotations.readOnlyHint
    assert invite.annotations and invite.annotations.destructiveHint
    assert "confirm_send" in invite.parameters["required"]
    assert "invite_to_follow_page" in SequentialToolExecutionMiddleware._WRITE_TOOLS


async def test_the_company_id_is_reduced_from_an_admin_url(mock_context):
    extractor = _extractor()
    fn = (await _tool("list_page_follow_candidates")).fn
    await fn(
        "https://www.linkedin.com/company/100000701/admin/dashboard/",
        mock_context,
        extractor=extractor,
    )
    args, kwargs = extractor.list_page_follow_candidates.call_args
    assert args == ("100000701",)
    assert kwargs["limit"] == 20


@pytest.mark.parametrize(
    ("people", "message"),
    [
        (["urn:li:company:1"], r"people\[0\] is not a LinkedIn profile"),
        ([PROFILE, "Some Person"], r"people\[1\] is not a LinkedIn profile"),
        ([], "at least one person"),
        ([f"urn:li:member:{n}" for n in range(21)], "at most 20"),
    ],
)
async def test_unreadable_people_are_refused_before_the_browser(
    mock_context, people, message
):
    extractor = _extractor()
    fn = (await _tool("invite_to_follow_page")).fn
    with pytest.raises(ToolError, match=message):
        await fn("100000701", people, False, mock_context, extractor=extractor)
    extractor.invite_to_follow_page.assert_not_awaited()


async def test_every_identifier_form_is_passed_on_once(mock_context):
    extractor = _extractor()
    fn = (await _tool("invite_to_follow_page")).fn
    people = [
        PROFILE,
        f" {PROFILE} ",
        "urn:li:member:4798",
        "https://www.linkedin.com/in/some-person/",
        "urn:li:fs_salesProfile:(ACwAAAAAEr4Bk7u-3pIa8wI55GOCgj6psu4_Jeo,NAME_SEARCH,x1)",
    ]
    await fn("100000701", people, False, mock_context, extractor=extractor)
    args, kwargs = extractor.invite_to_follow_page.call_args
    assert args == ("100000701", [PROFILE, *people[2:]])
    assert kwargs["confirm_send"] is False


async def test_a_confirmed_invite_is_refused_by_the_cap_before_the_browser(
    mock_context, monkeypatch
):
    seen: list[str] = []

    def refuse(_store, kind, _now):
        seen.append(kind)
        raise ActionLimitError(kind, limit=20, window="24 h", resume_at=_now)

    monkeypatch.setattr(company_page, "refuse_action", refuse)
    extractor = _extractor()
    fn = (await _tool("invite_to_follow_page")).fn
    with pytest.raises(ToolError):
        await fn("100000701", [PROFILE], True, mock_context, extractor=extractor)
    assert seen == [PAGE_INVITES]
    extractor.invite_to_follow_page.assert_not_awaited()

    seen.clear()
    await fn("100000701", [PROFILE], False, mock_context, extractor=extractor)
    assert seen == []
    extractor.invite_to_follow_page.assert_awaited_once()
