"""add_post_to_campaign refuses a feed activity id before the browser."""

from unittest.mock import patch

import pytest
from fastmcp.exceptions import ToolError

from test_campaign_manager_action_tools import _MODULE, VALID, _extractor, _mcp, _ready

ACTIVITY = "urn:li:activity:100000801"


@pytest.mark.parametrize(
    "post_urn",
    [ACTIVITY, f"https://www.linkedin.com/feed/update/{ACTIVITY}/"],
    ids=["urn", "feed-link"],
)
async def test_an_activity_is_refused_with_the_way_out(post_urn):
    args = {**VALID["add_post_to_campaign"], "post_urn": post_urn}

    with (
        patch(f"{_MODULE}.refuse_action") as refuse,
        patch(f"{_MODULE}.record_action") as record,
        _ready(_extractor()) as ready,
        pytest.raises(ToolError) as raised,
    ):
        await _mcp().call_tool("add_post_to_campaign", args)

    message = str(raised.value)
    assert "Campaign Manager doesn't link activity ids to posts" in message
    assert "list_sponsorable_posts" in message
    ready.assert_not_awaited()
    refuse.assert_not_called()
    record.assert_not_called()
