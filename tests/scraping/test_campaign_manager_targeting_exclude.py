"""Excluding a value: it joins the one exclusion block, never a new AND group.

LinkedIn keeps exclusions as one OR (``exclude.or``), shown as one block whose
own button opens it, also when it is still empty. Synthetic data only: the
scripted module and LinkedIn of the targeting tests stand in for the page.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from linkedin_mcp_server.error_diagnostics import build_issue_diagnostics
from linkedin_mcp_server.response_structure import current_log
from linkedin_mcp_server.scraping import campaign_manager_targeting as targeting
from linkedin_mcp_server.scraping import campaign_manager_write as write_module
from linkedin_mcp_server.scraping.campaign_manager_changes import plan_targeting

from .test_campaign_manager_targeting import (
    CAMPAIGN,
    EMPLOYERS,
    ORIGINAL,
    _criteria,
    _LinkedIn,
    _Module,
    _update,
)

SKILLS = "urn:li:adTargetingFacet:skills"
SKILL = {"urn": "urn:li:skill:5", "name": "Example skill", "facetUrn": SKILLS}
EMPLOYER = {"urn": "urn:li:organization:8", "name": "Example", "facetUrn": EMPLOYERS}
NO_EXCLUSIONS = [key for key in ORIGINAL if not key[0].startswith("exclude")]


def _exclude(module: _Module, entity: dict, query: str) -> list[dict[str, Any]]:
    module.rows[query] = [entity]
    return [{"urn": entity["urn"], "query": query, "exclude": True}]


async def test_a_new_facet_is_excluded_in_the_existing_exclusion_block(mock_page):
    linkedin = _LinkedIn(mock_page)
    module = _Module(linkedin)
    add = _exclude(module, SKILL, "skill")

    result = await _update(mock_page, linkedin, module, add=add, remove=[])

    assert result["status"] == "updated"
    assert "new:" not in " ".join(module.opened)
    assert set(module.opened) == {"excludedFacets"}
    assert module.clicks == [("row", SKILL["urn"])]
    assert ("exclude_facets", SKILLS, SKILL["urn"]) in linkedin.keys
    assert result["applied"] == {
        "add": [{"urn": SKILL["urn"], "facetUrn": SKILLS, "exclude": True}],
        "remove": [],
    }


async def test_a_value_of_an_excluded_facet_joins_that_block(mock_page):
    linkedin = _LinkedIn(mock_page)
    module = _Module(linkedin)
    add = _exclude(module, EMPLOYER, "employer")

    result = await _update(mock_page, linkedin, module, add=add, remove=[])

    assert result["status"] == "updated"
    assert set(module.opened) == {"excludedFacets"}
    excluded = [key for key in linkedin.keys if key[0] == "exclude_facets"]
    assert excluded == [
        ("exclude_facets", EMPLOYERS, "urn:li:organization:9"),
        ("exclude_facets", EMPLOYERS, EMPLOYER["urn"]),
    ]


async def test_a_first_exclusion_opens_the_exclusion_block_not_a_new_group(
    mock_page,
):
    linkedin = _LinkedIn(mock_page)
    linkedin.keys = list(NO_EXCLUSIONS)
    module = _Module(linkedin)
    module.add_button = False
    add = _exclude(module, SKILL, "skill")

    result = await _update(mock_page, linkedin, module, add=add, remove=[])

    assert result["status"] == "updated"
    assert set(module.opened) == {"excludedFacets"}
    assert linkedin.keys == [*NO_EXCLUSIONS, ("exclude_facets", SKILLS, SKILL["urn"])]


def test_an_exclusion_is_never_a_new_group():
    plan = plan_targeting(
        _criteria(NO_EXCLUSIONS),
        add=[(SKILL["urn"], SKILLS, True), ("urn:li:skill:6", SKILLS, False)],
        remove=[],
    )

    assert not isinstance(plan, tuple)
    assert targeting._new_sections(plan) == {"include_or:2"}


async def test_an_off_plan_exclusion_save_is_refused(mock_page):
    linkedin = _LinkedIn(mock_page)
    module = _Module(linkedin)
    add = _exclude(module, SKILL, "skill")
    module.save = lambda keys: {
        "patch": {
            "$set": {
                "targetingCriteria": _criteria(
                    keys + [("exclude_facets", SKILLS, "urn:li:skill:99")]
                )
            }
        }
    }

    result = await _update(mock_page, linkedin, module, add=add, remove=[])

    assert result["status"] == "change_blocked"
    assert linkedin.keys == ORIGINAL


_BLOCKS = {
    "blocks": 4,
    "open": [],
    "edit": {
        "excludedFacets": {"count": 1, "in_block": 0, "visible": 1, "disabled": 0}
    },
    "exclude_button": 0,
    "add_group_button": 1,
    "exclusion_blocks": 0,
}


async def test_a_missing_block_is_control_unavailable_with_its_structure_noted(
    mock_page,
):
    linkedin = _LinkedIn(mock_page)
    module = _Module(linkedin)
    add = _exclude(module, SKILL, "skill")
    missing = AsyncMock(
        side_effect=targeting._not_found("The targeting block was not found.")
    )

    async def evaluate(script: str, arg: Any = None) -> Any:
        if script == targeting._BLOCKS_SUMMARY_JS:
            return _BLOCKS
        if script == write_module._STRUCTURE_JS:
            return {"anchors": {}, "loading": False}
        raise AssertionError("unexpected page script")

    mock_page.evaluate = AsyncMock(side_effect=evaluate)

    with patch.object(module, "open_block", missing):
        result = await _update(mock_page, linkedin, module, add=add, remove=[])

    assert result["status"] == "control_unavailable"
    assert result["message"].endswith("Nothing was changed.")
    log = current_log()
    assert log is not None
    (page,) = log.pages
    assert page["targeting_blocks"] == _BLOCKS
    notes = build_issue_diagnostics(
        RuntimeError("control unavailable"), context="update_campaign_targeting"
    )
    with open(notes["issue_template_path"], encoding="utf-8") as handle:
        section = handle.read().split("## Page structure when a control")[1]
    assert '"targeting_blocks"' in section
    assert CAMPAIGN not in section


async def test_a_stop_that_is_not_a_missing_block_reads_no_structure(mock_page):
    linkedin = _LinkedIn(mock_page)
    module = _Module(linkedin)
    add = _exclude(module, SKILL, "skill")
    stuck = AsyncMock(
        side_effect=targeting._unavailable("The targeting block did not open.")
    )
    mock_page.evaluate = AsyncMock()

    with patch.object(module, "open_block", stuck):
        result = await _update(mock_page, linkedin, module, add=add, remove=[])

    assert result["status"] == "control_unavailable"
    mock_page.evaluate.assert_not_awaited()
