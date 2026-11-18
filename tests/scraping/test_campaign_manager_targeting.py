"""Tests for the Campaign Manager targeting editor (synthetic data only).

The page's targeting module is replaced by a scripted one that keeps the
values a block shows and, like a draft, sends the app's own save through the
real write guard after every edit. LinkedIn's side keeps what the guard let
through and returns it on the next record read.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from linkedin_mcp_server.scraping import campaign_manager_targeting as targeting
from linkedin_mcp_server.scraping import campaign_manager_write as write_module
from linkedin_mcp_server.scraping.campaign_manager_targeting import (
    CampaignTargetingEditor,
    _block_attr,
    _emptied,
    _Row,
    _Stop,
)
from linkedin_mcp_server.scraping.campaign_manager_changes import plan_targeting
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

CM = "https://www.linkedin.com/campaign-manager-api/"
ACCOUNT = "100000001"
CAMPAIGN = "100000301"
APP = f"https://www.linkedin.com/campaignmanager/accounts/{ACCOUNT}"
DETAILS = f"{APP}/campaigns/{CAMPAIGN}/details"
LOC = "urn:li:adTargetingFacet:locations"
TITLES = "urn:li:adTargetingFacet:titles"
INDUSTRIES = "urn:li:adTargetingFacet:industries"
EMPLOYERS = "urn:li:adTargetingFacet:employers"
ORIGINAL = [
    ("include_locations", LOC, "urn:li:geo:101"),
    ("include_or:0", TITLES, "urn:li:title:1"),
    ("include_or:0", TITLES, "urn:li:title:2"),
    ("include_or:1", INDUSTRIES, "urn:li:industry:4"),
    ("exclude_facets", EMPLOYERS, "urn:li:organization:9"),
]
NEW_TITLE = {"urn": "urn:li:title:7", "name": "Example title", "facetUrn": TITLES}
DECOY = {"urn": "urn:li:title:8", "name": "Example title", "facetUrn": TITLES}
BLOCKS = {
    "includedLocations": 0,
    "includedOrs0": 1,
    "includedOrs1": 2,
    "includedOrs2": 3,
    "excludedFacets": 4,
}
SECTIONS = {
    0: "include_locations",
    1: "include_or:0",
    2: "include_or:1",
    3: "include_or:2",
    4: "exclude_facets",
}


def _criteria(keys: list[tuple[str, str, str]]) -> dict:
    def ors(section_keys: list[tuple[str, str, str]]) -> list[dict]:
        facets: dict[str, list[dict]] = {}
        for _section, facet, urn in section_keys:
            facets.setdefault(facet, []).append({"urn": urn, "name": "Example"})
        return [
            {"facet": {"urn": facet, "name": "Example"}, "segments": segments}
            for facet, segments in facets.items()
        ]

    sections = ["include_locations"] + sorted(
        {key[0] for key in keys if key[0].startswith("include_or:")},
        key=lambda section: int(section.split(":")[1]),
    )
    ands = [
        {"or": ors([key for key in keys if key[0] == section])}
        for section in sections
        if any(key[0] == section for key in keys)
    ]
    excluded = [key for key in keys if key[0].startswith("exclude")]
    return {"include": {"and": ands}, "exclude": {"or": ors(excluded)}}


def _record(keys: list[tuple[str, str, str]], status: str = "DRAFT") -> dict:
    return {
        "data": {
            "id": int(CAMPAIGN),
            "accountId": int(ACCOUNT),
            "name": "Example ad set",
            "status": status,
            "targetingCriteria": _criteria(keys),
        },
        "included": [],
    }


def _response(
    path: str,
    payload: Any = None,
    *,
    status: int = 200,
    method: str = "GET",
    body: Any = None,
    restli: str | None = None,
) -> SimpleNamespace:
    headers = {"x-restli-method": restli} if restli else {}
    request = SimpleNamespace(
        method=method,
        url=CM + path,
        headers={},
        post_data=json.dumps(body) if body is not None else None,
        all_headers=AsyncMock(return_value=headers),
    )
    return SimpleNamespace(
        url=CM + path,
        status=status,
        request=request,
        json=AsyncMock(return_value=payload),
        all_headers=AsyncMock(return_value={}),
    )


def _emit(page: Any, response: SimpleNamespace) -> None:
    for callback in list(page.listeners.get("response", [])):
        callback(response)


class _LinkedIn:
    """The ad set as LinkedIn keeps it, and the page views that read it."""

    def __init__(self, page: Any, status: str = "DRAFT") -> None:
        self.page = page
        self.status = status
        self.keys = list(ORIGINAL)
        self.save_status = 200
        self.navigated: list[str] = []

    async def navigate(self, url: str) -> None:
        self.navigated.append(url)
        self.page.url = url
        self.page.main_frame = SimpleNamespace(url=url)
        path = f"campaignManagerCampaigns/{CAMPAIGN}"
        _emit(self.page, _response(path, _record(self.keys, self.status)))

    async def send(self, body: dict) -> str:
        """The app's save, through the guard; LinkedIn keeps what passes."""
        path = f"campaignManagerCampaigns/{CAMPAIGN}"
        save = _response(path, {}, method="POST", body=body, restli="partial_update")
        outcome: list[str] = []
        route = SimpleNamespace(
            request=save.request,
            abort=AsyncMock(side_effect=lambda: outcome.append("abort")),
            fallback=AsyncMock(side_effect=lambda: outcome.append("fallback")),
        )
        (handler,) = [
            call.args[1]
            for call in self.page.route.await_args_list
            if call.args[0] == "**/campaign-manager-api/**"
        ]
        await handler(route)
        if outcome == ["fallback"]:
            save.status = self.save_status
            if self.save_status < 300:
                criteria = body["patch"]["$set"]["targetingCriteria"]
                self.keys = list(targeting.targeting_keys(criteria) or [])
            _emit(self.page, save)
        return outcome[0]

    @contextmanager
    def running(self) -> Iterator[None]:
        with (
            patch.object(
                PageNavigator, "_navigate_to_page", AsyncMock(side_effect=self.navigate)
            ),
            patch.object(ScrapingSession, "check_rate_limit", new_callable=AsyncMock),
            patch.object(ScrapingSession, "dismiss_modal", new_callable=AsyncMock),
            patch.object(write_module, "_READY_TIMEOUT", 0.2),
            patch.object(targeting, "_STEP_PAUSE", 0.0),
        ):
            yield


class _Module:
    """The targeting module: blocks show the page's values, rows the answers."""

    def __init__(self, linkedin: _LinkedIn) -> None:
        self.linkedin = linkedin
        self.keys = list(linkedin.keys)
        self.rows: dict[str, list[dict]] = {"engineer": [DECOY, NEW_TITLE]}
        self.disabled: dict[str, list[bool]] = {}
        self.opened: list[str] = []
        self.clicks: list[tuple[str, Any]] = []
        self.save: Callable[[list], dict] = lambda keys: {
            "patch": {"$set": {"targetingCriteria": _criteria(keys)}}
        }
        self.on_click: Callable[[], None] = lambda: None
        self.typed: list[str] = []
        self.pill_offset = 0
        self.autosave = True
        self.add_button = True

    def __call__(self, page: Any, capture: Any) -> _Module:
        self.page = page
        return self

    def _section(self, index: int) -> str:
        return SECTIONS[index]

    async def open_block(self, attr: str) -> int:
        self.opened.append(attr)
        return BLOCKS[attr]

    async def can_add_block(self) -> bool:
        return self.add_button

    async def open_new_block(self, ors: int) -> int:
        self.opened.append(f"new:{ors}")
        if not self.add_button:
            raise targeting._unavailable("no new group")
        return BLOCKS[f"includedOrs{ors}"]

    async def pills(self, index: int) -> int:
        return sum(1 for key in self.keys if key[0] == self._section(index))

    async def expect_pills(self, index: int, count: int) -> None:
        if await self.pills(index) + self.pill_offset != count:
            raise targeting._unavailable("count")

    async def remove_pill(self, index: int, expected: int, pill: int) -> None:
        section = [key for key in self.keys if key[0] == self._section(index)]
        self.keys.remove(section[pill])
        self.clicks.append(("pill", section[pill][2]))
        self.on_click()
        if self.autosave:
            await self.linkedin.send(self.save(self.keys))

    async def clear_search(self, location: bool) -> None:
        return None

    async def search(self, query: str, *, location: bool) -> list[_Row]:
        self.typed.append(query)
        entities: list[Any] = [
            {**entity, "targetable": True} for entity in self.rows.get(query, [])
        ]
        disabled = self.disabled.get(query, [False] * len(entities))
        return [
            _Row(entity, flag, False)
            for entity, flag in zip(entities, disabled, strict=True)
        ]

    async def select_row(self, row: int, expected: int) -> None:
        entity = self.rows[self.typed[-1]][row]
        self.clicks.append(("row", entity["urn"]))
        section = SECTIONS[BLOCKS[self.opened[-1].replace("new:", "includedOrs")]]
        self.keys.append((section, entity["facetUrn"], entity["urn"]))
        self.on_click()
        if self.autosave:
            await self.linkedin.send(self.save(self.keys))


def _editor(page: Any) -> CampaignTargetingEditor:
    session = ScrapingSession(page)
    return CampaignTargetingEditor(session, PageNavigator(session))


async def _update(page: Any, linkedin: _LinkedIn, module: _Module, **kwargs: Any):
    kwargs.setdefault("add", [{"urn": NEW_TITLE["urn"], "query": "engineer"}])
    kwargs.setdefault("remove", ["urn:li:title:1"])
    kwargs.setdefault("confirm_send", True)
    with linkedin.running(), patch.object(targeting, "_TargetingPage", module):
        return await _editor(page).update_campaign_targeting(
            ACCOUNT, CAMPAIGN, **kwargs
        )


# --- pure helpers -------------------------------------------------------------


def test_block_attr_names_each_ui_block():
    assert _block_attr("include_locations") == "includedLocations"
    assert _block_attr("exclude_locations") == "excludedLocations"
    assert _block_attr("exclude_facets") == "excludedFacets"
    assert _block_attr("include_or:2") == "includedOrs2"


def test_block_attr_counts_past_or_blocks_already_emptied():
    assert _block_attr("include_or:2", [0]) == "includedOrs1"
    assert _block_attr("include_or:2", [3]) == "includedOrs2"


def test_language_has_no_block():
    with pytest.raises(ValueError):
        _block_attr("include_language")


def test_emptied_lists_or_blocks_the_plan_removes_entirely():
    plan = plan_targeting(
        _criteria(ORIGINAL), add=[], remove=["urn:li:industry:4", "urn:li:title:1"]
    )
    assert not isinstance(plan, tuple)
    assert _emptied(plan) == {1}


# --- search_targeting_entities ------------------------------------------------


async def test_search_reports_each_row_with_whether_the_block_allows_it(mock_page):
    linkedin = _LinkedIn(mock_page)
    module = _Module(linkedin)
    module.disabled["engineer"] = [True, False]

    with linkedin.running(), patch.object(targeting, "_TargetingPage", module):
        result = await _editor(mock_page).search_targeting_entities(
            ACCOUNT, CAMPAIGN, "engineer"
        )

    assert module.opened == ["includedOrs0"]
    assert [(e["urn"], e["addable"]) for e in result["targeting_entities"]] == [
        (DECOY["urn"], False),
        (NEW_TITLE["urn"], True),
    ]
    assert result["url"] == DETAILS
    assert "targeting_entities" in result["sections"]


async def test_search_on_a_block_that_cannot_open_raises(mock_page):
    linkedin = _LinkedIn(mock_page)
    module = _Module(linkedin)
    refused = AsyncMock(side_effect=_Stop("not_editable", "Not editable."))

    with (
        linkedin.running(),
        patch.object(targeting, "_TargetingPage", module),
        patch.object(module, "open_block", refused),
        pytest.raises(targeting.LinkedInScraperException, match="Not editable"),
    ):
        await _editor(mock_page).search_targeting_entities(
            ACCOUNT, CAMPAIGN, "engineer"
        )


# --- update_campaign_targeting: read phase ----------------------------------------


async def test_dry_run_resolves_everything_and_clicks_nothing(mock_page):
    linkedin = _LinkedIn(mock_page)
    module = _Module(linkedin)

    result = await _update(mock_page, linkedin, module, confirm_send=False)

    assert result["status"] == "confirmation_required"
    assert result["retry_safe"] is True
    assert module.clicks == []
    assert result["planned"] == {
        "remove": [
            {
                "urn": "urn:li:title:1",
                "facetUrn": TITLES,
                "exclude": False,
                "name": "Example",
            }
        ],
        "add": [
            {
                "urn": NEW_TITLE["urn"],
                "facetUrn": TITLES,
                "exclude": False,
                "name": "Example title",
            }
        ],
    }
    assert linkedin.keys == ORIGINAL


async def test_an_add_the_search_does_not_return_changes_nothing(mock_page):
    linkedin = _LinkedIn(mock_page)
    module = _Module(linkedin)
    module.rows["engineer"] = [DECOY]

    result = await _update(mock_page, linkedin, module)

    assert result["status"] == "entity_not_found"
    assert result["retry_safe"] is True
    assert module.clicks == []
    assert linkedin.keys == ORIGINAL


async def test_a_row_the_block_disables_is_not_addable(mock_page):
    linkedin = _LinkedIn(mock_page)
    module = _Module(linkedin)
    module.disabled["engineer"] = [False, True]

    result = await _update(mock_page, linkedin, module)

    assert result["status"] == "entity_not_addable"
    assert module.clicks == []


async def test_a_block_showing_other_pills_than_the_record_stops_first(mock_page):
    linkedin = _LinkedIn(mock_page)
    module = _Module(linkedin)
    module.pill_offset = 1

    result = await _update(mock_page, linkedin, module)

    assert result["status"] == "control_unavailable"
    assert result["retry_safe"] is True
    assert module.clicks == []


async def test_a_completed_ad_set_is_not_editable_and_opens_nothing(mock_page):
    linkedin = _LinkedIn(mock_page, status="COMPLETED")
    module = _Module(linkedin)

    result = await _update(mock_page, linkedin, module)

    assert result["status"] == "not_editable"
    assert module.opened == []


async def test_an_add_whose_answer_names_another_block_is_checked_there(mock_page):
    linkedin = _LinkedIn(mock_page)
    module = _Module(linkedin)
    industry = {"urn": "urn:li:industry:5", "name": "Example", "facetUrn": INDUSTRIES}
    module.rows["example industry"] = [industry]

    result = await _update(
        mock_page,
        linkedin,
        module,
        add=[{"urn": industry["urn"], "query": "example industry"}],
        remove=[],
        confirm_send=False,
    )

    assert result["status"] == "confirmation_required"
    # Probed in the first OR block, then confirmed in the one holding industries.
    assert module.opened == ["includedOrs0", "includedOrs1"]
    assert module.typed == ["example industry", "example industry"]


# --- update_campaign_targeting: apply and verify -------------------------------


async def test_draft_change_is_saved_step_by_step_and_verified(mock_page):
    linkedin = _LinkedIn(mock_page)
    module = _Module(linkedin)

    result = await _update(mock_page, linkedin, module)

    assert result["status"] == "updated"
    assert result["retry_safe"] is False
    assert module.clicks == [("pill", "urn:li:title:1"), ("row", NEW_TITLE["urn"])]
    assert result["applied"] == {
        "add": [{"urn": NEW_TITLE["urn"], "facetUrn": TITLES, "exclude": False}],
        "remove": [{"urn": "urn:li:title:1", "facetUrn": TITLES, "exclude": False}],
    }
    assert "refused" not in result


async def test_an_off_plan_autosave_is_refused_and_nothing_is_saved(mock_page):
    linkedin = _LinkedIn(mock_page)
    module = _Module(linkedin)
    module.save = lambda keys: {
        "patch": {
            "$set": {
                "targetingCriteria": _criteria(
                    keys + [("include_or:0", TITLES, "urn:li:title:99")]
                )
            }
        }
    }

    result = await _update(mock_page, linkedin, module)

    assert result["status"] == "change_blocked"
    assert result["retry_safe"] is True
    assert linkedin.keys == ORIGINAL
    assert module.clicks == [("pill", "urn:li:title:1")]
    assert result["refused"] == [
        {
            "method": "POST",
            "resource": "campaignManagerCampaigns",
            "reason": "targetingCriteria is not the planned value",
        }
    ]


async def test_the_deadline_stops_after_a_saved_step_as_partially_applied(mock_page):
    linkedin = _LinkedIn(mock_page)
    module = _Module(linkedin)
    clock = {"now": 0.0}

    def advance() -> None:
        clock["now"] = 40.0

    module.on_click = advance

    with patch.object(
        targeting, "time", SimpleNamespace(monotonic=lambda: clock["now"])
    ):
        result = await _update(mock_page, linkedin, module, deadline=50.0)

    assert result["status"] == "partially_applied"
    assert result["retry_safe"] is False
    assert module.clicks == [("pill", "urn:li:title:1")]
    assert result["applied"] == {
        "add": [],
        "remove": [{"urn": "urn:li:title:1", "facetUrn": TITLES, "exclude": False}],
    }


async def test_a_refused_save_is_save_failed_and_retry_safe(mock_page):
    linkedin = _LinkedIn(mock_page)
    linkedin.save_status = 400
    module = _Module(linkedin)

    result = await _update(mock_page, linkedin, module, add=[])

    assert result["status"] == "save_failed"
    assert result["retry_safe"] is True
    assert linkedin.keys == ORIGINAL


async def test_a_launched_ad_set_saves_once_with_save_and_exit(mock_page):
    linkedin = _LinkedIn(mock_page, status="PAUSED")
    module = _Module(linkedin)
    # A launched ad set does not save itself after each edit.
    module.autosave = False
    sent: list[str] = []

    async def save_and_exit(_flow: Any, page: Any, *, draft: bool) -> bool:
        assert draft is False
        sent.append(
            await linkedin.send(
                {"patch": {"$set": {"targetingCriteria": _criteria(module.keys)}}}
            )
        )
        return True

    with patch.object(write_module.CampaignWriteFlow, "save_and_exit", save_and_exit):
        result = await _update(mock_page, linkedin, module)

    assert sent == ["fallback"]
    assert result["status"] == "updated"
    assert result["retry_safe"] is False


SKILLS = "urn:li:adTargetingFacet:skills"
SKILL = {"urn": "urn:li:skill:5", "name": "Example skill", "facetUrn": SKILLS}
SKILL_TWO = {"urn": "urn:li:skill:6", "name": "Example skill", "facetUrn": SKILLS}


async def test_a_new_facet_is_added_in_a_new_group_never_an_existing_one(mock_page):
    linkedin = _LinkedIn(mock_page)
    module = _Module(linkedin)
    module.rows["skill"] = [SKILL, SKILL_TWO]

    result = await _update(
        mock_page,
        linkedin,
        module,
        add=[
            {"urn": SKILL["urn"], "query": "skill"},
            {"urn": NEW_TITLE["urn"], "query": "engineer"},
            {"urn": SKILL_TWO["urn"], "query": "skill"},
        ],
        remove=[],
    )

    assert result["status"] == "updated"
    # One new group for both skills, made once; the title joins its own block.
    assert module.opened.count("new:2") == 1
    assert ("include_or:2", SKILLS, SKILL["urn"]) in linkedin.keys
    assert ("include_or:2", SKILLS, SKILL_TWO["urn"]) in linkedin.keys
    assert ("include_or:0", TITLES, NEW_TITLE["urn"]) in linkedin.keys
    assert not any(
        key[1] == SKILLS and key[0] != "include_or:2" for key in linkedin.keys
    )


async def test_a_page_without_the_new_group_button_changes_nothing(mock_page):
    linkedin = _LinkedIn(mock_page)
    module = _Module(linkedin)
    module.rows["skill"] = [SKILL]
    module.add_button = False

    result = await _update(
        mock_page, linkedin, module, add=[{"urn": SKILL["urn"], "query": "skill"}]
    )

    assert result["status"] == "control_unavailable"
    assert result["retry_safe"] is True
    assert module.clicks == []
    assert linkedin.keys == ORIGINAL


async def test_an_app_save_that_ors_a_new_facet_into_a_block_is_refused(mock_page):
    linkedin = _LinkedIn(mock_page)
    module = _Module(linkedin)
    module.rows["skill"] = [SKILL]

    def widened(keys: list) -> dict:
        moved = [
            ("include_or:0", key[1], key[2]) if key[1] == SKILLS else key
            for key in keys
        ]
        return {"patch": {"$set": {"targetingCriteria": _criteria(moved)}}}

    module.save = widened

    result = await _update(
        mock_page,
        linkedin,
        module,
        add=[{"urn": SKILL["urn"], "query": "skill"}],
        remove=[],
    )

    assert result["status"] == "change_blocked"
    assert result["retry_safe"] is True
    assert linkedin.keys == ORIGINAL
