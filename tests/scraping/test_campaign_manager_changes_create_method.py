"""Every create admission takes the same Rest.li method rule (synthetic data).

A POST to a bare collection is a create with or without the method header:
the app's ``restli.create`` sends ``create`` (a creative), its plain
``xhr.post`` sends none (a new ad set, a list upload, a saved audience).
"""

from __future__ import annotations

from typing import Any

import pytest

from linkedin_mcp_server.scraping.campaign_manager_changes import (
    admit_campaign_create,
    admit_creative_create,
    is_create_method,
)

from .test_campaign_manager_changes import ACCOUNT, CAMPAIGN, GROUP, _req

POST = "urn:li:share:100000005"


def _creative(**kwargs: Any) -> str | None:
    body = {"campaignId": int(CAMPAIGN), "reference": POST, "status": "ACTIVE"}
    req = _req(body, resource="campaignManagerCreatives", entity_id=None, **kwargs)
    return admit_creative_create(req, campaign_id=CAMPAIGN, reference=POST)


def _campaign(**kwargs: Any) -> str | None:
    body = {
        "status": "DRAFT",
        "accountId": int(ACCOUNT),
        "campaignGroupId": int(GROUP),
        "objectiveType": "WEBSITE_VISIT",
        "name": "Example draft",
    }
    admit = admit_campaign_create(
        account_id=ACCOUNT,
        group_id=GROUP,
        objective="WEBSITE_VISIT",
        name="Example draft",
    )
    return admit(_req(body, entity_id=None, **kwargs))


@pytest.mark.parametrize("admit", [_creative, _campaign])
@pytest.mark.parametrize("restli", [None, "create", "CREATE"])
def test_a_create_passes_with_or_without_the_header(admit, restli):
    assert admit(restli=restli) is None


@pytest.mark.parametrize("admit", [_creative, _campaign])
@pytest.mark.parametrize(
    "kwargs",
    [
        {"restli": "partial_update"},
        {"restli": "batch_create"},
        {"restli": "action"},
        {"restli": None, "path": "{resource}/100000009"},
        {"restli": None, "path": "{resource};x=1"},
        {"restli": None, "params": {"action": "duplicate"}},
    ],
    ids=[
        "partial-update",
        "batch-create",
        "action-method",
        "sub-path",
        "matrix",
        "action-param",
    ],
)
def test_anything_else_is_refused(admit, kwargs):
    resource = (
        "campaignManagerCreatives" if admit is _creative else "campaignManagerCampaigns"
    )
    if "path" in kwargs:
        kwargs = {**kwargs, "path": kwargs["path"].format(resource=resource)}
    assert admit(**kwargs)


def test_the_rule_itself():
    assert [is_create_method(m) for m in (None, "", "create", "Create")] == [True] * 4
    assert not any(
        is_create_method(m) for m in ("partial_update", "batch_create", "action", "get")
    )
