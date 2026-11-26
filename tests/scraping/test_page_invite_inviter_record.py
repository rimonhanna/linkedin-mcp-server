"""The inviter the page dialog really sends, measured on a live dialog.

The URL and body are a sanitised copy of the dialog's own batch create
(captured with the request aborted in the browser): the page id and the
invitee id are placeholders, the shape is LinkedIn's.
"""

from __future__ import annotations

import pytest

from linkedin_mcp_server.scraping import page_invite_payload as p

COMPANY = "100000701"
INVITEE = "ACoAAPlaceholder0000000000000000000000"
BASE = "https://www.linkedin.com/voyager/api/voyagerRelationshipsDashInvitations"
OBSERVED = f"{BASE}?inviter=(organizationUrn:urn%3Ali%3Afsd_company%3A{COMPANY})"
OBSERVED_BODY = {
    "elements": [
        {
            "inviteeMember": f"urn:li:fsd_profile:{INVITEE}",
            "genericInvitationType": "ORGANIZATION",
        }
    ]
}


def test_the_dialogs_organization_record_names_this_page():
    assert p.inviter_allowed(OBSERVED, COMPANY), (
        "the observed (organizationUrn:<page urn>) inviter must be admitted"
    )


def test_the_observed_body_is_read_as_one_invitee():
    assert p.invitation_batch(OBSERVED_BODY) == [INVITEE]


@pytest.mark.parametrize(
    "inviter",
    [
        # Another page.
        "(organizationUrn:urn%3Ali%3Afsd_company%3A9)",
        f"(organizationUrn:urn%3Ali%3Afsd_company%3A1{COMPANY})",
        # The member, or any profile, is never the page, even with its id.
        f"(organizationUrn:urn%3Ali%3Afsd_profile%3A{COMPANY})",
        f"(organizationUrn:urn%3Ali%3Amember%3A{COMPANY})",
        f"urn%3Ali%3Amember%3A{COMPANY}",
        f"urn%3Ali%3Afsd_profile%3A{COMPANY}",
        f"(organizationUrn:urn%3Ali%3Afsd_profile%3A{INVITEE})",
        # Another field, or more than the one field.
        f"(memberUrn:urn%3Ali%3Afsd_company%3A{COMPANY})",
        f"(organizationUrn:urn%3Ali%3Afsd_company%3A{COMPANY},memberUrn:x)",
        f"(organizationUrn:urn%3Ali%3Afsd_company%3A{COMPANY}",
    ],
)
def test_an_inviter_that_is_not_this_page_is_refused(inviter):
    assert not p.inviter_allowed(f"{BASE}?inviter={inviter}", COMPANY), inviter


def test_two_inviters_are_refused_even_when_both_name_the_page():
    query = f"inviter=(organizationUrn:urn%3Ali%3Afsd_company%3A{COMPANY})"
    assert not p.inviter_allowed(f"{BASE}?{query}&{query}", COMPANY)
