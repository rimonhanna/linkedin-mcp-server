"""Tests for the Sales Navigator inbox identifier helpers (synthetic ids)."""

import pytest

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.scraping.identifiers import (
    SALES_INBOX_URL,
    normalize_sales_lead_id,
    normalize_sales_thread_id,
    sales_inbox_thread_url,
    sales_inbox_url,
)

THREAD = "2-ZXhhbXBsZQ=="


@pytest.mark.parametrize(
    "value",
    [
        THREAD,
        f" {THREAD} ",
        f"https://www.linkedin.com/sales/inbox/{THREAD}",
        f"https://www.linkedin.com/sales/inbox/{THREAD}?filter=UNREAD",
        f"/sales/inbox/{THREAD}/",
    ],
)
def test_thread_id_accepts_the_id_and_its_inbox_url(value):
    assert normalize_sales_thread_id(value) == THREAD


@pytest.mark.parametrize(
    "value",
    [
        "",
        "compose",
        "https://www.linkedin.com/sales/inbox/compose",
        "/sales/inbox/mobile-compose",
        "../feed",
        "2-abc/../../feed",
    ],
)
def test_thread_id_refuses_composers_and_paths(value):
    with pytest.raises(InvalidReferenceError, match="thread_id"):
        normalize_sales_thread_id(value)


@pytest.mark.parametrize(
    "value",
    [
        "ACwAAA0001",
        " ACwAAA0001 ",
        "urn:li:fs_salesProfile:(ACwAAA0001,NAME_SEARCH,abc)",
        "https://www.linkedin.com/sales/lead/ACwAAA0001,NAME_SEARCH,abc",
        "/sales/lead/ACwAAA0001,NAME_SEARCH,abc?trk=x",
    ],
)
def test_lead_id_accepts_the_id_its_urn_and_its_lead_url(value):
    assert normalize_sales_lead_id(value) == "ACwAAA0001"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "urn:li:member:100000001",
        "urn:li:fs_salesProfile:(,NAME_SEARCH,abc)",
        "https://www.linkedin.com/in/example-person/",
        "ACwAAA0001/../x",
    ],
)
def test_lead_id_refuses_anything_else(value):
    with pytest.raises(InvalidReferenceError, match="lead_id"):
        normalize_sales_lead_id(value)


def test_urls_escape_the_id_and_carry_only_the_filter():
    assert sales_inbox_url() == SALES_INBOX_URL
    assert sales_inbox_url("UNREAD") == f"{SALES_INBOX_URL}?filter=UNREAD"
    assert sales_inbox_thread_url(THREAD) == f"{SALES_INBOX_URL}/2-ZXhhbXBsZQ%3D%3D"
