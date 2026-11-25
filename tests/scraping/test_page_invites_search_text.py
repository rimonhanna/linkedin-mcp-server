"""The text the dialog's name search is given for a person's name.

A name with a bracketed part, emoji or credentials after a comma found no one
in the dialog's search and timed out; the search is now given the name's own
words only. Every value is synthetic.
"""

from __future__ import annotations

import pytest

from linkedin_mcp_server.scraping import page_invites as pi
from linkedin_mcp_server.scraping.api_capture import CapturedResponse
from linkedin_mcp_server.scraping.page_invite_payload import parse_invite_call

from .test_page_invites import COMPANY, IDS, _actions, _statuses
from .test_page_invites import writes  # noqa: F401  (fixture)
from .test_page_invites_name_search import _invite, _SearchPage


@pytest.mark.parametrize(
    ("name", "typed"),
    [
        ("Alex Example", "Alex Example"),
        ("Alex Example (nee Sample)", "Alex Example"),
        ("Alex (Al) Example", "Alex Example"),
        ("Alex Example [she/her]", "Alex Example"),
        ("Alex Example (unclosed", "Alex Example"),
        ("Alex Example, MBBS, MSc.(Oxon)", "Alex Example"),
        ("Alex \U0001f680 Example ❤️", "Alex Example"),
        ("1️⃣ Alex Example", "Alex Example"),
        ("Alex Middle Other Example", "Alex Middle Other Example"),
        ("Seán O'Example-Sample", "Seán O'Example-Sample"),
        ("Person 0", "Person 0"),
        ("\U0001f525 ★", ""),
        ("Alex Example | Head of Examples", "Alex Example"),
        ("Alex Example - Founder", "Alex Example"),
        ("Alex Example-Sample - Founder", "Alex Example-Sample"),
        ("Alex Example \u2022 Engineer", "Alex Example"),
        ("Alex Example \u00b7 3rd", "Alex Example"),
        ("Dr. Alex Example", "Alex Example"),
        ("Prof Mrs Alex Example", "Alex Example"),
        ("Alex Example Jr.", "Alex Example"),
        ("Alex Example PhD MBA", "Alex Example"),
        ("Alex Example Ph.D.", "Alex Example"),
        ("Dr", "Dr"),
        ("Abd al-Rahman bin Example", "Abd al-Rahman bin Example"),
        (
            "\u0639\u0628\u062f \u0627\u0644\u0631\u062d\u0645\u0646 \u0628\u0646 \u062e\u0627\u0644\u062f",
            "\u0639\u0628\u062f \u0627\u0644\u0631\u062d\u0645\u0646 \u0628\u0646 \u062e\u0627\u0644\u062f",
        ),
        (
            "\u0930\u093e\u0939\u0941\u0932 \u0936\u0930\u094d\u092e\u093e",
            "\u0930\u093e\u0939\u0941\u0932 \u0936\u0930\u094d\u092e\u093e",
        ),
        ("\u674e\u660e", "\u674e\u660e"),
        ("\u674e\u660e\uff08Leo\uff09", "\u674e\u660e"),
        ("\uff21\uff4c\uff45\uff58 Example", "Alex Example"),
    ],
)
def test_the_search_text_keeps_the_name_words_only(name, typed):
    assert pi._search_text(name) == typed


@pytest.mark.parametrize(
    ("variables", "typed"),
    [
        ("(type:CONNECTIONS,keywords:Alex (Al),start:0)", "Alex (Al)"),
        ("(type:CONNECTIONS,keywords:Alex (Al))", "Alex (Al)"),
        ("(type:CONNECTIONS,keywords:Alex Example)", "Alex Example"),
    ],
)
def test_the_response_keywords_keep_a_closing_bracket_of_their_own(variables, typed):
    call = parse_invite_call(
        "https://www.linkedin.com/voyager/api/graphql"
        f"?queryId=voyagerSearchDashReusableTypeahead.abc&variables={variables}",
        "GET",
    )
    assert call is not None
    assert pi._typed(CapturedResponse(call, 200, None)) == typed


async def test_a_bracketed_name_is_searched_without_the_bracket(writes):  # noqa: F811
    page = _SearchPage({"Alex Example": [IDS[10]]}, rows=3)
    result = await _invite(
        page, [IDS[10]], ["Alex (Al) Example \U0001f680"], confirm=False
    )

    assert page.searches == ["Alex Example"], "the search box gets the search text"
    assert _statuses(result) == {IDS[10]: "would_invite"}


async def test_a_name_with_no_words_is_not_searched(writes):  # noqa: F811
    page = _SearchPage({}, rows=3)
    result = await _invite(page, [IDS[10]], ["\U0001f525"], confirm=False)

    assert page.searches == []
    assert _statuses(result) == {IDS[10]: "not_invitable"}


async def test_keywords_with_a_bracket_are_read_whole_from_the_response(writes):  # noqa: F811
    page = _SearchPage({"Alex (Al) Example": [IDS[10]]}, rows=3)
    actions, _ = _actions(page)
    result = await actions.list_follow_candidates(COMPANY, keywords="Alex (Al) Example")

    assert [c["profileId"] for c in result["candidates"]] == [IDS[10]], (
        "the response for the text typed must match it, bracket and all"
    )
