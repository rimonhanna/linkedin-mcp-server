"""Campaign Manager write tools: refusal, pacing and delegation."""

import ast
import inspect
from datetime import date, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError, ValidationError
from fastmcp.tools import FunctionTool

from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.exceptions import ActionLimitError
from linkedin_mcp_server.scraping import (
    campaign_manager_audiences,
    campaign_manager_changes,
    campaign_manager_content,
    campaign_manager_editor,
    campaign_manager_political,
    campaign_manager_targeting,
    campaign_manager_write,
)
from linkedin_mcp_server.scraping.campaign_manager_results import STATUSES, SUCCEEDED
from linkedin_mcp_server.tools.campaign_manager_actions import (
    register_campaign_manager_action_tools,
)

_MODULE = "linkedin_mcp_server.tools.campaign_manager_actions"
ACCOUNT = "100000001"
CAMPAIGN = "200000002"
ACCOUNT_URN = f"urn:li:sponsoredAccount:{ACCOUNT}"
CAMPAIGN_URN = f"urn:li:sponsoredCampaign:{CAMPAIGN}"

# One valid call per tool, with ids given as URNs so normalization shows.
VALID = {
    "update_campaign": {
        "ad_account_id": ACCOUNT_URN,
        "campaign_id": CAMPAIGN_URN,
        "daily_budget": "12.5",
        "confirm_send": True,
    },
    "set_campaign_status": {
        "ad_account_id": ACCOUNT_URN,
        "campaign_id": CAMPAIGN_URN,
        "status": "PAUSED",
        "confirm_send": True,
    },
    "update_campaign_targeting": {
        "ad_account_id": ACCOUNT_URN,
        "campaign_id": CAMPAIGN_URN,
        "add": [{"urn": " urn:li:title:100 ", "query": "engineer"}],
        "remove": ["urn:li:title:200"],
        "confirm_send": True,
    },
    "create_campaign_draft": {
        "ad_account_id": ACCOUNT_URN,
        "campaign_group_id": "urn:li:sponsoredCampaignGroup:300000003",
        "objective": "WEBSITE_VISIT",
        "name": "Example ad set",
        "confirm_send": True,
    },
    "add_post_to_campaign": {
        "ad_account_id": ACCOUNT_URN,
        "campaign_id": CAMPAIGN_URN,
        "post_urn": "https://www.linkedin.com/feed/update/urn:li:share:400000004/",
        "confirm_send": True,
    },
    "create_company_list_audience": {
        "ad_account_id": ACCOUNT_URN,
        "name": "Example list",
        "companies": [{"name": "Example company"}],
        "confirm_send": True,
    },
    "create_saved_audience": {
        "ad_account_id": ACCOUNT_URN,
        "campaign_id": CAMPAIGN_URN,
        "name": "Example audience",
        "confirm_send": True,
    },
}
TOOLS = tuple(VALID)

# Arguments that fail a pure check, so no browser is needed to refuse them.
INVALID_CHANGE = {
    "update_campaign": {"daily_budget": "12,5"},
    "update_campaign_targeting": {"remove": ["not-a-urn"]},
    "create_campaign_draft": {"objective": "website visit"},
    "create_company_list_audience": {"companies": []},
    "create_saved_audience": {"name": "x" * 26},
}


def _result(status: str = "confirmation_required", *, retry_safe: bool = True):
    return {"url": "u", "status": status, "message": "m", "retry_safe": retry_safe}


def _extractor(result=None) -> MagicMock:
    mock = MagicMock()
    for name in TOOLS:
        setattr(mock, name, AsyncMock(return_value=result or _result()))
    return mock


def _mcp() -> FastMCP:
    mcp = FastMCP("test")
    register_campaign_manager_action_tools(mcp)
    return mcp


@pytest.fixture
def pacing():
    with (
        patch(f"{_MODULE}.refuse_action") as refuse,
        patch(f"{_MODULE}.record_action") as record,
        patch(f"{_MODULE}.JobStore"),
    ):
        yield refuse, record


def _ready(extractor):
    return patch(f"{_MODULE}.get_ready_extractor", AsyncMock(return_value=extractor))


async def _call(name: str, args: dict, extractor) -> dict:
    with _ready(extractor) as ready:
        result = await _mcp().call_tool(name, args)
    ready.assert_awaited_once()
    return cast(dict, result.structured_content)


class TestRegistration:
    @pytest.mark.parametrize("name", TOOLS)
    async def test_tool_is_destructive_and_tagged(self, name):
        tool = await _mcp().get_tool(name)
        assert tool is not None
        assert tool.annotations is not None
        assert tool.annotations.destructiveHint is True
        assert tool.annotations.openWorldHint is True
        assert tool.tags == {"campaign_manager", "actions"}

    @pytest.mark.parametrize("name", TOOLS)
    async def test_docstring_opens_with_terminology_and_names_the_dry_run(self, name):
        tool = await _mcp().get_tool(name)
        assert tool is not None
        description = tool.description or ""
        assert description.lstrip().startswith("Terminology: API campaign group")
        assert "confirm_send False is a dry run" in " ".join(description.split())

    async def test_list_upload_says_it_accepts_the_ads_agreement(self):
        tool = await _mcp().get_tool("create_company_list_audience")
        assert tool is not None
        description = " ".join((tool.description or "").split())
        assert "accepts the LinkedIn Ads Agreement" in description
        assert "on the user's behalf" in description

    @pytest.mark.parametrize(
        ("name", "floor"),
        [("update_campaign_targeting", 300.0), ("create_company_list_audience", 300.0)],
    )
    async def test_long_tools_keep_their_own_timeout_floor(self, name, floor):
        tool = await _mcp().get_tool(name)
        assert tool is not None
        assert cast(FunctionTool, tool).timeout == floor


class TestDelegation:
    async def test_update_campaign(self, pacing):
        extractor = _extractor()
        await _call(
            "update_campaign",
            {**VALID["update_campaign"], "end_date": "2099-01-31"},
            extractor,
        )
        extractor.update_campaign.assert_awaited_once_with(
            ACCOUNT,
            CAMPAIGN,
            daily_budget="12.5",
            lifetime_budget=None,
            start_date=None,
            end_date=date(2099, 1, 31),
            confirm_send=True,
            political_ad=None,
        )

    async def test_set_campaign_status(self, pacing):
        extractor = _extractor()
        await _call("set_campaign_status", VALID["set_campaign_status"], extractor)
        extractor.set_campaign_status.assert_awaited_once_with(
            ACCOUNT, CAMPAIGN, status="PAUSED", confirm_send=True
        )

    async def test_update_campaign_targeting_normalizes_urns_and_passes_a_deadline(
        self, pacing
    ):
        extractor = _extractor()
        with patch(f"{_MODULE}.time.monotonic", return_value=1000.0):
            await _call(
                "update_campaign_targeting",
                VALID["update_campaign_targeting"],
                extractor,
            )
        call = extractor.update_campaign_targeting.await_args
        assert call.args == (ACCOUNT, CAMPAIGN)
        assert call.kwargs["add"] == [{"urn": "urn:li:title:100", "query": "engineer"}]
        assert call.kwargs["remove"] == ["urn:li:title:200"]
        assert call.kwargs["confirm_send"] is True
        assert call.kwargs["deadline"] == pytest.approx(1000.0 + 300.0 * 0.75)

    async def test_create_campaign_draft_normalizes_the_group(self, pacing):
        extractor = _extractor()
        await _call("create_campaign_draft", VALID["create_campaign_draft"], extractor)
        extractor.create_campaign_draft.assert_awaited_once_with(
            ACCOUNT,
            "300000003",
            objective="WEBSITE_VISIT",
            name="Example ad set",
            confirm_send=True,
        )

    async def test_add_post_to_campaign_normalizes_the_post_link(self, pacing):
        extractor = _extractor()
        await _call("add_post_to_campaign", VALID["add_post_to_campaign"], extractor)
        extractor.add_post_to_campaign.assert_awaited_once_with(
            ACCOUNT, CAMPAIGN, "urn:li:share:400000004", confirm_send=True
        )

    async def test_create_company_list_audience(self, pacing):
        extractor = _extractor()
        await _call(
            "create_company_list_audience",
            VALID["create_company_list_audience"],
            extractor,
        )
        extractor.create_company_list_audience.assert_awaited_once_with(
            ACCOUNT, "Example list", [{"name": "Example company"}], confirm_send=True
        )

    async def test_create_saved_audience(self, pacing):
        extractor = _extractor()
        await _call("create_saved_audience", VALID["create_saved_audience"], extractor)
        extractor.create_saved_audience.assert_awaited_once_with(
            ACCOUNT, CAMPAIGN, "Example audience", confirm_send=True
        )


class TestRefusedBeforeTheBrowser:
    @pytest.mark.parametrize(("name", "override"), INVALID_CHANGE.items())
    async def test_invalid_change_is_a_status_and_never_starts_the_browser(
        self, pacing, name, override
    ):
        refuse, record = pacing
        with _ready(_extractor()) as ready:
            result = await _mcp().call_tool(name, {**VALID[name], **override})
        content = cast(dict, result.structured_content)
        assert content["status"] == "invalid_change"
        assert content["retry_safe"] is True
        ready.assert_not_awaited()
        refuse.assert_not_called()
        record.assert_not_called()

    @pytest.mark.parametrize(
        ("name", "override"),
        [
            ("update_campaign", {"ad_account_id": "not-an-id"}),
            ("set_campaign_status", {"campaign_id": "../1"}),
            ("set_campaign_status", {"status": "DRAFT"}),
            ("add_post_to_campaign", {"post_urn": "urn:li:article:1"}),
            ("create_company_list_audience", {"ad_account_id": "abc"}),
            ("create_saved_audience", {"campaign_id": "x"}),
            ("create_campaign_draft", {"campaign_group_id": "example"}),
        ],
    )
    async def test_bad_reference_raises_and_never_starts_the_browser(
        self, pacing, name, override
    ):
        with _ready(_extractor()) as ready, pytest.raises((ToolError, ValidationError)):
            await _mcp().call_tool(name, {**VALID[name], **override})
        ready.assert_not_awaited()

    @pytest.mark.parametrize("name", TOOLS)
    async def test_action_limit_is_raised_before_the_browser(self, pacing, name):
        refuse, record = pacing
        refuse.side_effect = ActionLimitError(
            "campaign_writes", limit=30, window="24 h", resume_at=datetime(2099, 1, 1)
        )
        with _ready(_extractor()) as ready, pytest.raises(ToolError):
            await _mcp().call_tool(name, VALID[name])
        ready.assert_not_awaited()
        record.assert_not_called()


class TestPacing:
    @pytest.mark.parametrize("name", TOOLS)
    async def test_dry_run_neither_refuses_nor_records(self, pacing, name):
        refuse, record = pacing
        extractor = _extractor(_result(retry_safe=False))
        await _call(name, {**VALID[name], "confirm_send": False}, extractor)
        refuse.assert_not_called()
        record.assert_not_called()

    @pytest.mark.parametrize("name", TOOLS)
    async def test_a_write_that_may_have_landed_is_recorded_once(self, pacing, name):
        refuse, record = pacing
        extractor = _extractor(_result("save_unverified", retry_safe=False))
        await _call(name, VALID[name], extractor)
        refuse.assert_called_once()
        assert refuse.call_args.args[1] == "campaign_writes"
        record.assert_called_once()
        assert record.call_args.args[1] == "campaign_writes"

    @pytest.mark.parametrize("name", TOOLS)
    async def test_nothing_sent_records_nothing(self, pacing, name):
        refuse, record = pacing
        extractor = _extractor(_result("change_blocked", retry_safe=True))
        await _call(name, VALID[name], extractor)
        refuse.assert_called_once()
        record.assert_not_called()


class TestTraceRetention:
    @pytest.mark.parametrize(
        ("name", "status"),
        [(name, status) for name in TOOLS for status in sorted(STATUSES[name])],
    )
    async def test_every_status_is_decided_against_the_tools_success_set(
        self, pacing, name, status
    ):
        extractor = _extractor(_result(status))
        with patch(f"{_MODULE}.retain_trace_for_failed_action") as retain:
            await _call(name, VALID[name], extractor)
        retain.assert_called_once_with(name, status, succeeded=SUCCEEDED[name])

    def test_success_sets_hold_no_failure(self):
        for name in TOOLS:
            assert SUCCEEDED[name] <= STATUSES[name]
            assert not SUCCEEDED[name] & {
                "invalid_change",
                "save_unverified",
                "partially_applied",
                "change_blocked",
            }


_SCRAPERS = (
    campaign_manager_editor,
    campaign_manager_targeting,
    campaign_manager_content,
    campaign_manager_audiences,
)


def _literal_statuses(node: ast.expr) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _literal_statuses(node.body) | _literal_statuses(node.orelse)
    return set()


def _builder_statuses() -> set[str]:
    """Status literals the four scrapers pass straight to the result builder."""
    found: set[str] = set()
    for module in _SCRAPERS:
        for call in ast.walk(ast.parse(inspect.getsource(module))):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "campaign_action_result"
                and len(call.args) > 2
                and not any(isinstance(arg, ast.Starred) for arg in call.args[:3])
            ):
                found |= _literal_statuses(call.args[2])
    return found


def _string_literals() -> set[str]:
    found: set[str] = set()
    for module in (
        *_SCRAPERS,
        campaign_manager_write,
        campaign_manager_changes,
        campaign_manager_political,
    ):
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.add(node.value)
    return found


def test_scrapers_build_only_statuses_of_the_vocabulary():
    vocabulary = set().union(*STATUSES.values())
    assert _builder_statuses() <= vocabulary


def test_every_status_of_the_vocabulary_is_produced_somewhere():
    vocabulary = set().union(*STATUSES.values())
    assert vocabulary - _string_literals() == set()


class TestAuthError:
    @pytest.mark.parametrize("name", TOOLS)
    async def test_auth_error_triggers_relogin(self, pacing, name):
        extractor = _extractor()
        for tool in TOOLS:
            getattr(extractor, tool).side_effect = AuthenticationError("expired")
        relogin = AsyncMock(side_effect=RuntimeError("login window opened"))
        with (
            _ready(extractor),
            patch(f"{_MODULE}.handle_auth_error", relogin),
            pytest.raises(ToolError),
        ):
            await _mcp().call_tool(name, VALID[name])
        relogin.assert_awaited_once()
