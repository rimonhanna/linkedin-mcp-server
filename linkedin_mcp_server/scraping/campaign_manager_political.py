"""The political-ad question Campaign Manager asks before saving an EU ad set.

EU political advertising rules make the advertiser declare whether an ad
set's ads are political. On the classic editor's Save and exit, an ad set
whose targeting includes an EU location always opens this dialog first, also
when the ad set already carries a declaration; confirming it sets the
campaign's ``politicalIntent`` (``POLITICAL`` or ``NOT_POLITICAL``) and saves.

The answer is a legal declaration the user makes on the advertiser's behalf,
so it is never defaulted here: a dialog met without the caller's
``political_ad`` is closed with Escape and nothing is sent.

Anchors, read from the app's bundle 2026-11-18 (no visible text is read): the
dialog's heading has the id ``political-ad-self-declaration-modal``, its one
checkbox the id ``political-ad-self-declaration``, and its footer holds Cancel
then Confirm after the checkbox. The same checkbox id also renders inline on
the details page, so it is only ever looked up inside the dialog. A checked
box declares the ads not political, an unchecked one political.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from linkedin_mcp_server.scraping.campaign_manager_changes import TargetingKey

POLITICAL_INTENT = "politicalIntent"

# The app's EU country group: its 27 member countries, the regions that
# expand to all of them and the subregions that expand to some.
_EU_COUNTRIES = frozenset(
    f"urn:li:geo:{geo}"
    for geo in (
        "103883259",  # Austria
        "100565514",  # Belgium
        "105333783",  # Bulgaria
        "104688944",  # Croatia
        "106774002",  # Cyprus
        "104508036",  # Czech Republic
        "104514075",  # Denmark
        "102974008",  # Estonia
        "100456013",  # Finland
        "105015875",  # France
        "101282230",  # Germany
        "104677530",  # Greece
        "100288700",  # Hungary
        "104738515",  # Ireland
        "103350119",  # Italy
        "104341318",  # Latvia
        "101464403",  # Lithuania
        "104042105",  # Luxembourg
        "100961908",  # Malta
        "102890719",  # Netherlands
        "105072130",  # Poland
        "100364837",  # Portugal
        "106670623",  # Romania
        "103119917",  # Slovakia
        "106137034",  # Slovenia
        "105646813",  # Spain
        "105117694",  # Sweden
    )
)
_EU_PARENTS = frozenset(
    f"urn:li:geo:{geo}"
    for geo in (
        "100506914",  # Europe
        "91000002",  # European Economic Area
        "91000000",  # European Union
        "91000007",  # EMEA
    )
)
_EU_SUBREGIONS = {
    "urn:li:geo:91000006": ("101282230", "103883259"),  # DACH
    "urn:li:geo:91000005": ("100565514", "102890719", "104042105"),  # Benelux
    "urn:li:geo:91000009": ("104514075", "100456013", "105117694"),  # Nordics
    "urn:li:geo:91000001": ("106774002",),  # Middle East
}

PoliticalQuestion = Literal["will", "may"]


def _eu_countries(urns: Iterable[str]) -> set[str]:
    countries: set[str] = set()
    for urn in urns:
        if urn in _EU_PARENTS:
            countries |= _EU_COUNTRIES
        elif urn in _EU_SUBREGIONS:
            countries |= {f"urn:li:geo:{geo}" for geo in _EU_SUBREGIONS[urn]}
        elif urn in _EU_COUNTRIES:
            countries.add(urn)
    return countries


def political_question(keys: Iterable[TargetingKey] | None) -> PoliticalQuestion:
    """``will`` when the targeting includes an EU location, else ``may``.

    Only countries and regions are known by URN; a city or other place in an
    EU country is recognised by the app from its parents, which the record
    does not carry, so any other targeting may still be asked.
    """
    included: list[str] = []
    excluded: list[str] = []
    for section, _facet, urn in keys or ():
        (included if section.startswith("include") else excluded).append(urn)
    return "will" if _eu_countries(included) - _eu_countries(excluded) else "may"


def declared_intent(political_ad: bool) -> str:
    """The ``politicalIntent`` value the caller's declaration stands for."""
    return "POLITICAL" if political_ad else "NOT_POLITICAL"


def question_sentence(question: PoliticalQuestion) -> str:
    """What a dry run without a declaration tells the caller about the dialog."""
    asks = "will" if question == "will" else "may"
    return (
        f" Saving {asks} make Campaign Manager ask whether this ad set's ads are "
        "political (EU political advertising rules). That answer is a legal "
        "declaration the user makes for the advertiser; this tool never chooses "
        "it. Pass political_ad true or false as the user declared it."
    )


DECLARATION_REQUIRED = (
    "Campaign Manager asked whether this ad set's ads are political (EU "
    "political advertising rules) before saving. That answer is a legal "
    "declaration the user makes for the advertiser, so the tool closed the "
    "question and nothing was sent. Ask the user, then call again with "
    "political_ad true (the ads are political) or false (they are not)."
)
UNANSWERED = (
    "Campaign Manager asked whether this ad set's ads are political, but the "
    "question did not show the controls this tool knows, so it was closed "
    "unanswered and nothing was sent."
)

# ``political`` null only reports whether the dialog is open; true or false
# sets the checkbox to that declaration (checked = not political) and
# confirms. The buttons are found by structure: the two after the checkbox
# are Cancel and Confirm, in that order.
POLITICAL_JS = """async ({ political }) => {
    /* linkedin-mcp:cm-editor */
    const dialogs = [...document.querySelectorAll('h2')]
        .filter((el) => el.id === 'political-ad-self-declaration-modal')
        .map((el) => el.closest('[role=dialog]'))
        .filter((el) => el && el.getClientRects().length > 0);
    if (dialogs.length === 0) return { state: 'absent' };
    if (dialogs.length !== 1) return { state: 'ambiguous' };
    if (political === null) return { state: 'open' };
    const dialog = dialogs[0];
    const boxes = [...dialog.querySelectorAll('input[type=checkbox]')]
        .filter((el) => el.id === 'political-ad-self-declaration');
    if (boxes.length !== 1 || boxes[0].disabled) return { state: 'layout' };
    const box = boxes[0];
    const after = [...dialog.querySelectorAll('button')].filter(
        (el) => box.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING
    );
    if (after.length !== 2) return { state: 'layout', buttons: after.length };
    const confirm = after[1];
    if (confirm.disabled || confirm.getClientRects().length === 0) {
        return { state: 'layout' };
    }
    const wanted = !political;
    if (box.checked !== wanted) box.click();
    await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
    if (box.checked !== wanted) return { state: 'unset' };
    confirm.click();
    return { state: 'answered' };
}"""


async def political_dialog(page: Any, political_ad: bool | None) -> str | None:
    """The dialog's state; with a declaration, answer it with exactly that.

    ``absent`` / ``open`` without a declaration; ``answered`` once confirmed;
    ``layout``, ``unset`` or ``ambiguous`` when it is not the dialog this
    tool knows, which the caller closes unanswered.
    """
    result = await page.evaluate(POLITICAL_JS, {"political": political_ad})
    return result.get("state") if isinstance(result, dict) else None
