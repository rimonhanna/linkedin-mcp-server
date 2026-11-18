"""Tests for Campaign Manager change planning and admission (synthetic data only)."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from linkedin_mcp_server.scraping.campaign_manager_changes import (
    CampaignEditPlan,
    TargetingPlan,
    admit_campaign_create,
    admit_campaign_patch,
    admit_creative_create,
    admit_list_upload,
    admit_saved_audience,
    admit_status_patch,
    format_budget,
    ms_to_date,
    planned_end_ms,
    parse_budget,
    pill_index,
    plan_campaign_edit,
    plan_targeting,
    refuse_invalid_audience_name,
    refuse_invalid_campaign_edit,
    refuse_invalid_draft,
    refuse_invalid_targeting_change,
    saved_audience_omitted,
    targeting_keys,
)
from linkedin_mcp_server.scraping.campaign_manager_payload import CmCall

CAMPAIGN = "100000002"
ACCOUNT = "100000001"
GROUP = "100000003"
TODAY = date(2026, 3, 1)
LOC = "urn:li:adTargetingFacet:locations"
LANG = "urn:li:adTargetingFacet:interfaceLocales"
TITLES = "urn:li:adTargetingFacet:titles"
INDUSTRIES = "urn:li:adTargetingFacet:industries"
EMPLOYERS = "urn:li:adTargetingFacet:employers"


# Records keep ends at 23:45 UTC of the shown day.
END_TIME = (23 * 60 + 45) * 60_000


def _ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)


def _facet(urn: str, *segments: str) -> dict:
    return {
        "facet": {"urn": urn, "name": "Example facet", "$type": "x"},
        "segments": [{"urn": s, "name": "Example value"} for s in segments],
    }


def _criteria() -> dict:
    return {
        "include": {
            "and": [
                {"or": [_facet(LOC, "urn:li:geo:101", "urn:li:geo:102")]},
                {"or": [_facet(LANG, "urn:li:locale:en_US")]},
                {"or": [_facet(TITLES, "urn:li:title:1", "urn:li:title:2")]},
                {
                    "or": [
                        _facet(INDUSTRIES, "urn:li:industry:4"),
                        _facet(TITLES, "urn:li:title:3"),
                    ]
                },
            ]
        },
        "exclude": {
            "or": [
                _facet(EMPLOYERS, "urn:li:organization:9"),
                _facet(LOC, "urn:li:geo:103"),
            ]
        },
    }


def _record(**overrides: Any) -> dict:
    record = {
        "id": int(CAMPAIGN),
        "accountId": int(ACCOUNT),
        "status": "DRAFT",
        "dailyBudget": {"amount": "10", "currencyCode": "EUR", "$type": "m"},
        "runSchedule": {
            "start": _ms(date(2026, 3, 10)),
            "end": _ms(date(2026, 3, 20)) + END_TIME,
        },
        "targetingCriteria": _criteria(),
        "name": "Example ad set",
        "costType": "CPM",
    }
    record.update(overrides)
    return record


@dataclass(frozen=True)
class Req:
    call: CmCall
    url: str
    method: str
    restli_method: str | None
    body: Any


def _req(
    body: Any,
    *,
    resource: str = "campaignManagerCampaigns",
    entity_id: str | None = CAMPAIGN,
    restli: str | None = "partial_update",
    method: str = "POST",
    path: str | None = None,
    params: dict[str, str] | None = None,
) -> Req:
    if path is None:
        path = resource if entity_id is None else f"{resource}/{entity_id}"
    url = f"https://www.linkedin.com/campaign-manager-api/{path}"
    if params:
        url += "?" + "&".join(f"{key}={value}" for key, value in params.items())
    call = CmCall(resource, entity_id, None, params or {})
    return Req(call, url, method, restli, body)


def _patch(**patch: Any) -> Req:
    return _req({"patch": patch})


def _status(result: object) -> str:
    assert isinstance(result, tuple), result
    status, _message = result
    assert isinstance(status, str)
    return status


def _reason(value: str | None) -> str:
    assert value is not None
    return value


# Budgets


@pytest.mark.parametrize("value", ["12,5", "1,000", "0", "-1", "12.345", "", "1e3"])
def test_parse_budget_refuses_non_plain_amounts(value):
    with pytest.raises(ValueError, match="plain number with a dot"):
        parse_budget(value)


def test_parse_and_format_budget():
    assert parse_budget("12.5") == Decimal("12.5")
    assert format_budget(parse_budget("12.5")) == "12.5"
    assert format_budget(Decimal("12.50")) == "12.5"
    assert format_budget(Decimal("10")) == "10"
    assert format_budget(Decimal("1000000")) == "1000000"


# Dates


def test_ms_to_date_is_the_utc_date():
    midnight = _ms(date(2026, 3, 10))
    assert ms_to_date(midnight) == date(2026, 3, 10)
    assert ms_to_date(midnight + END_TIME) == date(2026, 3, 10)
    # A creation-time start late in the UTC day stays on that UTC day.
    assert ms_to_date(midnight + 86_399_999) == date(2026, 3, 10)


def test_planned_end_keeps_the_record_end_time():
    assert (
        planned_end_ms(_record(), date(2026, 4, 2)) == _ms(date(2026, 4, 2)) + END_TIME
    )
    continuous = _record(runSchedule={"start": _ms(date(2026, 3, 10))})
    assert planned_end_ms(continuous, date(2026, 4, 2)) == _ms(date(2026, 4, 2))


# Campaign edit planning


def _plan(record=None, **kwargs: Any):
    values = {"daily": None, "lifetime": None, "start": None, "end": None}
    values.update(kwargs)
    return plan_campaign_edit(record or _record(), today=TODAY, **values)


def test_plan_drops_unchanged_fields():
    plan = _plan(daily=Decimal("10"), end=date(2026, 3, 25))
    assert plan == CampaignEditPlan(None, None, None, date(2026, 3, 25), ("end",))
    assert _plan(daily=Decimal("10.00"), start=date(2026, 3, 10))[0] == "no_change"


def test_plan_order_start_first_when_new_start_before_old_end():
    plan = _plan(daily=Decimal("12.5"), start=date(2026, 3, 5), end=date(2026, 3, 8))
    assert isinstance(plan, CampaignEditPlan)
    assert plan.order == ("daily", "start", "end")


def test_plan_order_end_first_when_new_start_after_old_end():
    plan = _plan(start=date(2026, 4, 1), end=date(2026, 4, 9))
    assert isinstance(plan, CampaignEditPlan)
    assert plan.order == ("end", "start")
    # Equal to the old end: start first would make start == end.
    assert _plan(start=date(2026, 3, 20), end=date(2026, 3, 30)).order == (
        "end",
        "start",
    )


def test_plan_schedule_locked_for_started_non_draft():
    record = _record(
        status="ACTIVE",
        runSchedule={"start": _ms(date(2026, 2, 1)), "end": _ms(date(2026, 3, 20))},
    )
    assert _plan(record, start=date(2026, 3, 5))[0] == "schedule_locked"
    assert isinstance(_plan(record, end=date(2026, 3, 25)), CampaignEditPlan)


def test_plan_refusals():
    assert _plan(_record(status="ARCHIVED"), daily=Decimal("5"))[0] == "not_editable"
    assert _plan(lifetime=Decimal("500"))[0] == "budget_type_mismatch"
    no_budget = _record()
    del no_budget["dailyBudget"]
    assert _plan(no_budget, daily=Decimal("5"))[0] == "budget_type_mismatch"
    continuous = _record(runSchedule={"start": _ms(date(2026, 3, 10))})
    assert _plan(continuous, end=date(2026, 3, 25))[0] == "invalid_change"
    assert _plan(start=date(2026, 2, 1))[0] == "invalid_change"
    assert _plan(start=date(2026, 3, 25))[0] == "invalid_change"


def test_plan_reads_creation_time_start_and_late_end_as_utc_dates():
    record = _record(
        status="PAUSED",
        runSchedule={
            "start": _ms(date(2026, 3, 10)) + 15 * 3_600_000,
            "end": _ms(date(2026, 3, 20)) + END_TIME,
        },
    )
    plan = _plan(record, start=date(2026, 3, 10), end=date(2026, 3, 22))
    assert plan == CampaignEditPlan(None, None, None, date(2026, 3, 22), ("end",))
    assert _plan(record, end=date(2026, 3, 20))[0] == "no_change"


def test_plan_refuses_completed_lifetime_budget():
    completed = _record(
        status="COMPLETED",
        totalBudget={"amount": "500", "currencyCode": "EUR"},
    )
    del completed["dailyBudget"]
    assert _plan(completed, lifetime=Decimal("600"))[0] == "not_editable"
    paused = {**completed, "status": "PAUSED"}
    assert _plan(paused, lifetime=Decimal("600")) == CampaignEditPlan(
        None, Decimal("600"), None, None, ("lifetime",)
    )


def test_refuse_invalid_campaign_edit():
    kwargs = {"lifetime_budget": None, "start_date": None, "end_date": None}
    assert (
        refuse_invalid_campaign_edit(daily_budget="12.5", today=TODAY, **kwargs) is None
    )
    assert "plain number" in _reason(
        refuse_invalid_campaign_edit(daily_budget="12,5", today=TODAY, **kwargs)
    )
    assert refuse_invalid_campaign_edit(daily_budget=None, today=TODAY, **kwargs)
    assert refuse_invalid_campaign_edit(
        daily_budget=None,
        lifetime_budget=None,
        start_date=date(2026, 3, 9),
        end_date=date(2026, 3, 9),
        today=TODAY,
    )


# Targeting keys


def test_targeting_keys_map_ui_blocks():
    keys = targeting_keys(_criteria())
    assert keys is not None
    assert keys == [
        ("include_locations", LOC, "urn:li:geo:101"),
        ("include_locations", LOC, "urn:li:geo:102"),
        ("include_language", LANG, "urn:li:locale:en_US"),
        ("include_or:0", TITLES, "urn:li:title:1"),
        ("include_or:0", TITLES, "urn:li:title:2"),
        ("include_or:1", INDUSTRIES, "urn:li:industry:4"),
        ("include_or:1", TITLES, "urn:li:title:3"),
        ("exclude_facets", EMPLOYERS, "urn:li:organization:9"),
        ("exclude_locations", LOC, "urn:li:geo:103"),
    ]
    assert pill_index(keys, "include_or:1", "urn:li:title:3") == 1
    assert pill_index(keys, "include_locations", "urn:li:geo:102") == 1
    with pytest.raises(ValueError):
        pill_index(keys, "include_or:0", "urn:li:title:3")


def test_targeting_keys_accept_undecorated_shape():
    undecorated = {
        "include": {
            "and": [
                {"or": {LOC: ["urn:li:geo:101", "urn:li:geo:102"]}},
                {"or": {LANG: ["urn:li:locale:en_US"]}},
                {"or": {TITLES: ["urn:li:title:1", "urn:li:title:2"]}},
                {
                    "or": [
                        {"facet": INDUSTRIES, "segments": ["urn:li:industry:4"]},
                        {"facet": TITLES, "segments": ["urn:li:title:3"]},
                    ]
                },
            ]
        },
        "exclude": {
            "or": {EMPLOYERS: ["urn:li:organization:9"], LOC: ["urn:li:geo:103"]}
        },
    }
    assert targeting_keys(undecorated) == targeting_keys(_criteria())


@pytest.mark.parametrize(
    "criteria",
    [
        None,
        [],
        {"include": []},
        {"include": {"and": [{"or": [{"facet": {"urn": TITLES}}]}]}},
        {"include": {"and": [{"or": [_facet(LOC, "urn:li:geo:1"), _facet(TITLES)]}]}},
    ],
)
def test_targeting_keys_wrong_shape(criteria):
    assert targeting_keys(criteria) is None


# Targeting plans


def test_plan_targeting_blocks_and_order():
    plan = plan_targeting(
        _criteria(),
        add=[
            ("urn:li:title:7", TITLES, False),
            ("urn:li:industry:8", INDUSTRIES, False),
            ("urn:li:skill:5", "urn:li:adTargetingFacet:skills", False),
            ("urn:li:geo:104", LOC, True),
            ("urn:li:organization:10", EMPLOYERS, True),
        ],
        remove=["urn:li:title:1", "urn:li:title:2", "urn:li:geo:101"],
    )
    assert isinstance(plan, TargetingPlan)
    assert plan.adds == (
        ("include_or:0", TITLES, "urn:li:title:7"),
        ("include_or:1", INDUSTRIES, "urn:li:industry:8"),
        # No block holds skills: a new AND block, never an existing OR.
        ("include_or:2", "urn:li:adTargetingFacet:skills", "urn:li:skill:5"),
        ("exclude_locations", LOC, "urn:li:geo:104"),
        ("exclude_facets", EMPLOYERS, "urn:li:organization:10"),
    )
    # Per block, the last pill goes first so earlier indices stay put.
    assert plan.removes == (
        ("include_locations", LOC, "urn:li:geo:101"),
        ("include_or:0", TITLES, "urn:li:title:2"),
        ("include_or:0", TITLES, "urn:li:title:1"),
    )
    assert ("include_or:0", TITLES, "urn:li:title:7") in plan.target
    assert ("include_or:0", TITLES, "urn:li:title:1") not in plan.target


SKILLS = "urn:li:adTargetingFacet:skills"
SENIORITIES = "urn:li:adTargetingFacet:seniorities"


def test_plan_targeting_new_facet_never_joins_an_existing_block():
    plan = plan_targeting(
        _criteria(), add=[("urn:li:skill:5", SKILLS, False)], remove=[]
    )
    assert isinstance(plan, TargetingPlan)
    assert plan.adds == (("include_or:2", SKILLS, "urn:li:skill:5"),)
    existing = {
        key for key in plan.target if key[0] in ("include_or:0", "include_or:1")
    }
    assert existing == {
        key
        for key in targeting_keys(_criteria()) or []
        if key[0].startswith("include_or")
    }


def test_plan_targeting_values_of_one_new_facet_share_one_new_block():
    plan = plan_targeting(
        _criteria(),
        add=[
            ("urn:li:skill:5", SKILLS, False),
            ("urn:li:seniority:3", SENIORITIES, False),
            ("urn:li:skill:6", SKILLS, False),
            ("urn:li:title:7", TITLES, False),
        ],
        remove=[],
    )
    assert isinstance(plan, TargetingPlan)
    assert plan.adds == (
        ("include_or:2", SKILLS, "urn:li:skill:5"),
        ("include_or:3", SENIORITIES, "urn:li:seniority:3"),
        ("include_or:2", SKILLS, "urn:li:skill:6"),
        ("include_or:0", TITLES, "urn:li:title:7"),
    )


def test_plan_targeting_first_or_block_when_none_exists():
    criteria = {
        "include": {"and": [{"or": [_facet(LOC, "urn:li:geo:101")]}]},
        "exclude": {"or": []},
    }
    plan = plan_targeting(criteria, add=[("urn:li:skill:5", SKILLS, False)], remove=[])
    assert isinstance(plan, TargetingPlan)
    assert plan.adds == (("include_or:0", SKILLS, "urn:li:skill:5"),)


def test_patch_admits_a_new_and_block_and_nothing_wider():
    record = _record()
    plan = plan_targeting(
        record["targetingCriteria"],
        add=[("urn:li:skill:5", SKILLS, False), ("urn:li:skill:6", SKILLS, False)],
        remove=[],
    )
    assert isinstance(plan, TargetingPlan)
    allowed = {"targetingCriteria": plan.target}
    path = ("targetingCriteria",)

    def sent(criteria: dict) -> Req:
        return _patch(**{"$set": {"targetingCriteria": criteria}})

    def appended(*skills: str) -> dict:
        criteria = _criteria()
        criteria["include"]["and"].append({"or": [_facet(SKILLS, *skills)]})
        return criteria

    assert (
        _admit(
            sent(appended("urn:li:skill:5", "urn:li:skill:6")), record, allowed=allowed
        )
        is None
    )
    # One value saved before the other: still on the path.
    assert (
        _admit(
            sent(appended("urn:li:skill:5")), record, allowed=allowed, path_fields=path
        )
        is None
    )
    # The same values ORed into an existing block widen the audience.
    widened = _criteria()
    widened["include"]["and"][2]["or"].append(
        _facet(SKILLS, "urn:li:skill:5", "urn:li:skill:6")
    )
    assert _admit(sent(widened), record, allowed=allowed, path_fields=path)
    # Two new blocks where one was planned.
    split = _criteria()
    split["include"]["and"] += [
        {"or": [_facet(SKILLS, "urn:li:skill:5")]},
        {"or": [_facet(SKILLS, "urn:li:skill:6")]},
    ]
    assert _admit(sent(split), record, allowed=allowed, path_fields=path)


def test_plan_targeting_renumbers_an_emptied_block():
    plan = plan_targeting(
        _criteria(), add=[], remove=["urn:li:title:1", "urn:li:title:2"]
    )
    assert isinstance(plan, TargetingPlan)
    assert ("include_or:0", TITLES, "urn:li:title:3") in plan.target
    assert not any(key[0] == "include_or:1" for key in plan.target)


def test_plan_targeting_refusals():
    criteria = _criteria()

    def status(add, remove, tc=criteria):
        return _status(plan_targeting(tc, add=add, remove=remove))

    assert status([], ["urn:li:title:99"]) == "value_not_present"
    assert status([], ["urn:li:title:1", "urn:li:title:1"]) == "invalid_change"
    assert status([("urn:li:title:1", TITLES, False)], []) == "no_change"
    assert status([("urn:li:locale:de_DE", LANG, False)], []) == "invalid_change"
    assert status([("urn:li:organization:9", EMPLOYERS, False)], []) == (
        "invalid_change"
    )
    assert status([("urn:li:title:1", TITLES, False)], ["urn:li:title:1"]) == (
        "invalid_change"
    )
    both = {
        "include": {"and": [{"or": [_facet(TITLES, "urn:li:title:1")]}]},
        "exclude": {"or": [_facet(TITLES, "urn:li:title:1")]},
    }
    assert status([], ["urn:li:title:1"], both) == "invalid_change"
    assert status([], ["urn:li:title:1"], None) == "not_editable"


def test_refuse_invalid_targeting_change():
    ok = [{"urn": "urn:li:title:1", "query": "engineer"}]
    assert refuse_invalid_targeting_change(ok, None) is None
    assert refuse_invalid_targeting_change(None, None)
    assert refuse_invalid_targeting_change(None, ["urn:li:title:1"] * 11)
    assert refuse_invalid_targeting_change(None, ["not a urn"])
    assert refuse_invalid_targeting_change([{"urn": "urn:li:title:1", "query": ""}], [])
    assert refuse_invalid_targeting_change(
        [{"urn": "urn:li:title:1", "query": "a\x07b"}], []
    )
    assert refuse_invalid_targeting_change(
        [{"urn": "urn:li:title:1", "query": "x" * 101}], []
    )


def test_refuse_invalid_names_and_objective():
    assert refuse_invalid_audience_name("Example audience", saved=True) is None
    assert refuse_invalid_audience_name(" ", saved=False)
    assert refuse_invalid_audience_name("x" * 25, saved=True) is None
    assert "1 to 25" in _reason(refuse_invalid_audience_name("x" * 26, saved=True))
    assert refuse_invalid_audience_name("x" * 200, saved=False) is None
    assert refuse_invalid_audience_name("x" * 201, saved=False)
    assert refuse_invalid_audience_name("a\nb", saved=True)
    assert refuse_invalid_draft("WEBSITE_VISIT", "Example ad set") is None
    assert refuse_invalid_draft("website_visit", "Example ad set")
    assert refuse_invalid_draft("WEBSITE_VISIT", "")
    assert refuse_invalid_draft("WEBSITE_VISIT", "x" * 256)


# Campaign patch admission


def _admit(req: Req, record=None, **kwargs: Any):
    return admit_campaign_patch(
        req, campaign_id=CAMPAIGN, record=record or _record(), **kwargs
    )


def test_patch_admits_planned_budget_and_noops():
    req = _patch(
        **{
            "$set": {
                "dailyBudget": {"amount": "12.50", "currencyCode": "EUR"},
                "costType": "CPM",
                "contentTrackingId": "abc",
            }
        }
    )
    assert _admit(req, allowed={"dailyBudget": Decimal("12.5")}) is None


def test_patch_refuses_wrong_target_resource_or_method():
    allowed = {"dailyBudget": Decimal("12.5")}
    body = {
        "patch": {"$set": {"dailyBudget": {"amount": "12.5", "currencyCode": "EUR"}}}
    }
    assert _admit(_req(body, entity_id="100000009"), allowed=allowed)
    assert _admit(_req(body, restli="create"), allowed=allowed)
    assert _admit(_req(body, resource="billingConfiguration"), allowed=allowed)
    assert _admit(_req({"dailyBudget": 1}), allowed=allowed)
    assert _admit(_req(body, restli="PARTIAL_UPDATE"), allowed=allowed) is None


def test_patch_refuses_wrong_currency_value_stray_key_status_and_delete():
    allowed = {"dailyBudget": Decimal("12.5")}
    wrong_currency = _patch(
        **{"$set": {"dailyBudget": {"amount": "12.5", "currencyCode": "USD"}}}
    )
    assert _admit(wrong_currency, allowed=allowed)
    wrong_value = _patch(
        **{"$set": {"dailyBudget": {"amount": "13", "currencyCode": "EUR"}}}
    )
    assert _admit(wrong_value, allowed=allowed)
    stray = _patch(**{"$set": {"costType": "CPC"}})
    assert "costType" in _admit(stray, allowed=allowed)
    status = _patch(**{"$set": {"status": "ACTIVE"}})
    assert "status" in _admit(status, allowed=allowed)
    same_status = _patch(**{"$set": {"status": "DRAFT"}})
    assert "status" in _admit(same_status, allowed=allowed)
    delete_present = _patch(**{"$delete": ["dailyBudget"]})
    assert "dailyBudget" in _admit(delete_present, allowed=allowed)
    assert _admit(_patch(**{"$delete": ["totalBudget"]}), allowed=allowed) is None
    unreadable = _patch(
        **{"$set": {"dailyBudget": {"amount": "x", "currencyCode": "EUR"}}}
    )
    assert _admit(unreadable, allowed=allowed)


def test_patch_schedule_on_path_and_nested_form():
    record = _record()
    allowed = {"runSchedule": {"start": date(2026, 4, 1), "end": date(2026, 4, 9)}}
    path = ("runSchedule",)
    end_step = _patch(
        **{
            "$set": {
                "runSchedule": {
                    "start": _ms(date(2026, 3, 10)),
                    "end": _ms(date(2026, 4, 9)),
                }
            }
        }
    )
    assert _admit(end_step, record, allowed=allowed, path_fields=path) is None
    # Without path_fields an intermediate step is not the target.
    assert _admit(end_step, record, allowed=allowed)
    off_path = _patch(
        **{
            "$set": {
                "runSchedule": {
                    "start": _ms(date(2026, 3, 10)),
                    "end": _ms(date(2026, 4, 10)),
                }
            }
        }
    )
    assert _admit(off_path, record, allowed=allowed, path_fields=path)
    # An on-path pair whose start is not before its end is refused.
    inverted = _patch(
        **{
            "$set": {
                "runSchedule": {
                    "start": _ms(date(2026, 4, 1)),
                    "end": _ms(date(2026, 3, 20)),
                }
            }
        }
    )
    assert _admit(inverted, record, allowed=allowed, path_fields=path)
    nested = _patch(runSchedule={"$set": {"end": _ms(date(2026, 3, 25))}})
    end_only = {"runSchedule": {"start": date(2026, 3, 10), "end": date(2026, 3, 25)}}
    assert _admit(nested, record, allowed=end_only) is None
    nested_delete = _patch(runSchedule={"$delete": ["end"]})
    assert _admit(nested_delete, record, allowed=end_only)
    nested_start_delete = _patch(runSchedule={"$delete": ["start"]})
    assert _admit(nested_start_delete, record, allowed=end_only)


def test_patch_targeting_path_does_not_follow_renumbered_blocks():
    skills, titles = "urn:li:skill:1", "urn:li:title:2"
    industry = "urn:li:industry:3"

    def criteria(*blocks: dict) -> dict:
        return {"include": {"and": [{"or": block} for block in blocks]}}

    record = {
        "status": "DRAFT",
        "targetingCriteria": criteria({SKILLS: [skills]}, {TITLES: [titles]}),
    }
    plan = plan_targeting(
        record["targetingCriteria"],
        add=[(industry, INDUSTRIES, False)],
        remove=[skills],
    )
    assert isinstance(plan, TargetingPlan)
    allowed = {"targetingCriteria": plan.target}
    path = ("targetingCriteria",)

    def sent(*blocks: dict) -> Req:
        return _patch(**{"$set": {"targetingCriteria": criteria(*blocks)}})

    # The kept titles block is gone.
    dropped = sent({SKILLS: [skills]})
    assert _admit(dropped, record, allowed=allowed, path_fields=path)
    # Two blocks merged into one OR widen the audience.
    merged = sent({SKILLS: [skills], TITLES: [titles]})
    assert _admit(merged, record, allowed=allowed, path_fields=path)
    # The remove saved, the add not yet; then the new block alone.
    removed = sent({TITLES: [titles]})
    assert _admit(removed, record, allowed=allowed, path_fields=path) is None
    added = sent({SKILLS: [skills]}, {TITLES: [titles]}, {INDUSTRIES: [industry]})
    assert _admit(added, record, allowed=allowed, path_fields=path) is None
    done = sent({TITLES: [titles]}, {INDUSTRIES: [industry]})
    assert _admit(done, record, allowed=allowed) is None
    # The new facet ORed into the kept block is not its own block.
    joined = sent({TITLES: [titles], INDUSTRIES: [industry]})
    assert _admit(joined, record, allowed=allowed, path_fields=path)


def test_patch_targeting_on_path_subset_and_off_path_urn():
    record = _record()
    plan = plan_targeting(
        record["targetingCriteria"],
        add=[("urn:li:title:7", TITLES, False)],
        remove=["urn:li:title:1"],
    )
    assert isinstance(plan, TargetingPlan)
    allowed = {"targetingCriteria": plan.target}
    path = ("targetingCriteria",)

    def with_titles(*titles: str) -> Req:
        criteria = _criteria()
        criteria["include"]["and"][2] = {"or": [_facet(TITLES, *titles)]}
        return _patch(**{"$set": {"targetingCriteria": criteria}})

    # The remove landed, the add not yet: a subset step on the path.
    step = with_titles("urn:li:title:2")
    assert _admit(step, record, allowed=allowed, path_fields=path) is None
    done = with_titles("urn:li:title:2", "urn:li:title:7")
    assert _admit(done, record, allowed=allowed) is None
    off_path = with_titles("urn:li:title:2", "urn:li:title:8")
    assert _admit(off_path, record, allowed=allowed, path_fields=path)
    # Losing a value the plan keeps is off the path too.
    lost = with_titles("urn:li:title:7")
    assert _admit(lost, record, allowed=allowed, path_fields=path)


# Other admission rules


def test_status_admission():
    pause = _patch(**{"$set": {"status": "PAUSED"}})
    assert (
        admit_status_patch(
            pause, campaign_id=CAMPAIGN, from_status="ACTIVE", to_status="PAUSED"
        )
        is None
    )
    launch = _patch(**{"$set": {"status": "ACTIVE"}})
    assert admit_status_patch(
        launch, campaign_id=CAMPAIGN, from_status="DRAFT", to_status="ACTIVE"
    )
    extra = _patch(**{"$set": {"status": "PAUSED", "name": "Example"}})
    assert admit_status_patch(
        extra, campaign_id=CAMPAIGN, from_status="ACTIVE", to_status="PAUSED"
    )
    other = _patch(**{"$set": {"status": "ARCHIVED"}})
    assert admit_status_patch(
        other, campaign_id=CAMPAIGN, from_status="ACTIVE", to_status="PAUSED"
    )


def _create(path: str | None = None, params=None, **body: Any) -> Req:
    base = {
        "status": "DRAFT",
        "accountId": int(ACCOUNT),
        "campaignGroupId": f"urn:li:sponsoredCampaignGroup:{GROUP}",
        "objectiveType": "WEBSITE_VISIT",
        "name": "",
    }
    base.update(body)
    return _req(base, entity_id=None, restli="create", path=path, params=params)


def test_campaign_create_admission():
    def fresh():
        return admit_campaign_create(
            account_id=ACCOUNT, group_id=GROUP, objective="WEBSITE_VISIT"
        )

    assert "draft" in _reason(fresh()(_create(status="ACTIVE")))
    assert fresh()(_create(accountId=100000009))
    assert fresh()(_create(campaignGroupId=100000009))
    assert fresh()(_create(objectiveType="LEAD_GENERATION"))
    assert fresh()(_create()) is None


def test_create_admissions_refuse_sub_paths_actions_and_other_methods():
    def fresh():
        return admit_campaign_create(
            account_id=ACCOUNT, group_id=GROUP, objective="WEBSITE_VISIT"
        )

    sub_path = _create(path="campaignManagerCampaigns/draft")
    assert "sub-path" in _reason(fresh()(sub_path))
    matrix = _create(path="campaignManagerCampaigns;x=1")
    assert "sub-path" in _reason(fresh()(matrix))
    action = _create(params={"action": "duplicate"})
    assert "action" in _reason(fresh()(action))
    signed_url = {
        "owner": f"urn:li:sponsoredAccount:{ACCOUNT}",
        "uploadType": "MATCHED_AUDIENCE",
        "requestType": "UPLOAD",
    }

    def upload(**kwargs: Any) -> Req:
        return _req(
            signed_url,
            resource="campaignManagerAmbrySignedUrl",
            entity_id=None,
            **kwargs,
        )

    def lists():
        return admit_list_upload(account_id=ACCOUNT, name="Example list")

    assert lists()(upload(restli=None)) is None
    assert lists()(upload(restli="create")) is None
    assert "not a create" in _reason(lists()(upload(restli="batch_create")))
    sub = upload(restli=None, path="campaignManagerAmbrySignedUrl/x")
    assert "sub-path" in _reason(lists()(sub))
    assert "action" in _reason(lists()(upload(restli=None, params={"action": "x"})))


def test_campaign_create_admits_only_one_create():
    admit = admit_campaign_create(
        account_id=ACCOUNT, group_id=GROUP, objective="WEBSITE_VISIT"
    )
    # A refused create does not use up the one allowed create.
    assert admit(_create(status="ACTIVE"))
    assert admit(_create()) is None
    assert "second" in _reason(admit(_create()))


def test_creative_create_admission():
    post = "urn:li:share:100000005"
    body = {"campaignId": int(CAMPAIGN), "reference": post, "status": "ACTIVE"}
    req = _req(
        body, resource="campaignManagerCreatives", entity_id=None, restli="create"
    )
    assert admit_creative_create(req, campaign_id=CAMPAIGN, reference=post) is None
    assert admit_creative_create(
        req, campaign_id=CAMPAIGN, reference="urn:li:share:100000006"
    )
    assert admit_creative_create(req, campaign_id="100000009", reference=post)


def test_list_upload_admits_one_of_each():
    admit = admit_list_upload(account_id=ACCOUNT, name="Example list")
    owner = f"urn:li:sponsoredAccount:{ACCOUNT}"
    signed = _req(
        {
            "owner": owner,
            "requestType": "UPLOAD",
            "uploadType": "MATCHED_AUDIENCE",
            "contentType": "csv",
        },
        resource="campaignManagerAmbrySignedUrl",
        entity_id=None,
        restli=None,
    )
    segment_body = {
        "account": owner,
        "name": "Example list",
        "sourcePlatform": "LIST_UPLOAD",
        "segmentType": "COMPANY",
        "inputFile": "urn:li:media:x",
    }
    segment = _req(
        segment_body, resource="campaignManagerDmpSegments", entity_id=None, restli=None
    )
    assert admit(signed) is None
    assert admit(segment) is None
    assert "second" in _reason(admit(segment))
    assert "second" in _reason(admit(signed))
    fresh = admit_list_upload(account_id=ACCOUNT, name="Example list")
    conversion = _req(
        {**segment_body, "sourceType": "CONVERSION"},
        resource="campaignManagerDmpSegments",
        entity_id=None,
        restli=None,
    )
    assert fresh(conversion)
    assert fresh(_req({}, resource="billingConfiguration", entity_id=None))


def test_saved_audience_admission():
    keys = targeting_keys(_criteria())
    assert keys is not None
    body = {
        "name": "Example audience",
        "account": f"urn:li:sponsoredAccount:{ACCOUNT}",
        "ownership": "ACCOUNT",
        **_criteria(),
    }
    req = _req(
        body, resource="campaignManagerTargetingCriteria", entity_id=None, restli=None
    )

    def admit(name: str = "Example audience", target_keys=keys) -> str | None:
        return admit_saved_audience(
            account_id=ACCOUNT, name=name, target_keys=target_keys
        )(req)

    assert admit() is None
    assert admit(target_keys=keys[:-1])
    assert admit(name="Other")

    once = admit_saved_audience(
        account_id=ACCOUNT, name="Example audience", target_keys=keys
    )
    # A refused create does not use up the one allowed create.
    assert once(_req({}, resource="campaignManagerTargetingCriteria", entity_id=None))
    assert once(req) is None
    assert "second" in _reason(once(req))


def test_patch_schedule_compares_utc_dates():
    record = _record()
    end_only = {"runSchedule": {"start": date(2026, 3, 10), "end": date(2026, 3, 25)}}
    late_end = _patch(
        runSchedule={"$set": {"end": planned_end_ms(record, date(2026, 3, 25))}}
    )
    assert _admit(late_end, record, allowed=end_only) is None
    # 23:45 UTC read in a zone east of UTC is the next day; the UTC date rules.
    next_day = _patch(runSchedule={"$set": {"end": _ms(date(2026, 3, 26))}})
    assert _admit(next_day, record, allowed=end_only)


def _app_saved_audience_body(**overrides: Any) -> dict:
    """The save audience modal's create as the app bundle builds it.

    No ``ownership``, an HTML-encoded name and an empty description; the
    profile language block is left out (LinkedIn's locale-saving flag off),
    and the AND blocks arrive in another order.
    """
    criteria = _criteria()
    ands = [
        block
        for block in criteria["include"]["and"]
        if block["or"][0]["facet"]["urn"] != LANG
    ]
    body = {
        "name": "Example &amp; audience",
        "description": "",
        "include": {"and": list(reversed(ands))},
        "exclude": criteria["exclude"],
        "account": f"urn:li:sponsoredAccount:{ACCOUNT}",
    }
    body.update(overrides)
    return body


def test_saved_audience_admits_the_apps_real_create():
    keys = targeting_keys(_criteria())
    assert keys is not None

    def admit(body: dict) -> str | None:
        req = _req(
            body,
            resource="campaignManagerTargetingCriteria",
            entity_id=None,
            restli=None,
        )
        return admit_saved_audience(
            account_id=ACCOUNT, name="Example & audience", target_keys=keys
        )(req)

    assert admit(_app_saved_audience_body()) is None
    with_language = _app_saved_audience_body(**_criteria())
    assert admit(with_language) is None
    assert admit(_app_saved_audience_body(ownership="ACCOUNT")) is None

    assert "fields outside" in _reason(admit(_app_saved_audience_body(audienceCount=1)))
    assert "not the planned" in _reason(
        admit(_app_saved_audience_body(ownership="MEMBER"))
    )
    assert "not the planned" in _reason(admit(_app_saved_audience_body(name="Other")))
    other_language = _criteria()
    other_language["include"]["and"][1] = {"or": [_facet(LANG, "urn:li:locale:de_DE")]}
    assert "differs" in _reason(admit(_app_saved_audience_body(**other_language)))
    merged = _criteria()
    ands = merged["include"]["and"]
    ands[2]["or"] += ands.pop(3)["or"]
    assert "differs" in _reason(admit(_app_saved_audience_body(**merged)))


def test_saved_audience_admits_only_an_empty_description():
    keys = targeting_keys(_criteria())
    assert keys is not None

    def admit(body: dict) -> str | None:
        req = _req(
            body,
            resource="campaignManagerTargetingCriteria",
            entity_id=None,
            restli=None,
        )
        return admit_saved_audience(
            account_id=ACCOUNT, name="Example & audience", target_keys=keys
        )(req)

    assert admit(_app_saved_audience_body()) is None
    without = _app_saved_audience_body()
    del without["description"]
    assert admit(without) is None
    for description in ("Example description", " ", None):
        assert "not the planned" in _reason(
            admit(_app_saved_audience_body(description=description))
        )


def test_saved_audience_omitted_names_the_dropped_language_block():
    keys = targeting_keys(_criteria())
    assert keys is not None
    assert saved_audience_omitted(_app_saved_audience_body(), keys) == [
        "include_language"
    ]
    assert saved_audience_omitted(_app_saved_audience_body(**_criteria()), keys) == []
