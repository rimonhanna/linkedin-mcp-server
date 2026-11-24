"""Compose routes and refusals of the Sales Navigator send tools (synthetic ids)."""

import pytest

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.scraping.sales_navigator_send_contract import (
    compose_route_urn,
    refuse_an_invalid_sales_message,
    refuse_an_invalid_sales_reply,
    sales_compose_url,
    sales_profile_urn,
)

LEAD = "ACwAAA1000001"
FULL = f"urn:li:fs_salesProfile:({LEAD},NAME_SEARCH,abcd)"
THREAD = "2-MTAwMDAwMDAxXzEwMDAwMDAwMg=="


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (LEAD, f"urn:li:fs_salesProfile:({LEAD},,)"),
        (FULL, FULL),
        (f"https://www.linkedin.com/sales/lead/{LEAD},NAME_SEARCH,abcd", FULL),
    ],
)
def test_every_lead_form_becomes_one_urn(value, expected):
    assert sales_profile_urn(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        f"urn:li:fs_salesProfile:({LEAD},NAME(SEARCH,abcd)",
        f"urn:li:fs_salesProfile:({LEAD},NAME_SEARCH)",
        "urn:li:member:1000001",
        "../../feed",
    ],
)
def test_a_malformed_lead_is_refused(value):
    with pytest.raises(InvalidReferenceError):
        sales_profile_urn(value)


def test_compose_url_round_trips_to_the_same_urn():
    url = sales_compose_url(FULL)

    assert url.startswith("https://www.linkedin.com/sales/inbox/mobile-compose/")
    assert "(" not in url and "," not in url
    assert compose_route_urn(url) == FULL


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/sales/inbox/compose",
        f"https://www.linkedin.com/sales/inbox/{THREAD}",
        "https://evil.example/sales/inbox/mobile-compose/x",
        "https://www.linkedin.com/sales/inbox/mobile-compose/a/b",
    ],
)
def test_other_pages_are_not_the_composer(url):
    assert compose_route_urn(url) is None


def test_a_valid_message_and_subject_are_not_refused():
    assert refuse_an_invalid_sales_message(LEAD, "Hi\nthere", "Hello") is None
    assert refuse_an_invalid_sales_reply(THREAD, "Hi\nthere") is None


@pytest.mark.parametrize(
    ("message", "subject"),
    [
        ("   ", None),
        ("tab\there", None),
        ("fine", "two\nlines"),
        ("fine", "carriage\rreturn"),
        ("fine", " "),
    ],
)
def test_unsafe_text_is_refused_with_the_lead(message, subject):
    refusal = refuse_an_invalid_sales_message(LEAD, message, subject)

    assert refusal is not None
    assert refusal["status"] == "invalid_message"
    assert refusal["retry_safe"] is True and refusal["sent"] is False
    assert refusal["recipient"] == {"leadId": LEAD}
    assert refusal["delivery"] is None
    assert "thread_id" not in refusal


def test_an_unsafe_reply_is_refused_with_the_thread():
    refusal = refuse_an_invalid_sales_reply(
        f"https://www.linkedin.com/sales/inbox/{THREAD}", "bell\x07"
    )

    assert refusal is not None
    assert refusal["status"] == "invalid_message"
    assert refusal["thread_id"] == THREAD
    assert refusal["participant"] is None
