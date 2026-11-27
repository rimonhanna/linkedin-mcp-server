"""Structural contracts for the final extractor facade."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

import ast
import inspect
import sys

import pytest

from linkedin_mcp_server.scraping import LinkedInExtractor
from linkedin_mcp_server.scraping import contracts, text


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "linkedin_mcp_server"
SCRAPING = PACKAGE / "scraping"
EXTRACTOR = SCRAPING / "extractor.py"
FACADE_PACKAGE_IMPORTERS = {
    Path("linkedin_mcp_server/dependencies.py"),
    Path("tests/scraping/policy_scenarios.py"),
    Path("tests/scraping/test_facade_contracts.py"),
    Path("tests/scraping/test_facade_results.py"),
    Path("tests/scraping/test_facade_structure.py"),
    Path("tests/test_dependencies.py"),
}

PUBLIC_SIGNATURES = {
    "click_button_by_text": "(self, text: 'str', *, scope: 'str' = 'main', timeout: 'int' = 5000) -> 'bool'",
    "connect_with_person": "(self, username: 'str | ConnectTarget', *, note: 'str | None' = None) -> 'dict[str, Any]'",
    "extract_feed": "(self, num_posts: 'int' = 10) -> 'ExtractedSection'",
    "extract_page": "(self, url: 'str', section_name: 'str', max_scrolls: 'int | None' = None) -> 'ExtractedSection'",
    "get_company_employees": "(self, company_name: 'str', keywords: 'str | None' = None) -> 'dict[str, Any]'",
    "get_campaign": "(self, account_id: 'str', campaign_id: 'str') -> 'dict[str, Any]'",
    "get_campaign_analytics": "(self, account_id: 'str', *, level: 'str' = 'campaign', start_date: 'date | None' = None, end_date: 'date | None' = None, demographics: 'Sequence[str] | None' = None) -> 'dict[str, Any]'",
    "get_conversation": "(self, linkedin_username: 'str | None' = None, thread_id: 'str | None' = None, index: 'int' = 0) -> 'dict[str, Any]'",
    "get_inbox": "(self, limit: 'int' = 20, callbacks: 'ProgressCallback | None' = None) -> 'dict[str, Any]'",
    "get_my_profile": "(self, sections: 'set[str] | None' = None, callbacks: 'ProgressCallback | None' = None, max_scrolls: 'int | None' = None) -> 'dict[str, Any]'",
    "get_page_text": "(self) -> 'str'",
    "get_saved_jobs": "(self, max_pages: 'int' = 3, stage: 'JobsTrackerStage' = 'saved') -> 'dict[str, Any]'",
    "get_sidebar_profiles": "(self, username: 'str') -> 'dict[str, Any]'",
    "list_ad_accounts": "(self) -> 'dict[str, Any]'",
    "list_campaigns": "(self, account_id: 'str') -> 'dict[str, Any]'",
    "scrape_company": "(self, company_name: 'str', requested: 'set[str]', callbacks: 'ProgressCallback | None' = None) -> 'dict[str, Any]'",
    "scrape_job": "(self, job_id: 'str') -> 'dict[str, Any]'",
    "scrape_person": "(self, username: 'str', requested: 'set[str]', callbacks: 'ProgressCallback | None' = None, max_scrolls: 'int | None' = None, *, main_profile_already_loaded: 'bool' = False, allow_self_alias: 'bool' = False) -> 'dict[str, Any]'",
    "search_companies": "(self, keywords: 'str | None' = None, industry: 'list[str] | None' = None, size: 'list[str] | None' = None, hq_location: 'str | None' = None, has_jobs: 'bool | None' = None, max_pages: 'int' = 1) -> 'dict[str, Any]'",
    "search_conversations": "(self, keywords: 'str', limit: 'int' = 20) -> 'dict[str, Any]'",
    "search_jobs": "(self, keywords: 'str', location: 'str | None' = None, max_pages: 'int' = 3, date_posted: 'str | None' = None, job_type: 'str | None' = None, experience_level: 'str | None' = None, work_type: 'str | None' = None, easy_apply: 'bool' = False, sort_by: 'str | None' = None, tool_timeout: 'float' = 180.0) -> 'dict[str, Any]'",
    "search_people": "(self, keywords: 'str | None' = None, location: 'str | None' = None, network: 'list[str] | None' = None, current_company: 'str | list[str] | None' = None, max_pages: 'int' = 1, *, title: 'str | None' = None, past_company: 'str | list[str] | None' = None, industry: 'str | list[str] | None' = None, school: 'str | None' = None, first_name: 'str | None' = None, last_name: 'str | None' = None, profile_language: 'str | list[str] | None' = None) -> 'dict[str, Any]'",
    "search_posts": "(self, keywords: 'str', date_posted: 'str | None' = None, max_posts: 'int' = 10, content_type: 'str | None' = None) -> 'dict[str, Any]'",
    "withdraw_invitations": "(self, older_than_days: 'int', *, exclude_usernames: 'list[str] | None' = None, max_withdrawals: 'int' = 20, dry_run: 'bool' = True, deadline: 'float | None' = None) -> 'dict[str, Any]'",
    "update_campaign": "(self, account_id: 'str', campaign_id: 'str', *, daily_budget: 'str | None' = None, lifetime_budget: 'str | None' = None, start_date: 'date | None' = None, end_date: 'date | None' = None, confirm_send: 'bool', political_ad: 'bool | None' = None) -> 'dict[str, Any]'",
    "set_campaign_status": "(self, account_id: 'str', campaign_id: 'str', *, status: 'str', confirm_send: 'bool') -> 'dict[str, Any]'",
    "search_targeting_entities": "(self, account_id: 'str', campaign_id: 'str', query: 'str') -> 'dict[str, Any]'",
    "update_campaign_targeting": "(self, account_id: 'str', campaign_id: 'str', *, add: 'Sequence[TargetingAdd] | None' = None, remove: 'Sequence[str] | None' = None, confirm_send: 'bool', deadline: 'float | None' = None, political_ad: 'bool | None' = None) -> 'dict[str, Any]'",
    "create_campaign_draft": "(self, account_id: 'str', campaign_group_id: 'str', *, objective: 'str', name: 'str', confirm_send: 'bool', political_ad: 'bool | None' = None) -> 'dict[str, Any]'",
    "add_post_to_campaign": "(self, account_id: 'str', campaign_id: 'str', post_urn: 'str', *, confirm_send: 'bool') -> 'dict[str, Any]'",
    "list_sponsorable_posts": "(self, account_id: 'str', campaign_id: 'str') -> 'dict[str, Any]'",
    "list_audiences": "(self, account_id: 'str') -> 'dict[str, Any]'",
    "create_company_list_audience": "(self, account_id: 'str', name: 'str', companies: 'Sequence[Mapping[str, Any]]', *, confirm_send: 'bool') -> 'dict[str, Any]'",
    "create_saved_audience": "(self, account_id: 'str', campaign_id: 'str', name: 'str', *, confirm_send: 'bool') -> 'dict[str, Any]'",
    "search_sales_navigator_accounts": "(self, *, keywords: 'str | None' = None, industry: 'Sequence[str] | None' = None, exclude_industry: 'Sequence[str] | None' = None, hq_location: 'Sequence[str] | None' = None, size: 'Sequence[str] | None' = None, headcount_growth_min: 'int | None' = None, headcount_growth_max: 'int | None' = None, page: 'int' = 1) -> 'dict[str, Any]'",
    "search_sales_navigator_leads": "(self, *, keywords: 'str | None' = None, title: 'Sequence[str] | None' = None, seniority: 'Sequence[str] | None' = None, current_company: 'Sequence[str] | None' = None, exclude_current_company: 'Sequence[str] | None' = None, location: 'Sequence[str] | None' = None, company_hq_location: 'Sequence[str] | None' = None, industry: 'Sequence[str] | None' = None, exclude_industry: 'Sequence[str] | None' = None, network: 'Sequence[str] | None' = None, function: 'Sequence[str] | None' = None, company_size: 'Sequence[str] | None' = None, years_in_current_role: 'Sequence[str] | None' = None, years_at_current_company: 'Sequence[str] | None' = None, account_list: 'Sequence[str] | None' = None, lead_list: 'Sequence[str] | None' = None, exclude_lead_list: 'Sequence[str] | None' = None, page: 'int' = 1) -> 'dict[str, Any]'",
    "get_sales_navigator_account": "(self, company_id: 'str') -> 'dict[str, Any]'",
    "list_sales_navigator_account_lists": "(self, page: 'int' = 1, *, deadline: 'float | None' = None) -> 'dict[str, Any]'",
    "get_sales_navigator_account_list": "(self, list_id: 'str', page: 'int' = 1, *, deadline: 'float | None' = None) -> 'dict[str, Any]'",
    "list_sales_navigator_lead_lists": "(self, page: 'int' = 1, *, deadline: 'float | None' = None) -> 'dict[str, Any]'",
    "get_sales_navigator_lead_list": "(self, list_id: 'str', page: 'int' = 1, *, deadline: 'float | None' = None) -> 'dict[str, Any]'",
    "list_page_follow_candidates": "(self, company_id: 'str', *, keywords: 'str | None' = None, limit: 'int' = 20, deadline: 'float | None' = None) -> 'dict[str, Any]'",
    "invite_to_follow_page": "(self, company_id: 'str', people: 'list[str]', *, confirm_send: 'bool', names: 'list[str | None] | None' = None, deadline: 'float | None' = None) -> 'dict[str, Any]'",
    "get_sales_navigator_inbox": "(self, limit: 'int' = 20, filter: 'InboxFilter' = 'all', *, deadline: 'float | None' = None) -> 'dict[str, Any]'",
    "get_sales_navigator_conversation": "(self, thread_id: 'str | None' = None, lead_id: 'str | None' = None, index: 'int' = 0, *, deadline: 'float | None' = None) -> 'dict[str, Any]'",
    "search_sales_navigator_conversations": "(self, keywords: 'str', limit: 'int' = 20, *, deadline: 'float | None' = None) -> 'dict[str, Any]'",
    "create_sales_navigator_account_list": "(self, name: 'str', *, description: 'str | None' = None, accounts: 'Sequence[Mapping[str, Any]]' = (), confirm_send: 'bool') -> 'dict[str, Any]'",
    "add_accounts_to_sales_navigator_list": "(self, list_id: 'str', accounts: 'Sequence[Mapping[str, Any]]', *, confirm_send: 'bool') -> 'dict[str, Any]'",
    "delete_sales_navigator_account_list": "(self, list_id: 'str', *, expected_name: 'str', confirm_send: 'bool') -> 'dict[str, Any]'",
    "save_sales_navigator_search": "(self, kind: 'str', name: 'str', *, criteria: 'Mapping[str, Any]', confirm_send: 'bool') -> 'dict[str, Any]'",
    "send_message": "(self, linkedin_username: 'str', message: 'str', *, confirm_send: 'bool', profile_urn: 'str | None' = None) -> 'dict[str, Any]'",
    "reply_to_conversation": "(self, thread_id: 'str', message: 'str', *, confirm_send: 'bool') -> 'dict[str, Any]'",
    "send_sales_navigator_message": "(self, lead_id: 'str', message: 'str', *, confirm_send: 'bool', subject: 'str | None' = None, spend_inmail_credit: 'bool' = False) -> 'dict[str, Any]'",
    "reply_to_sales_navigator_conversation": "(self, thread_id: 'str', message: 'str', *, confirm_send: 'bool', spend_inmail_credit: 'bool' = False) -> 'dict[str, Any]'",
}

DELEGATES = {
    "click_button_by_text": ("_content", "click_button_by_text"),
    "connect_with_person": ("_connection", "connect_with_person"),
    "extract_feed": ("_feed", "extract_feed"),
    "extract_page": ("_capture", "extract_page"),
    "get_company_employees": ("_company", "get_company_employees"),
    "get_campaign": ("_campaigns", "get_campaign"),
    "get_campaign_analytics": ("_campaigns", "get_campaign_analytics"),
    "get_conversation": ("_conversations", "get_conversation"),
    "get_inbox": ("_conversations", "get_inbox"),
    "get_my_profile": ("_person", "get_my_profile"),
    "get_page_text": ("_content", "get_page_text"),
    "get_saved_jobs": ("_jobs", "get_saved_jobs"),
    "get_sidebar_profiles": ("_person", "get_sidebar_profiles"),
    "list_ad_accounts": ("_campaigns", "list_ad_accounts"),
    "list_campaigns": ("_campaigns", "list_campaigns"),
    "scrape_company": ("_company", "scrape_company"),
    "scrape_job": ("_jobs", "scrape_job"),
    "scrape_person": ("_person", "scrape_person"),
    "search_companies": ("_company", "search_companies"),
    "search_conversations": ("_conversations", "search_conversations"),
    "search_jobs": ("_jobs", "search_jobs"),
    "search_people": ("_person", "search_people"),
    "search_posts": ("_posts", "search_posts"),
    "send_message": ("_message_sender", "send_message"),
    "reply_to_conversation": ("_message_sender", "reply_to_conversation"),
    "withdraw_invitations": ("_connection", "withdraw_invitations"),
    "update_campaign": ("_campaign_editor", "update_campaign"),
    "set_campaign_status": ("_campaign_editor", "set_campaign_status"),
    "search_targeting_entities": ("_campaign_targeting", "search_targeting_entities"),
    "update_campaign_targeting": ("_campaign_targeting", "update_campaign_targeting"),
    "create_campaign_draft": ("_campaign_editor", "create_campaign_draft"),
    "add_post_to_campaign": ("_campaign_content", "add_post_to_campaign"),
    "list_sponsorable_posts": ("_campaign_content", "list_sponsorable_posts"),
    "list_audiences": ("_audiences", "list_audiences"),
    "create_company_list_audience": ("_audiences", "create_company_list_audience"),
    "create_saved_audience": ("_audiences", "create_saved_audience"),
    "search_sales_navigator_accounts": ("_sales_navigator", "search_accounts"),
    "search_sales_navigator_leads": ("_sales_navigator", "search_leads"),
    "get_sales_navigator_account": ("_sales_navigator", "get_account"),
    "list_sales_navigator_account_lists": ("_sales_navigator", "list_account_lists"),
    "get_sales_navigator_account_list": ("_sales_navigator", "get_account_list"),
    "list_sales_navigator_lead_lists": ("_sales_navigator", "list_lead_lists"),
    "get_sales_navigator_lead_list": ("_sales_navigator", "get_lead_list"),
    "list_page_follow_candidates": ("_page_invites", "list_follow_candidates"),
    "invite_to_follow_page": ("_page_invites", "invite_to_follow"),
    "get_sales_navigator_inbox": ("_sales_inbox", "get_inbox"),
    "get_sales_navigator_conversation": ("_sales_inbox", "get_conversation"),
    "search_sales_navigator_conversations": ("_sales_inbox", "search_conversations"),
    "send_sales_navigator_message": ("_sales_navigator_sender", "send_message"),
    "reply_to_sales_navigator_conversation": (
        "_sales_navigator_sender",
        "reply_to_conversation",
    ),
    "create_sales_navigator_account_list": (
        "_sales_navigator_lists",
        "create_account_list",
    ),
    "add_accounts_to_sales_navigator_list": ("_sales_navigator_lists", "add_accounts"),
    "delete_sales_navigator_account_list": (
        "_sales_navigator_lists",
        "delete_account_list",
    ),
    "save_sales_navigator_search": ("_sales_navigator_searches", "save_search"),
}

DELEGATE_CALLS = {
    "click_button_by_text": "self._content.click_button_by_text(text, scope=scope, timeout=timeout)",
    "connect_with_person": "self._connection.connect_with_person(username, note=note)",
    "extract_feed": "self._feed.extract_feed(num_posts)",
    "extract_page": "self._capture.extract_page(url, section_name, max_scrolls)",
    "get_company_employees": "self._company.get_company_employees(company_name, keywords)",
    "get_campaign": "self._campaigns.get_campaign(account_id, campaign_id)",
    "get_campaign_analytics": "self._campaigns.get_campaign_analytics(account_id, level=level, start_date=start_date, end_date=end_date, demographics=demographics)",
    "get_conversation": "self._conversations.get_conversation(linkedin_username, thread_id, index)",
    "get_inbox": "self._conversations.get_inbox(limit, callbacks)",
    "get_my_profile": "self._person.get_my_profile(sections, callbacks, max_scrolls)",
    "get_page_text": "self._content.get_page_text()",
    "get_saved_jobs": "self._jobs.get_saved_jobs(max_pages, stage)",
    "get_sidebar_profiles": "self._person.get_sidebar_profiles(username)",
    "list_ad_accounts": "self._campaigns.list_ad_accounts()",
    "list_campaigns": "self._campaigns.list_campaigns(account_id)",
    "scrape_company": "self._company.scrape_company(company_name, requested, callbacks)",
    "scrape_job": "self._jobs.scrape_job(job_id)",
    "scrape_person": "self._person.scrape_person(username, requested, callbacks, max_scrolls, main_profile_already_loaded=main_profile_already_loaded, allow_self_alias=allow_self_alias)",
    "search_companies": "self._company.search_companies(keywords, industry=industry, size=size, hq_location=hq_location, has_jobs=has_jobs, max_pages=max_pages)",
    "search_conversations": "self._conversations.search_conversations(keywords, limit)",
    "search_jobs": "self._jobs.search_jobs(keywords, location, max_pages, date_posted, job_type, experience_level, work_type, easy_apply, sort_by, tool_timeout)",
    "search_people": "self._person.search_people(keywords, location=location, network=network, current_company=current_company, max_pages=max_pages, title=title, past_company=past_company, industry=industry, school=school, first_name=first_name, last_name=last_name, profile_language=profile_language)",
    "search_posts": "self._posts.search_posts(keywords, date_posted=date_posted, max_posts=max_posts, content_type=content_type)",
    "send_message": "self._message_sender.send_message(linkedin_username, message, confirm_send=confirm_send, profile_urn=profile_urn)",
    "reply_to_conversation": "self._message_sender.reply_to_conversation(thread_id, message, confirm_send=confirm_send)",
    "withdraw_invitations": "self._connection.withdraw_invitations(older_than_days, exclude_usernames=exclude_usernames, max_withdrawals=max_withdrawals, dry_run=dry_run, deadline=deadline)",
    "update_campaign": "self._campaign_editor.update_campaign(account_id, campaign_id, daily_budget=daily_budget, lifetime_budget=lifetime_budget, start_date=start_date, end_date=end_date, confirm_send=confirm_send, political_ad=political_ad)",
    "set_campaign_status": "self._campaign_editor.set_campaign_status(account_id, campaign_id, status=status, confirm_send=confirm_send)",
    "search_targeting_entities": "self._campaign_targeting.search_targeting_entities(account_id, campaign_id, query)",
    "update_campaign_targeting": "self._campaign_targeting.update_campaign_targeting(account_id, campaign_id, add=add, remove=remove, confirm_send=confirm_send, deadline=deadline, political_ad=political_ad)",
    "create_campaign_draft": "self._campaign_editor.create_campaign_draft(account_id, campaign_group_id, objective=objective, name=name, confirm_send=confirm_send, political_ad=political_ad)",
    "add_post_to_campaign": "self._campaign_content.add_post_to_campaign(account_id, campaign_id, post_urn, confirm_send=confirm_send)",
    "list_sponsorable_posts": "self._campaign_content.list_sponsorable_posts(account_id, campaign_id)",
    "list_audiences": "self._audiences.list_audiences(account_id)",
    "create_company_list_audience": "self._audiences.create_company_list_audience(account_id, name, companies, confirm_send=confirm_send)",
    "create_saved_audience": "self._audiences.create_saved_audience(account_id, campaign_id, name, confirm_send=confirm_send)",
    "search_sales_navigator_accounts": "self._sales_navigator.search_accounts(keywords=keywords, industry=industry, exclude_industry=exclude_industry, hq_location=hq_location, size=size, headcount_growth_min=headcount_growth_min, headcount_growth_max=headcount_growth_max, page=page)",
    "search_sales_navigator_leads": "self._sales_navigator.search_leads(keywords=keywords, title=title, seniority=seniority, current_company=current_company, exclude_current_company=exclude_current_company, location=location, company_hq_location=company_hq_location, industry=industry, exclude_industry=exclude_industry, network=network, function=function, company_size=company_size, years_in_current_role=years_in_current_role, years_at_current_company=years_at_current_company, account_list=account_list, lead_list=lead_list, exclude_lead_list=exclude_lead_list, page=page)",
    "get_sales_navigator_account": "self._sales_navigator.get_account(company_id)",
    "list_sales_navigator_account_lists": (
        "self._sales_navigator.list_account_lists(page, deadline=deadline)"
    ),
    "get_sales_navigator_account_list": (
        "self._sales_navigator.get_account_list(list_id, page, deadline=deadline)"
    ),
    "list_sales_navigator_lead_lists": (
        "self._sales_navigator.list_lead_lists(page, deadline=deadline)"
    ),
    "get_sales_navigator_lead_list": (
        "self._sales_navigator.get_lead_list(list_id, page, deadline=deadline)"
    ),
    "list_page_follow_candidates": (
        "self._page_invites.list_follow_candidates(company_id, keywords=keywords, "
        "limit=limit, deadline=deadline)"
    ),
    "invite_to_follow_page": (
        "self._page_invites.invite_to_follow(company_id, people, "
        "confirm_send=confirm_send, names=names, deadline=deadline)"
    ),
    "get_sales_navigator_inbox": (
        "self._sales_inbox.get_inbox(limit, filter, deadline=deadline)"
    ),
    "get_sales_navigator_conversation": (
        "self._sales_inbox.get_conversation(thread_id, lead_id, index, deadline=deadline)"
    ),
    "search_sales_navigator_conversations": (
        "self._sales_inbox.search_conversations(keywords, limit, deadline=deadline)"
    ),
    "send_sales_navigator_message": "self._sales_navigator_sender.send_message(lead_id, message, confirm_send=confirm_send, subject=subject, spend_inmail_credit=spend_inmail_credit)",
    "reply_to_sales_navigator_conversation": "self._sales_navigator_sender.reply_to_conversation(thread_id, message, confirm_send=confirm_send, spend_inmail_credit=spend_inmail_credit)",
    "create_sales_navigator_account_list": (
        "self._sales_navigator_lists.create_account_list(name, "
        "description=description, accounts=accounts, confirm_send=confirm_send)"
    ),
    "add_accounts_to_sales_navigator_list": (
        "self._sales_navigator_lists.add_accounts(list_id, accounts, "
        "confirm_send=confirm_send)"
    ),
    "delete_sales_navigator_account_list": (
        "self._sales_navigator_lists.delete_account_list(list_id, "
        "expected_name=expected_name, confirm_send=confirm_send)"
    ),
    "save_sales_navigator_search": (
        "self._sales_navigator_searches.save_search(kind, name, "
        "criteria=criteria, confirm_send=confirm_send)"
    ),
}

FACADE_STATE = {
    "_audiences",
    "_campaign_content",
    "_campaign_editor",
    "_campaign_targeting",
    "_campaigns",
    "_capture",
    "_company",
    "_connection",
    "_content",
    "_conversations",
    "_feed",
    "_jobs",
    "_message_sender",
    "_page_invites",
    "_person",
    "_posts",
    "_sales_inbox",
    "_sales_navigator",
    "_sales_navigator_lists",
    "_sales_navigator_searches",
    "_sales_navigator_sender",
}

PERMANENT_ALIASES = {
    "ExtractedSection": contracts.ExtractedSection,
    "FilterValidationError": contracts.FilterValidationError,
    "rate_limited_section_error": contracts.rate_limited_section_error,
    "strip_linkedin_noise": text.strip_linkedin_noise,
    "strip_conversation_chrome": text.strip_conversation_chrome,
}

FORBIDDEN_LAYER_PREFIXES = (
    "linkedin_mcp_server.browser_",
    "linkedin_mcp_server.daemon",
    "linkedin_mcp_server.drivers",
    "linkedin_mcp_server.hidden_target",
    "linkedin_mcp_server.private_state",
    "linkedin_mcp_server.process_",
    "linkedin_mcp_server.profile_claim",
    "linkedin_mcp_server.profile_lease",
    "linkedin_mcp_server.server_role",
    "linkedin_mcp_server.session_state",
)


def _facade(source: str) -> ast.ClassDef:
    tree = ast.parse(source)
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "LinkedInExtractor"
    )


def _method(facade: ast.ClassDef, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    return next(
        node
        for node in facade.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )


def _assert_facade_shape(source: str) -> None:
    facade = _facade(source)
    assert not facade.bases
    assert not facade.keywords
    methods = {
        node.name: node
        for node in facade.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert set(methods) == {*PUBLIC_SIGNATURES, "__init__"}
    assert all(
        isinstance(methods[name], ast.AsyncFunctionDef) for name in PUBLIC_SIGNATURES
    )
    assert isinstance(methods["__init__"], ast.FunctionDef)
    assert "__getattr__" not in methods


def _assert_delegate_bodies(source: str) -> None:
    facade = _facade(source)
    for name, (owner, target) in DELEGATES.items():
        method = _method(facade, name)
        statements = method.body[1:] if ast.get_docstring(method) else method.body
        assert len(statements) == 1
        statement = statements[0]
        assert isinstance(statement, ast.Return)
        assert isinstance(statement.value, ast.Await)
        call = statement.value.value
        assert isinstance(call, ast.Call)
        assert ast.unparse(call) == DELEGATE_CALLS[name]
        assert isinstance(call.func, ast.Attribute) and call.func.attr == target
        receiver = call.func.value
        assert isinstance(receiver, ast.Attribute) and receiver.attr == owner
        assert isinstance(receiver.value, ast.Name) and receiver.value.id == "self"
        assert not any(
            isinstance(node, (ast.Try, ast.Raise)) for node in ast.walk(method)
        )

        parameters = {
            argument.arg
            for argument in [
                *method.args.posonlyargs,
                *method.args.args[1:],
                *method.args.kwonlyargs,
            ]
        }
        forwarded = {
            node.id
            for argument in [*call.args, *(keyword.value for keyword in call.keywords)]
            for node in ast.walk(argument)
            if isinstance(node, ast.Name)
        }
        assert forwarded == parameters


def _assert_state(source: str) -> None:
    init = _method(_facade(source), "__init__")
    assigned = {
        node.attr
        for node in ast.walk(init)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and isinstance(node.ctx, ast.Store)
    }
    assert assigned == FACADE_STATE


def _imports(path: Path, source: str) -> set[str]:
    package_parts = list(path.parent.parts)
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source, filename=str(path))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parent_parts = package_parts[: len(package_parts) - node.level + 1]
                imported_from = ".".join(
                    [*parent_parts, *(node.module or "").split(".")]
                ).rstrip(".")
            else:
                imported_from = node.module or ""
            if imported_from:
                modules.add(imported_from)
            modules.update(
                f"{imported_from}.{alias.name}" if imported_from else alias.name
                for alias in node.names
                if alias.name != "*"
            )
    return modules


def _assert_scraping_dependencies(sources: dict[Path, str]) -> None:
    graph: dict[str, set[str]] = defaultdict(set)
    for path, source in sources.items():
        module = f"linkedin_mcp_server.scraping.{path.stem}"
        for imported in _imports(path, source):
            assert not imported.startswith(FORBIDDEN_LAYER_PREFIXES)
            if path.name not in {"__init__.py", "extractor.py"}:
                assert imported not in {
                    "linkedin_mcp_server.scraping",
                    "linkedin_mcp_server.scraping.extractor",
                }
            if imported.startswith("linkedin_mcp_server.scraping."):
                graph[module].add(imported)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module: str) -> None:
        assert module not in visiting, f"scraping import cycle through {module}"
        if module in visited:
            return
        visiting.add(module)
        for dependency in graph[module]:
            visit(dependency)
        visiting.remove(module)
        visited.add(module)

    for module in list(graph):
        visit(module)


def _assert_no_private_facade_accesses(sources: dict[Path, str]) -> None:
    for path, source in sources.items():
        tree = ast.parse(source, filename=str(path))
        facade_classes = {"LinkedInExtractor"}
        facade_names = {"extractor"}
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if _resolved_import_module(path, node) != "linkedin_mcp_server.scraping":
                continue
            facade_classes.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "LinkedInExtractor"
            )

        assignments = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
        ]
        changed = True
        while changed:
            changed = False
            for node in assignments:
                value = node.value
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                names = {
                    target.id for target in targets if isinstance(target, ast.Name)
                }
                if isinstance(value, ast.Name) and value.id in facade_classes:
                    additions = names - facade_classes
                    facade_classes.update(additions)
                    changed |= bool(additions)
                if (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id in facade_classes
                ) or (isinstance(value, ast.Name) and value.id in facade_names):
                    additions = names - facade_names
                    facade_names.update(additions)
                    changed |= bool(additions)

        facade_names.update(facade_classes)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr.startswith("_")
                and isinstance(node.value, ast.Name)
                and node.value.id in facade_names
            ):
                raise AssertionError(
                    f"{path}:{node.lineno}: private facade access {node.attr}"
                )
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"object", "setattr"}
                and len(node.args) > 1
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in facade_names
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
                and node.args[1].value.startswith("_")
            ):
                raise AssertionError(
                    f"{path}:{node.lineno}: private facade patch {node.args[1].value}"
                )


def _resolved_import_module(path: Path, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module
    package = list(path.parent.parts)
    keep = len(package) - node.level + 1
    if keep < 0:
        return None
    resolved = package[:keep]
    if node.module:
        resolved.extend(node.module.split("."))
    return ".".join(resolved)


def _facade_package_imports(
    path: Path, tree: ast.AST
) -> list[ast.Import | ast.ImportFrom]:
    imports: list[ast.Import | ast.ImportFrom] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name == "linkedin_mcp_server.scraping" for alias in node.names
        ):
            imports.append(node)
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolved_import_module(path, node)
            if resolved == "linkedin_mcp_server" and any(
                alias.name == "scraping" for alias in node.names
            ):
                imports.append(node)
            elif resolved == "linkedin_mcp_server.scraping" and any(
                alias.name in {"LinkedInExtractor", "*"} for alias in node.names
            ):
                imports.append(node)
    return imports


def _assert_no_obsolete_extractor_seams(sources: dict[Path, str]) -> None:
    extractor_module = "linkedin_mcp_server.scraping.extractor"
    for path, source in sources.items():
        tree = ast.parse(source, filename=str(path))
        if path not in FACADE_PACKAGE_IMPORTERS:
            imports = _facade_package_imports(path, tree)
            assert not imports, (
                f"{path}:{imports[0].lineno}: unauthorized scraping facade import"
            )
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name != extractor_module for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                imported_from = _resolved_import_module(path, node)
                names = {alias.name for alias in node.names}
                if imported_from == extractor_module:
                    package_export = path == Path(
                        "linkedin_mcp_server/scraping/__init__.py"
                    ) and names == {"LinkedInExtractor"}
                    permanent_aliases = path == Path(
                        "tests/scraping/test_facade_contracts.py"
                    ) and names <= set(PERMANENT_ALIASES)
                    assert package_export or permanent_aliases
                if imported_from == "linkedin_mcp_server.scraping":
                    assert "extractor" not in names
                    if "LinkedInExtractor" in names:
                        assert path in FACADE_PACKAGE_IMPORTERS
            if isinstance(node, ast.Call) and node.args:
                function = node.func
                is_patch = isinstance(function, ast.Name) and function.id == "patch"
                is_setattr = (
                    isinstance(function, ast.Attribute) and function.attr == "setattr"
                )
                target = node.args[0]
                if (
                    (is_patch or is_setattr)
                    and isinstance(target, ast.Constant)
                    and isinstance(target.value, str)
                ):
                    assert not target.value.startswith(
                        "linkedin_mcp_server.scraping.extractor."
                    )


def _sources(root: Path) -> dict[Path, str]:
    return {
        path.relative_to(ROOT): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.py"))
    }


def _assert_runtime_signatures(facade: Any) -> None:
    assert {
        name: str(inspect.signature(getattr(facade, name)))
        for name in PUBLIC_SIGNATURES
    } == PUBLIC_SIGNATURES


def test_public_surface_signatures_and_coroutine_status_are_exact():
    source = EXTRACTOR.read_text(encoding="utf-8")
    _assert_facade_shape(source)
    _assert_runtime_signatures(LinkedInExtractor)


def test_facade_state_and_delegate_bodies_are_explicit():
    source = EXTRACTOR.read_text(encoding="utf-8")
    _assert_state(source)
    _assert_delegate_bodies(source)


def test_package_export_and_permanent_aliases_keep_identity():
    module = sys.modules["linkedin_mcp_server.scraping.extractor"]
    assert LinkedInExtractor is module.LinkedInExtractor
    for name, owner in PERMANENT_ALIASES.items():
        assert getattr(module, name) is owner


def test_scraping_dependencies_are_one_way_acyclic_and_layered():
    _assert_scraping_dependencies(_sources(SCRAPING))


def test_imports_canonicalize_absolute_and_relative_from_imports():
    path = Path("linkedin_mcp_server/scraping/nested/module.py")
    source = """
from linkedin_mcp_server import process_protocol
from .. import capture
from ..capture import SectionCapture
from ...config import settings
"""

    imports = _imports(path, source)

    assert "linkedin_mcp_server.process_protocol" in imports
    assert "linkedin_mcp_server.scraping.capture" in imports
    assert "linkedin_mcp_server.config" in imports


def test_obsolete_extractor_seams_and_private_accesses_are_absent():
    sources = {**_sources(PACKAGE), **_sources(ROOT / "tests")}
    _assert_no_obsolete_extractor_seams(sources)
    _assert_no_private_facade_accesses(sources)


@pytest.mark.parametrize(
    "mutate,guard",
    [
        (
            lambda value: value.replace(
                "async def get_page_text", "async def page_text", 1
            ),
            _assert_facade_shape,
        ),
        (
            lambda value: value.replace(
                "async def get_page_text", "def get_page_text", 1
            ),
            _assert_facade_shape,
        ),
        (
            lambda value: value.replace(
                "self._content = content",
                "self._content = content\n        self._page = page",
                1,
            ),
            _assert_state,
        ),
        (
            lambda value: value.replace(
                "self._jobs.search_jobs", "self._posts.search_jobs", 1
            ),
            _assert_delegate_bodies,
        ),
        (
            lambda value: value.replace(
                "            keywords,\n            location,",
                "            location,\n            keywords,",
                1,
            ),
            _assert_delegate_bodies,
        ),
    ],
)
def test_facade_ast_guards_reject_representative_mutations(mutate, guard):
    with pytest.raises(AssertionError):
        guard(mutate(EXTRACTOR.read_text(encoding="utf-8")))


@pytest.mark.parametrize(
    "path,addition",
    [
        (
            Path("linkedin_mcp_server/scraping/content.py"),
            "from . import capture\n",
        ),
        (
            Path("linkedin_mcp_server/scraping/content.py"),
            "from .capture import SectionCapture\n",
        ),
        (
            Path("linkedin_mcp_server/scraping/content.py"),
            "from linkedin_mcp_server import process_protocol\n",
        ),
        (
            Path("linkedin_mcp_server/scraping/content.py"),
            "from .. import process_protocol\n",
        ),
        (
            Path("linkedin_mcp_server/scraping/content.py"),
            "from linkedin_mcp_server.daemon_owner import owner\n",
        ),
        (
            Path("linkedin_mcp_server/scraping/content.py"),
            "from linkedin_mcp_server.scraping import LinkedInExtractor\n",
        ),
    ],
)
def test_dependency_guards_reject_cycles_layers_and_reverse_imports(path, addition):
    sources = _sources(SCRAPING)
    sources[path] = addition + sources[path]
    with pytest.raises(AssertionError):
        _assert_scraping_dependencies(sources)


@pytest.mark.parametrize(
    "path,addition",
    [
        (
            Path("linkedin_mcp_server/tools/feed.py"),
            "from linkedin_mcp_server.scraping.extractor import LinkedInExtractor\n",
        ),
        (
            Path("tests/test_tools.py"),
            "import linkedin_mcp_server.scraping.extractor as extractor_module\n"
            "patch.object(extractor_module, '_drain_listener_tasks', fake)\n",
        ),
        (
            Path("tests/test_tools.py"),
            "import linkedin_mcp_server.scraping.extractor as extractor_module\n"
            "monkeypatch.setattr("
            "extractor_module, '_drain_listener_tasks', fake, raising=False)\n",
        ),
        (
            Path("linkedin_mcp_server/server.py"),
            "from linkedin_mcp_server.scraping import extractor as extractor_module\n",
        ),
        (
            Path("linkedin_mcp_server/server.py"),
            "import linkedin_mcp_server.scraping\n",
        ),
        (
            Path("linkedin_mcp_server/server.py"),
            "import linkedin_mcp_server.scraping as scraping\n",
        ),
        (
            Path("linkedin_mcp_server/server.py"),
            "from linkedin_mcp_server import scraping\n",
        ),
        (
            Path("linkedin_mcp_server/server.py"),
            "from linkedin_mcp_server import scraping as scraping_package\n",
        ),
        (
            Path("linkedin_mcp_server/server.py"),
            "from linkedin_mcp_server.scraping import LinkedInExtractor\n",
        ),
        (
            Path("linkedin_mcp_server/server.py"),
            "from linkedin_mcp_server.scraping import LinkedInExtractor as Facade\n",
        ),
        (
            Path("linkedin_mcp_server/server.py"),
            "from linkedin_mcp_server.scraping import *\n",
        ),
        (
            Path("tests/test_tools.py"),
            "from unittest.mock import patch\npatch('linkedin_mcp_server.scraping.extractor.logger')\n",
        ),
    ],
)
def test_obsolete_extractor_seam_guard_rejects_mutations(path, addition):
    sources = {path: addition}
    with pytest.raises(AssertionError):
        _assert_no_obsolete_extractor_seams(sources)


@pytest.mark.parametrize(
    "source",
    [
        "import linkedin_mcp_server.scraping\n",
        "import linkedin_mcp_server.scraping as scraping\n",
        "from linkedin_mcp_server import scraping\n",
        "from linkedin_mcp_server import scraping as scraping_package\n",
        "from linkedin_mcp_server.scraping import LinkedInExtractor\n",
        "from linkedin_mcp_server.scraping import LinkedInExtractor as Facade\n",
        "from linkedin_mcp_server.scraping import *\n",
    ],
)
def test_approved_facade_consumers_accept_equivalent_import_forms(source):
    _assert_no_obsolete_extractor_seams(
        {Path("linkedin_mcp_server/dependencies.py"): source}
    )


def test_non_facade_scraping_imports_and_unrelated_scopes_are_allowed():
    _assert_no_obsolete_extractor_seams(
        {
            Path("linkedin_mcp_server/tools/feed.py"): (
                "from linkedin_mcp_server.scraping import contracts, fields\n"
                "from linkedin_mcp_server.scraping.contracts import ExtractedSection\n"
                "\n"
                "def unrelated(scraping):\n"
                "    return scraping.LinkedInExtractor\n"
            )
        }
    )


@pytest.mark.parametrize(
    "addition",
    [
        "extractor = LinkedInExtractor(page)\nextractor._content\n",
        "patch.object(LinkedInExtractor, '_content', replacement)\n",
        "from linkedin_mcp_server.scraping import LinkedInExtractor as Facade\n"
        "subject = Facade(page)\n"
        "patch.object(subject, '_content', fake)\n",
    ],
)
def test_private_facade_access_guard_rejects_mutations(addition):
    with pytest.raises(AssertionError):
        _assert_no_private_facade_accesses({Path("tests/synthetic.py"): addition})


def test_signature_guard_rejects_a_mutated_default():
    class MutatedFacade:
        pass

    for name in PUBLIC_SIGNATURES:
        setattr(MutatedFacade, name, getattr(LinkedInExtractor, name))

    async def get_page_text(self, unexpected: bool = False) -> str:
        return ""

    setattr(MutatedFacade, "get_page_text", get_page_text)
    with pytest.raises(AssertionError):
        _assert_runtime_signatures(MutatedFacade)
