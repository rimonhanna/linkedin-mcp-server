"""Who the signed-in member is in an inbox response (synthetic data)."""

from __future__ import annotations

from typing import Any

from linkedin_mcp_server.scraping.sales_navigator_inbox_payload import (
    chrome_viewer,
    no_inbox_access,
    viewer_of,
)

VIEWER = "urn:li:fs_salesProfile:(ACwAAA0000,NAME_SEARCH,v0)"
LEAD = "urn:li:fs_salesProfile:(ACwAAA0001,NAME_SEARCH,t1)"
OTHER = "urn:li:fs_salesProfile:(ACwAAA0002,NAME_SEARCH,t2)"


def _profiles(viewer_degree: int = 0, lead_degree: int = 2) -> list[dict[str, Any]]:
    return [
        {
            "entityUrn": VIEWER,
            "objectUrn": "urn:li:member:100000000",
            "degree": viewer_degree,
        },
        {
            "entityUrn": LEAD,
            "objectUrn": "urn:li:member:100000001",
            "degree": lead_degree,
        },
        {"entityUrn": OTHER, "objectUrn": "urn:li:member:100000002", "degree": 2},
    ]


def _list(*participants: list[str], **profile_args: int) -> dict[str, Any]:
    threads = [
        {"id": f"2-T{n}", "participants": list(names)}
        for n, names in enumerate(participants)
    ]
    return {
        "data": {"elements": threads, "paging": {}},
        "included": _profiles(**profile_args),
    }


def test_the_chrome_member_who_is_degree_zero_and_in_every_thread_is_the_viewer():
    payload = _list([VIEWER, LEAD], [OTHER, VIEWER])

    assert viewer_of(payload, VIEWER.replace("v0", "other-token")) == (
        "ACwAAA0000",
        True,
    )
    assert viewer_of(payload, "urn:li:member:100000000") == ("ACwAAA0000", True)


def test_without_the_chrome_the_degree_zero_member_in_every_thread_is_the_viewer():
    assert viewer_of(_list([VIEWER, LEAD], [VIEWER, OTHER]), None) == (
        "ACwAAA0000",
        True,
    )


def test_a_chrome_member_who_is_not_the_degree_zero_participant_is_refused():
    payload = _list([VIEWER, LEAD])

    assert viewer_of(payload, LEAD) == (None, False), (
        "the chrome naming the lead while the viewer is degree 0 must refuse"
    )


def test_a_chrome_member_missing_from_a_thread_is_refused():
    payload = _list([VIEWER, LEAD], [LEAD, OTHER])

    assert viewer_of(payload, VIEWER) == (None, False), (
        "the chrome's member must be a participant of every thread"
    )


def test_a_chrome_member_nobody_matches_is_refused():
    assert viewer_of(_list([VIEWER, LEAD]), "urn:li:member:999") == (None, False)


def test_no_degree_zero_participant_and_no_chrome_is_refused():
    assert viewer_of(_list([VIEWER, LEAD], viewer_degree=2), None) == (None, False)


def test_a_single_thread_response_is_checked_the_same_way():
    record = {
        "data": {"id": "2-T", "participants": [VIEWER, LEAD]},
        "included": _profiles(),
    }

    assert viewer_of(record, VIEWER) == ("ACwAAA0000", True)
    assert viewer_of(record, LEAD) == (None, False)


def test_a_response_with_no_thread_names_no_viewer():
    assert viewer_of(_list(), None) == (None, True)


def test_the_chrome_viewer_is_read_only_in_a_recognised_form():
    assert chrome_viewer({"viewerSalesUrn": VIEWER}) == VIEWER
    assert chrome_viewer({"data": {"viewerSalesUrn": VIEWER}, "included": []}) == VIEWER
    assert chrome_viewer({"member": "urn:li:member:1"}) == "urn:li:member:1"
    assert chrome_viewer({"viewerSalesUrn": "urn:li:fs_salesSeat:1"}) is None
    assert chrome_viewer({"elements": []}) is None


def test_no_inbox_access_is_only_an_explicit_false():
    assert no_inbox_access({"hasContractInboxAccess": False}) is True
    assert no_inbox_access({"data": {"hasContractInboxAccess": False}}) is True
    assert no_inbox_access({"hasContractInboxAccess": True}) is False
    assert no_inbox_access({}) is False
    assert no_inbox_access(None) is False
