"""connect_with_person invites a member named by id only once a page proves who.

An id is opened at ``/in/<id>/``, which LinkedIn redirects to the member's
public address (measured 2026-11-27). The public identifier is taken from
where the page landed and only when the page's profile URN is the member asked
for, and the profile page every click lands on is checked again. Whatever
cannot be confirmed is refused before any click: nothing reaches the invite
dialog, the custom-invite deeplink or the sent list.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from linkedin_mcp_server.scraping.connect_target import ConnectTarget, connect_target
from linkedin_mcp_server.scraping.connection import ActionSignals
from linkedin_mcp_server.scraping.connection_actions import ConnectionActions
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession


def _encoded(header: bytes, member: int) -> str:
    raw = header + member.to_bytes(4, "big") + b"\x01" * 21
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


SN_LEAD_ID = _encoded(b"\x00\x2c\x00\x00", 185140733)
PROFILE_ID = _encoded(b"\x00\x2a\x00\x00", 185140733)
OTHER_PROFILE_ID = _encoded(b"\x00\x2a\x00\x00", 185140734)

PAGE_TEXT = "Jane\n\n· 2nd\n\nEngineer\n\nConnect\nMore\nAbout\n"
VANITY = "jane-doe"
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


def _page(urn: str | None, public: str | None = VANITY) -> dict[str, Any]:
    page: dict[str, Any] = {
        "url": "https://www.linkedin.com/in/x/",
        "sections": {"main_profile": PAGE_TEXT},
    }
    if urn is not None:
        page["profile_urn"] = urn
    if public is not None:
        page["public_profile_url"] = f"https://www.linkedin.com/in/{public}/"
    return page


def _target() -> ConnectTarget:
    target = connect_target(SN_LEAD_ID)
    assert isinstance(target, ConnectTarget)
    return target


def _actions(mock_page, *pages: dict[str, Any]) -> tuple[ConnectionActions, AsyncMock]:
    read = AsyncMock(side_effect=list(pages))
    session = ScrapingSession(mock_page)
    return ConnectionActions(session, PageNavigator(session), read), read


@contextmanager
def _write_spies(actions: ConnectionActions) -> Iterator[list[AsyncMock]]:
    """Every way the flow could act, patched to record whether it was reached."""
    with ExitStack() as stack:
        yield [
            stack.enter_context(
                patch.object(
                    actions,
                    "_read_action_signals",
                    new_callable=AsyncMock,
                    return_value=CONNECTABLE,
                )
            ),
            stack.enter_context(
                patch.object(
                    actions,
                    "_click_connect",
                    new_callable=AsyncMock,
                    return_value=True,
                )
            ),
            stack.enter_context(
                patch.object(
                    actions,
                    "_submit_invite_dialog",
                    new_callable=AsyncMock,
                    return_value=(True, False, None, "", None),
                )
            ),
            stack.enter_context(
                patch.object(
                    actions,
                    "_invitation_in_sent_list",
                    new_callable=AsyncMock,
                    return_value=True,
                )
            ),
            stack.enter_context(
                patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock)
            ),
        ]


def _assert_nothing_sent(result: dict[str, Any], spies: list[AsyncMock]) -> None:
    signals, click, submit, sent, navigate = spies
    assert result["status"] == "identity_unconfirmed"
    assert result["note_sent"] is False
    assert "no invitation was sent" in result["message"]
    click.assert_not_awaited()
    submit.assert_not_awaited()
    sent.assert_not_awaited()
    navigate.assert_not_awaited()


async def test_a_resolved_member_is_invited_at_their_public_address(mock_page):
    actions, read = _actions(mock_page, _page(PROFILE_ID), _page(PROFILE_ID))

    with _write_spies(actions) as spies:
        result = await actions.connect_with_person(_target())

    _signals, click, _submit, sent, _navigate = spies
    assert result["status"] == "connected"
    assert result["url"] == f"https://www.linkedin.com/in/{VANITY}/"
    assert [call.args[0] for call in read.await_args_list] == [SN_LEAD_ID, VANITY]
    click.assert_awaited_once()
    sent.assert_awaited_once_with(VANITY)


async def test_another_member_on_the_landed_page_is_refused(mock_page):
    actions, read = _actions(mock_page, _page(OTHER_PROFILE_ID))

    with _write_spies(actions) as spies:
        result = await actions.connect_with_person(_target())

    _assert_nothing_sent(result, spies)
    assert "profile URN" in result["message"]
    read.assert_awaited_once_with(SN_LEAD_ID)
    spies[0].assert_not_awaited()


@pytest.mark.parametrize(
    "landed",
    [
        _page(None),
        _page(PROFILE_ID, public=None),
        # Still on the id: LinkedIn did not redirect to a public address.
        _page(PROFILE_ID, public=SN_LEAD_ID),
    ],
    ids=["no-urn", "no-public-address", "no-redirect"],
)
async def test_a_landing_that_proves_nothing_is_refused(mock_page, landed):
    actions, read = _actions(mock_page, landed)

    with _write_spies(actions) as spies:
        result = await actions.connect_with_person(_target())

    _assert_nothing_sent(result, spies)
    read.assert_awaited_once()


async def test_the_page_acted_on_is_checked_again(mock_page):
    """The vanity resolved, but the page loaded under it is someone else."""
    actions, read = _actions(mock_page, _page(PROFILE_ID), _page(OTHER_PROFILE_ID))

    with _write_spies(actions) as spies:
        result = await actions.connect_with_person(_target())

    _assert_nothing_sent(result, spies)
    assert result["url"] == f"https://www.linkedin.com/in/{VANITY}/"
    assert read.await_count == 2
    spies[0].assert_not_awaited()


async def test_a_public_identifier_is_not_asked_to_prove_itself(mock_page):
    """A vanity input reads one page and needs no URN, as before."""
    actions, read = _actions(mock_page, _page(None, public=None))

    with _write_spies(actions) as spies:
        result = await actions.connect_with_person(VANITY)

    assert result["status"] == "connected"
    read.assert_awaited_once_with(VANITY)
    spies[1].assert_awaited_once()
