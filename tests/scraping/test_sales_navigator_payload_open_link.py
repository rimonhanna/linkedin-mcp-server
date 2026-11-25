"""A lead search card's openLink is not reported (synthetic data)."""

from __future__ import annotations

from linkedin_mcp_server.scraping.sales_navigator_payload import build_lead

from .test_sales_navigator_payload import _lead_card


def test_a_lead_carries_no_open_profile_flag_from_the_search_card():
    # The search request never asks for openLink, so the card's value is a
    # default, whatever it says.
    lead = build_lead(_lead_card(openLink=True))

    assert lead is not None
    assert "openLink" not in lead, "openLink from a search card is no signal"
