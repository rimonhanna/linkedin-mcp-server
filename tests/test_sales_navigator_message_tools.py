"""Sales Navigator send tools: refusal before the browser, pacing, delegation."""

from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP

from linkedin_mcp_server.pacing import SALES_NAVIGATOR_MESSAGES
from linkedin_mcp_server.sequential_tool_middleware import (
    SequentialToolExecutionMiddleware,
)
from linkedin_mcp_server.tools.sales_navigator_inbox_actions import (
    register_sales_navigator_inbox_action_tools,
)

_MODULE = "linkedin_mcp_server.tools.sales_navigator_inbox_actions"
LEAD = "ACwAAA1000001"
THREAD = "2-MTAwMDAwMDAxXzEwMDAwMDAwMg=="

VALID = {
    "send_sales_navigator_message": {
        "lead_id": LEAD,
        "message": "Hello\nthere",
        "confirm_send": True,
    },
    "reply_to_sales_navigator_conversation": {
        "thread_id": THREAD,
        "message": "Hello\nthere",
        "confirm_send": True,
    },
}
TOOLS = tuple(VALID)
METHOD = {
    "send_sales_navigator_message": "send_sales_navigator_message",
    "reply_to_sales_navigator_conversation": "reply_to_sales_navigator_conversation",
}


def _mcp() -> FastMCP:
    mcp = FastMCP("test")
    register_sales_navigator_inbox_action_tools(mcp)
    return mcp


def _extractor(name: str, result: dict) -> MagicMock:
    extractor = MagicMock()
    setattr(extractor, METHOD[name], AsyncMock(return_value=result))
    return extractor


async def _call(name: str, args: dict, extractor) -> tuple[dict, MagicMock, MagicMock]:
    with (
        patch(f"{_MODULE}.get_ready_extractor", AsyncMock(return_value=extractor)),
        patch(f"{_MODULE}.refuse_action") as refuse,
        patch(f"{_MODULE}.record_action") as record,
        patch(f"{_MODULE}.JobStore"),
        patch(f"{_MODULE}.retain_trace_for_failed_action"),
    ):
        result = await _mcp().call_tool(name, args)
    return cast(dict, result.structured_content), refuse, record


def _kinds(mock: MagicMock) -> list[str]:
    return [call.args[1] for call in mock.call_args_list]


@pytest.mark.parametrize("name", TOOLS)
async def test_tools_are_destructive_write_tools(name):
    tool = await _mcp().get_tool(name)
    assert tool is not None and tool.annotations is not None
    assert tool.annotations.destructiveHint is True
    assert tool.tags == {"sales_navigator", "actions"}
    assert name in SequentialToolExecutionMiddleware._WRITE_TOOLS


@pytest.mark.parametrize("name", TOOLS)
async def test_confirm_send_is_required_with_no_default(name):
    tool = await _mcp().get_tool(name)
    assert tool is not None
    assert "confirm_send" in tool.parameters["required"]


async def test_spending_a_credit_defaults_to_off():
    tool = await _mcp().get_tool("send_sales_navigator_message")
    assert tool is not None
    assert tool.parameters["properties"]["spend_inmail_credit"]["default"] is False


@pytest.mark.parametrize("name", TOOLS)
async def test_an_invalid_message_is_refused_before_the_browser(name):
    extractor = _extractor(name, {})
    args = {**VALID[name], "message": "tab\there"}

    with patch(f"{_MODULE}.get_ready_extractor", AsyncMock()) as ready:
        result = await _mcp().call_tool(name, args)

    ready.assert_not_awaited()
    content = cast(dict, result.structured_content)
    assert content["status"] == "invalid_message"
    assert content["retry_safe"] is True
    getattr(extractor, METHOD[name]).assert_not_awaited()


@pytest.mark.parametrize("subject", ["two\nlines", "   ", "bell\x07"])
async def test_an_unsafe_subject_is_refused_before_the_browser(subject):
    args = {**VALID["send_sales_navigator_message"], "subject": subject}

    with patch(f"{_MODULE}.get_ready_extractor", AsyncMock()) as ready:
        result = await _mcp().call_tool("send_sales_navigator_message", args)

    ready.assert_not_awaited()
    assert cast(dict, result.structured_content)["status"] == "invalid_message"


@pytest.mark.parametrize("name", TOOLS)
async def test_a_dry_run_is_neither_refused_nor_counted(name):
    extractor = _extractor(
        name, {"status": "confirmation_required", "sent": False, "retry_safe": True}
    )

    _, refuse, record = await _call(
        name, {**VALID[name], "confirm_send": False}, extractor
    )

    refuse.assert_not_called()
    record.assert_not_called()


@pytest.mark.parametrize("name", TOOLS)
async def test_a_confirmed_send_is_refused_and_counted_on_its_own_cap(name):
    extractor = _extractor(name, {"status": "sent", "sent": True, "retry_safe": False})

    _, refuse, record = await _call(name, VALID[name], extractor)

    assert _kinds(refuse) == [SALES_NAVIGATOR_MESSAGES]
    assert _kinds(record) == [SALES_NAVIGATOR_MESSAGES]


@pytest.mark.parametrize("name", TOOLS)
async def test_an_unconfirmed_send_is_counted(name):
    extractor = _extractor(
        name, {"status": "send_unconfirmed", "sent": False, "retry_safe": False}
    )

    _, _, record = await _call(name, VALID[name], extractor)

    assert _kinds(record) == [SALES_NAVIGATOR_MESSAGES]


@pytest.mark.parametrize("name", TOOLS)
@pytest.mark.parametrize(
    "status", ["compose_interact_failed", "recipient_resolution_failed"]
)
async def test_typing_that_stopped_before_the_click_is_not_counted(name, status):
    extractor = _extractor(name, {"status": status, "sent": False, "retry_safe": False})

    _, _, record = await _call(name, VALID[name], extractor)

    record.assert_not_called()


@pytest.mark.parametrize("name", TOOLS)
async def test_any_other_status_that_is_not_retry_safe_is_counted(name):
    extractor = _extractor(
        name, {"status": "some_new_status", "sent": False, "retry_safe": False}
    )

    _, _, record = await _call(name, VALID[name], extractor)

    assert _kinds(record) == [SALES_NAVIGATOR_MESSAGES]


@pytest.mark.parametrize("name", TOOLS)
async def test_a_send_refused_before_typing_is_not_counted(name):
    extractor = _extractor(
        name, {"status": "inmail_credit_required", "sent": False, "retry_safe": True}
    )

    _, _, record = await _call(name, VALID[name], extractor)

    record.assert_not_called()


async def test_send_forwards_every_argument():
    extractor = _extractor("send_sales_navigator_message", {"status": "sent"})
    args = {
        **VALID["send_sales_navigator_message"],
        "subject": "Hello",
        "spend_inmail_credit": True,
    }

    await _call("send_sales_navigator_message", args, extractor)

    extractor.send_sales_navigator_message.assert_awaited_once_with(
        LEAD,
        "Hello\nthere",
        confirm_send=True,
        subject="Hello",
        spend_inmail_credit=True,
    )


async def test_reply_forwards_the_normalized_thread():
    extractor = _extractor("reply_to_sales_navigator_conversation", {"status": "sent"})
    args = {
        **VALID["reply_to_sales_navigator_conversation"],
        "thread_id": f"https://www.linkedin.com/sales/inbox/{THREAD}",
    }

    await _call("reply_to_sales_navigator_conversation", args, extractor)

    extractor.reply_to_sales_navigator_conversation.assert_awaited_once_with(
        THREAD, "Hello\nthere", confirm_send=True, spend_inmail_credit=False
    )


async def test_reply_forwards_the_credit_flag():
    extractor = _extractor("reply_to_sales_navigator_conversation", {"status": "sent"})
    args = {
        **VALID["reply_to_sales_navigator_conversation"],
        "spend_inmail_credit": True,
    }

    await _call("reply_to_sales_navigator_conversation", args, extractor)

    extractor.reply_to_sales_navigator_conversation.assert_awaited_once_with(
        THREAD, "Hello\nthere", confirm_send=True, spend_inmail_credit=True
    )
