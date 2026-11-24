"""The invite dialog's payload readers. Payloads are hand-written in the shapes
measured on a page admin's dialog; no value is LinkedIn's."""

from __future__ import annotations

import pytest

from linkedin_mcp_server.scraping import page_invite_payload as p

COMPANY = "100000701"


def _urn(profile_id: str) -> str:
    return f"urn:li:fsd_profile:{profile_id}"


def _suggestions(*ids: str, start: int = 0) -> dict:
    return {
        "data": {
            "elements": [{"suggestedProfile": _urn(i)} for i in ids],
            "paging": {"start": start, "count": len(ids), "links": []},
        },
        "included": [
            {
                "entityUrn": _urn(i),
                "firstName": "Example",
                "lastName": f"Person {i}",
                "headline": "Example headline",
                "publicIdentifier": f"example-{i}",
            }
            for i in ids
        ],
    }


def test_a_dialog_call_is_recognised_and_the_dashboard_card_is_too():
    call = p.parse_invite_call(
        "https://www.linkedin.com/voyager/api/"
        f"{p.SUGGESTIONS}?q=community&community=urn%3Ali%3Afs_normalized_company%3A"
        f"{COMPANY}&start=20&count=10",
        "get",
    )
    assert call is not None
    assert call.resource == p.SUGGESTIONS
    assert call.method == "GET"
    assert call.params["community"] == p.dialog_community(COMPANY)
    assert call.params["start"] == "20"


def test_only_the_connections_typeahead_counts_as_a_search():
    base = "https://www.linkedin.com/voyager/api/graphql?queryId="
    search = p.parse_invite_call(
        base + p.TYPEAHEAD + ".abc&variables=(type:CONNECTIONS,keywords:an)", "GET"
    )
    other = p.parse_invite_call(
        base + p.TYPEAHEAD + ".abc&variables=(type:COMPANY,keywords:an)", "GET"
    )
    unrelated = p.parse_invite_call(base + "voyagerSearchDashClusters.abc", "GET")
    assert search is not None and search.resource == p.TYPEAHEAD
    assert other is None
    assert unrelated is None


def test_candidates_keep_the_dialog_order_with_their_profile():
    rows = p.suggestion_candidates(_suggestions("ACoAAone1", "ACoAAtwo2"))
    assert rows == [
        {
            "profileId": "ACoAAone1",
            "name": "Example Person ACoAAone1",
            "headline": "Example headline",
            "publicIdentifier": "example-ACoAAone1",
        },
        {
            "profileId": "ACoAAtwo2",
            "name": "Example Person ACoAAtwo2",
            "headline": "Example headline",
            "publicIdentifier": "example-ACoAAtwo2",
        },
    ]
    assert p.suggestion_page(_suggestions("ACoAAone1", start=20)) == (20, 1)


def test_a_candidate_that_is_not_a_profile_refuses_the_page():
    payload = _suggestions("ACoAAone1")
    payload["data"]["elements"].append({"suggestedProfile": "urn:li:company:1"})
    assert p.suggestion_candidates(payload) is None


def test_search_results_read_the_name_and_profile_from_the_typeahead():
    payload = {
        "data": {
            "data": {
                "$type": "x",
                "searchDashReusableTypeaheadByType": {
                    "elements": [
                        {
                            "title": {"text": "Example Person"},
                            "subtitle": {"text": "Example headline"},
                            "target": {"*profile": _urn("ACoAAone1")},
                        }
                    ],
                },
            }
        },
        "included": [{"entityUrn": _urn("ACoAAone1")}],
    }
    assert p.typeahead_candidates(payload) == [
        {
            "profileId": "ACoAAone1",
            "name": "Example Person",
            "headline": "Example headline",
        }
    ]


def test_statuses_and_credits():
    statuses = {
        "data": {
            "elements": [
                {"suggestedProfile": _urn("ACoAAone1"), "ableToBeInvited": True},
                {"suggestedProfile": _urn("ACoAAtwo2"), "ableToBeInvited": False},
            ]
        },
        "included": [],
    }
    assert p.invitable_by_profile(statuses) == {"ACoAAone1": True, "ACoAAtwo2": False}
    credits = {
        "data": {
            "elements": [
                {
                    "remainingCredit": 7,
                    "initialCreditGrant": 100,
                    "remainingInvitationQuota": -1,
                    "nextCreditGrantOn": {"year": 2026, "month": 12, "day": 1},
                }
            ]
        },
        "included": [],
    }
    assert p.page_credits(credits) == {
        "remaining": 7,
        "total": 100,
        "nextGrantOn": "2026-12-01",
    }


def test_a_status_without_a_boolean_refuses_the_response():
    statuses = {
        "data": {"elements": [{"suggestedProfile": _urn("ACoAAone1")}]},
        "included": [],
    }
    assert p.invitable_by_profile(statuses) is None


def _element(profile_id: str, **extra) -> dict:
    return {
        "inviteeMember": _urn(profile_id),
        "genericInvitationType": "ORGANIZATION",
        "trackingId": "t",
        **extra,
    }


def test_a_page_invitation_batch_names_its_invitees():
    body = {"elements": [_element("ACoAAone1"), _element("ACoAAtwo2")]}
    assert p.invitation_batch(body) == ["ACoAAone1", "ACoAAtwo2"]


def test_a_batch_carrying_anything_else_is_not_a_page_invitation():
    assert p.invitation_batch({"elements": []}) is None
    assert (
        p.invitation_batch({"elements": [_element("ACoAAone1", message="hi")]}) is None
    )
    other_type = _element("ACoAAone1")
    other_type["genericInvitationType"] = "EVENT"
    assert p.invitation_batch({"elements": [other_type]}) is None
    email = {"inviteeHandle": "a@example.com", "genericInvitationType": "ORGANIZATION"}
    assert p.invitation_batch({"elements": [email]}) is None


def test_batch_statuses_follow_request_order_and_unknowns_stay_unknown():
    answer = {"data": {"elements": [{"status": 201}, {"status": 422}]}, "included": []}
    assert p.batch_statuses(answer, 2) == [201, 422]
    assert p.batch_statuses(answer, 3) == [None, None, None]
    assert p.batch_statuses({"unexpected": True}, 1) == [None]


def test_an_inviter_must_be_this_page_when_present():
    base = "https://www.linkedin.com/voyager/api/" + p.INVITATIONS
    assert p.inviter_allowed(base, COMPANY)
    assert p.inviter_allowed(
        f"{base}?inviter=urn%3Ali%3Afsd_company%3A{COMPANY}", COMPANY
    )
    assert not p.inviter_allowed(f"{base}?inviter=urn%3Ali%3Afsd_company%3A9", COMPANY)
    assert not p.inviter_allowed(
        f"{base}?inviter=urn%3Ali%3Afsd_company%3A1{COMPANY}", COMPANY
    )


def _encoded(kind: int, member: int) -> str:
    import base64

    raw = bytes([0, kind, 0, 0]) + member.to_bytes(4, "big") + b"\x01\x02\x03"
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_profile_and_lead_ids_decode_to_the_same_member():
    assert p.member_id_of(_encoded(0x2A, 123456789)) == "123456789"
    assert p.member_id_of(_encoded(0x2C, 123456789)) == "123456789"
    assert p.member_id_of("ACoAA") is None
    assert p.member_id_of("not-an-id-at-all") is None


@pytest.mark.parametrize(
    ("value", "kind", "key"),
    [
        (_encoded(0x2A, 42), "member", "42"),
        (f"urn:li:fsd_profile:{_encoded(0x2A, 42)}", "member", "42"),
        (f"urn:li:fs_miniProfile:{_encoded(0x2A, 42)}", "member", "42"),
        (_encoded(0x2C, 42), "member", "42"),
        (
            f"urn:li:fs_salesProfile:({_encoded(0x2C, 42)},NAME_SEARCH,x1)",
            "member",
            "42",
        ),
        (
            f"https://www.linkedin.com/sales/lead/{_encoded(0x2C, 42)},NAME_SEARCH,x1",
            "member",
            "42",
        ),
        ("urn:li:member:42", "member", "42"),
        (f"https://www.linkedin.com/in/{_encoded(0x2A, 42)}/", "member", "42"),
        ("https://www.linkedin.com/in/Some-Person/", "vanity", "some-person"),
        ("linkedin.com/in/some-person", "vanity", "some-person"),
        ("some-person", "vanity", "some-person"),
    ],
)
def test_every_form_the_other_tools_return_is_read(value, kind, key):
    ref = p.invitee_ref(value)
    assert ref is not None
    assert (ref.kind, ref.key) == (kind, key)
    assert ref.given == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "Some Person",
        "urn:li:company:42",
        "urn:li:member:abc",
        "https://example.com/in/some-person/",
        "https://www.linkedin.com/company/example/",
    ],
)
def test_anything_else_is_refused(value):
    assert p.invitee_ref(value) is None


def test_a_reference_matches_by_identifier_only():
    candidate: p.Candidate = {
        "profileId": _encoded(0x2A, 42),
        "name": "some-person",
        "publicIdentifier": "Some-Person",
    }
    assert p.ref_matches(p.InviteeRef("x", "member", "42"), candidate)
    assert not p.ref_matches(p.InviteeRef("x", "member", "43"), candidate)
    assert p.ref_matches(p.InviteeRef("x", "vanity", "some-person"), candidate)
    no_public: p.Candidate = {"profileId": _encoded(0x2A, 42), "name": "some-person"}
    assert not p.ref_matches(p.InviteeRef("x", "vanity", "some-person"), no_public)
