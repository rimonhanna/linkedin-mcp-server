"""Locale-independent connection-state classification tests."""

import pytest

from linkedin_mcp_server.scraping.connection import (
    ActionSignals,
    detect_connection_state,
)


def _signals(
    top_card: bool = True,
    edit: bool = False,
    pending: bool = False,
    connect: bool = False,
    menu: bool = True,
    recommendation: bool = False,
    ignore: bool = False,
    message_in_menu: bool = False,
    compose: bool = False,
) -> ActionSignals:
    return ActionSignals(
        has_top_card=top_card,
        has_edit_intro_anchor=edit,
        has_pending_item=pending,
        has_connect_item=connect,
        menu_read=menu,
        has_recommendation_link=recommendation,
        has_ignore_item=ignore,
        has_message_item_in_menu=message_in_menu,
        has_compose_anchor_in_top_card=compose,
    )


class TestDetectConnectionState:
    def test_self_profile(self):
        assert detect_connection_state(_signals(edit=True)) == "self_profile"

    def test_self_profile_needs_no_top_card(self):
        assert (
            detect_connection_state(_signals(edit=True, top_card=False, menu=False))
            == "self_profile"
        )

    def test_pending_from_the_menu(self):
        assert detect_connection_state(_signals(pending=True)) == "pending"

    def test_pending_from_the_top_card_without_a_menu_read(self):
        assert detect_connection_state(_signals(pending=True, menu=False)) == "pending"

    def test_connectable_from_the_menu(self):
        assert detect_connection_state(_signals(connect=True)) == "connectable"

    def test_connectable_from_the_top_card_without_a_menu_read(self):
        assert (
            detect_connection_state(_signals(connect=True, menu=False)) == "connectable"
        )

    def test_pending_wins_over_connectable(self):
        # Both keys at once is a contradiction; the answer that cannot
        # trigger a write wins.
        assert (
            detect_connection_state(_signals(pending=True, connect=True)) == "pending"
        )

    def test_already_connected_from_recommendation_links(self):
        assert (
            detect_connection_state(_signals(recommendation=True, compose=True))
            == "already_connected"
        )

    def test_pending_wins_over_already_connected(self):
        # The case upstream settled with the sent list: Message in the top
        # card and Pending in the menu.
        assert (
            detect_connection_state(
                _signals(pending=True, recommendation=True, compose=True)
            )
            == "pending"
        )

    def test_incoming_request_from_ignore_and_a_menu_message(self):
        assert (
            detect_connection_state(_signals(ignore=True, message_in_menu=True))
            == "incoming_request"
        )

    @pytest.mark.parametrize(
        "signals",
        [
            # The icon alone: one design token is too thin to accept on.
            _signals(ignore=True),
            # Message in the top card, as on 1st-degree and pending rows.
            _signals(ignore=True, message_in_menu=True, compose=True),
        ],
        ids=["icon-only", "message-in-top-card"],
    )
    def test_half_an_incoming_request_is_unavailable(self, signals):
        # Neither incoming nor the confident follow_only: nothing measured
        # explains these signals.
        assert detect_connection_state(signals) == "unavailable"

    def test_a_menu_message_alone_is_not_incoming(self):
        assert detect_connection_state(_signals(message_in_menu=True)) == (
            "follow_only"
        )

    def test_recommendation_links_win_over_the_ignore_item(self):
        assert (
            detect_connection_state(
                _signals(recommendation=True, ignore=True, message_in_menu=True)
            )
            == "already_connected"
        )

    def test_follow_only_when_the_menu_offers_no_connect(self):
        assert detect_connection_state(_signals(compose=True)) == "follow_only"

    def test_unavailable_without_a_top_card(self):
        assert (
            detect_connection_state(_signals(top_card=False, menu=False))
            == "unavailable"
        )

    @pytest.mark.parametrize(
        "signals",
        [
            _signals(menu=False),
            _signals(menu=False, compose=True),
            # Menu-only signals mean nothing when the menu was never read.
            _signals(menu=False, recommendation=True),
            _signals(menu=False, ignore=True),
        ],
        ids=["bare", "compose", "recommendation", "ignore"],
    )
    def test_unavailable_when_the_menu_could_not_be_read(self, signals):
        assert detect_connection_state(signals) == "unavailable"

    def test_no_signals_is_never_pending(self):
        # The regression: a page with nothing to say said "pending".
        assert detect_connection_state(_signals()) == "follow_only"
        assert (
            detect_connection_state(_signals(top_card=False, menu=False))
            == "unavailable"
        )
