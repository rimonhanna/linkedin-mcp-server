"""``connect_with_person`` when the invite dialog's note control is not found.

``tests/test_invite_note_control_dom.py`` runs the finder against a real DOM;
here the submit step is patched to report the control missing, and the
connect path must turn that into its own status without checking the sent
list, since nothing in the dialog was clicked.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from linkedin_mcp_server.scraping.connection import ActionSignals
from linkedin_mcp_server.scraping.connection_actions import ConnectionActions
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

DIALOG_TEXT = "Add a note to your invitation? Send without a note"
REPORT = (
    "no control other than Send and Dismiss is beside the Send button."
    " Clickable elements in the dialog: [{'tag': 'button', 'text': 'Send without a note'}]"
)
CONNECTABLE = ActionSignals(
    has_top_card=True,
    has_edit_intro_anchor=False,
    has_pending_item=False,
    has_connect_item=True,
    menu_read=True,
    has_recommendation_link=False,
    has_ignore_item=False,
    has_message_item_in_menu=False,
    has_compose_anchor_in_top_card=False,
)


def _actions(page) -> ConnectionActions:
    async def read(_username: str) -> dict[str, Any]:
        return {
            "url": "https://www.linkedin.com/in/testuser/",
            "sections": {"main_profile": "Jane\n\nConnect\nMore\n"},
        }

    session = ScrapingSession(page)
    return ConnectionActions(session, PageNavigator(session), read)


async def test_a_missing_note_control_is_its_own_status_and_sends_nothing(mock_page):
    actions = _actions(mock_page)

    with (
        patch.object(
            actions,
            "_read_action_signals",
            new_callable=AsyncMock,
            return_value=CONNECTABLE,
        ),
        patch.object(
            actions, "_click_connect", new_callable=AsyncMock, return_value=True
        ),
        patch.object(
            actions,
            "_submit_invite_dialog",
            new_callable=AsyncMock,
            return_value=(False, False, None, DIALOG_TEXT, REPORT),
        ) as mock_submit,
        patch.object(
            actions, "_invitation_in_sent_list", new_callable=AsyncMock
        ) as mock_sent,
    ):
        result = await actions.connect_with_person("testuser", note="Hello")

    mock_submit.assert_awaited_once_with("Hello")
    assert result["status"] == "note_control_not_found"
    assert result["note_sent"] is False
    assert "no invitation was sent" in result["message"]
    assert DIALOG_TEXT in result["message"]
    assert REPORT in result["message"]
    mock_sent.assert_not_awaited()


async def test_a_mark_on_more_than_one_element_clicks_nothing(mock_page):
    actions = _actions(mock_page)
    mock_page.evaluate = AsyncMock(
        return_value={"found": True, "reason": "", "controls": []}
    )
    marked = MagicMock()
    marked.count = AsyncMock(return_value=2)
    marked.click = AsyncMock()
    dialog = MagicMock()
    dialog.locator = MagicMock(return_value=marked)

    clicked, report = await actions._click_note_control(dialog)

    assert clicked is False
    assert "2 elements carry the note-control mark" in report
    marked.click.assert_not_awaited()
