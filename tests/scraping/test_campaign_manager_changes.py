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
) -> Req:
    return Req(CmCall(resource, entity_id, None, {}), method, restli, body)


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
        ("include_or:0", "urn:li:adTargetingFacet:skills", "urn:li:skill:5"),
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


def _create(**body: Any) -> Req:
    base = {
        "status": "DRAFT",
        "accountId": int(ACCOUNT),
        "campaignGroupId": f"urn:li:sponsoredCampaignGroup:{GROUP}",
        "objectiveType": "WEBSITE_VISIT",
        "name": "",
    }
    base.update(body)
    return _req(base, entity_id=None, restli="create")


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
            req, account_id=ACCOUNT, name=name, target_keys=target_keys
        )

    assert admit() is None
    assert admit(target_keys=keys[:-1])
    assert admit(name="Other")


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
