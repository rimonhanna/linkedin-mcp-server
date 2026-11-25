"""Public scraping facade and collaborator composition root."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import TYPE_CHECKING, Any

from patchright.async_api import Page

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.scraping.campaign_manager import CampaignManagerReader
from linkedin_mcp_server.scraping.campaign_manager_audiences import CampaignAudiences
from linkedin_mcp_server.scraping.campaign_manager_content import (
    CampaignContentEditor,
)
from linkedin_mcp_server.scraping.campaign_manager_editor import CampaignManagerEditor
from linkedin_mcp_server.scraping.campaign_manager_targeting import (
    CampaignTargetingEditor,
    TargetingAdd,
)
from linkedin_mcp_server.scraping.capture import SectionCapture
from linkedin_mcp_server.scraping.company import CompanyScraper
from linkedin_mcp_server.scraping.connection_actions import ConnectionActions
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.contracts import (
    ExtractedSection as ExtractedSection,
    FilterValidationError as FilterValidationError,
    rate_limited_section_error as rate_limited_section_error,
)
from linkedin_mcp_server.scraping.conversations import ConversationReader
from linkedin_mcp_server.scraping.facets import FacetResolver
from linkedin_mcp_server.scraping.feed import FeedScraper
from linkedin_mcp_server.scraping.job_pages import JobPageReader
from linkedin_mcp_server.scraping.job_policy import JobsTrackerStage
from linkedin_mcp_server.scraping.jobs import JobScraper
from linkedin_mcp_server.scraping.message_sender import MessageSender
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.page_invites import PageInviteActions
from linkedin_mcp_server.scraping.person import PersonScraper
from linkedin_mcp_server.scraping.posts import PostSearch
from linkedin_mcp_server.scraping.profile_page import ProfilePageReader
from linkedin_mcp_server.scraping.sales_navigator import SalesNavigatorReader
from linkedin_mcp_server.scraping.sales_navigator_inbox import (
    SalesNavigatorInboxReader,
)
from linkedin_mcp_server.scraping.sales_navigator_inbox_payload import InboxFilter
from linkedin_mcp_server.scraping.sales_navigator_sender import (
    SalesNavigatorMessageSender,
)
from linkedin_mcp_server.scraping.sales_navigator_list_writes import (
    SalesNavigatorListWriter,
)
from linkedin_mcp_server.scraping.session import ScrapingSession
from linkedin_mcp_server.scraping.text import (
    strip_conversation_chrome as strip_conversation_chrome,
    strip_linkedin_noise as strip_linkedin_noise,
)


if TYPE_CHECKING:
    from linkedin_mcp_server.callbacks import ProgressCallback


class LinkedInExtractor:
    """Compose scraping owners and expose the stable tool-facing API."""

    def __init__(self, page: Page):
        session = ScrapingSession(page)
        navigator = PageNavigator(session)
        content = PageContentReader(session)
        capture = SectionCapture(session, navigator, content)
        message_sender = MessageSender(session, navigator)
        profile_page = ProfilePageReader(
            session,
            lambda: message_sender._read_profile_message_target(),
        )
        facets = FacetResolver(session, navigator, capture)
        person = PersonScraper(session, navigator, capture, profile_page, facets)

        self._content = content
        self._capture = capture
        self._feed = FeedScraper(session, navigator, content)
        self._message_sender = message_sender
        self._person = person
        self._company = CompanyScraper(session, capture, facets)
        self._connection = ConnectionActions(
            session,
            navigator,
            lambda username: self.scrape_person(username, {"main_profile"}),
        )
        job_pages = JobPageReader(session, navigator, content)
        self._jobs = JobScraper(session, navigator, capture, job_pages, facets)
        self._posts = PostSearch(capture)
        self._conversations = ConversationReader(
            session, navigator, content, profile_page
        )
        self._campaigns = CampaignManagerReader(session, navigator)
        self._campaign_editor = CampaignManagerEditor(session, navigator)
        self._campaign_targeting = CampaignTargetingEditor(session, navigator)
        self._campaign_content = CampaignContentEditor(session, navigator)
        self._audiences = CampaignAudiences(session, navigator)
        self._sales_navigator = SalesNavigatorReader(session, navigator, facets)
        self._page_invites = PageInviteActions(session, navigator)
        self._sales_inbox = SalesNavigatorInboxReader(
            session,
            self._sales_navigator,
        )
        self._sales_navigator_sender = SalesNavigatorMessageSender(
            session, self._sales_navigator
        )
        self._sales_navigator_lists = SalesNavigatorListWriter(self._sales_navigator)

    async def get_page_text(self) -> str:
        """Extract innerText from the main content area of the current page."""
        return await self._content.get_page_text()

    async def click_button_by_text(
        self, text: str, *, scope: str = "main", timeout: int = 5000
    ) -> bool:
        """Click the first button or link whose visible text exactly matches."""
        return await self._content.click_button_by_text(
            text, scope=scope, timeout=timeout
        )

    async def extract_feed(self, num_posts: int = 10) -> ExtractedSection:
        """Scrape the LinkedIn home feed, scrolling until enough posts load."""
        return await self._feed.extract_feed(num_posts)

    async def extract_page(
        self,
        url: str,
        section_name: str,
        max_scrolls: int | None = None,
    ) -> ExtractedSection:
        """Navigate, scroll to load lazy content, and extract innerText."""
        return await self._capture.extract_page(url, section_name, max_scrolls)

    async def scrape_person(
        self,
        username: str,
        requested: set[str],
        callbacks: ProgressCallback | None = None,
        max_scrolls: int | None = None,
        *,
        main_profile_already_loaded: bool = False,
        allow_self_alias: bool = False,
    ) -> dict[str, Any]:
        """Scrape a person profile with configurable sections."""
        return await self._person.scrape_person(
            username,
            requested,
            callbacks,
            max_scrolls,
            main_profile_already_loaded=main_profile_already_loaded,
            allow_self_alias=allow_self_alias,
        )

    async def get_my_profile(
        self,
        sections: set[str] | None = None,
        callbacks: ProgressCallback | None = None,
        max_scrolls: int | None = None,
    ) -> dict[str, Any]:
        """Scrape the authenticated user's own LinkedIn profile."""
        return await self._person.get_my_profile(sections, callbacks, max_scrolls)

    async def connect_with_person(
        self,
        username: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Send a LinkedIn connection request or accept an incoming one."""
        return await self._connection.connect_with_person(username, note=note)

    async def withdraw_invitations(
        self,
        older_than_days: int,
        *,
        exclude_usernames: list[str] | None = None,
        max_withdrawals: int = 20,
        dry_run: bool = True,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Withdraw (or list, on a dry run) old pending sent invitations."""
        return await self._connection.withdraw_invitations(
            older_than_days,
            exclude_usernames=exclude_usernames,
            max_withdrawals=max_withdrawals,
            dry_run=dry_run,
            deadline=deadline,
        )

    async def get_sidebar_profiles(self, username: str) -> dict[str, Any]:
        """Extract profile links from sidebar sections on a profile page."""
        return await self._person.get_sidebar_profiles(username)

    async def scrape_company(
        self,
        company_name: str,
        requested: set[str],
        callbacks: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Scrape a company profile with configurable sections."""
        return await self._company.scrape_company(company_name, requested, callbacks)

    async def get_company_employees(
        self,
        company_name: str,
        keywords: str | None = None,
    ) -> dict[str, Any]:
        """List employees at a company from the people page."""
        return await self._company.get_company_employees(company_name, keywords)

    async def scrape_job(self, job_id: str) -> dict[str, Any]:
        """Scrape a single job posting."""
        return await self._jobs.scrape_job(job_id)

    async def search_jobs(
        self,
        keywords: str,
        location: str | None = None,
        max_pages: int = 3,
        date_posted: str | None = None,
        job_type: str | None = None,
        experience_level: str | None = None,
        work_type: str | None = None,
        easy_apply: bool = False,
        sort_by: str | None = None,
        tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Search for jobs with pagination and job ID extraction."""
        return await self._jobs.search_jobs(
            keywords,
            location,
            max_pages,
            date_posted,
            job_type,
            experience_level,
            work_type,
            easy_apply,
            sort_by,
            tool_timeout,
        )

    async def get_saved_jobs(
        self, max_pages: int = 3, stage: JobsTrackerStage = "saved"
    ) -> dict[str, Any]:
        """List the authenticated user's saved job postings."""
        return await self._jobs.get_saved_jobs(max_pages, stage)

    async def search_people(
        self,
        keywords: str | None = None,
        location: str | None = None,
        network: list[str] | None = None,
        current_company: str | list[str] | None = None,
        max_pages: int = 1,
        *,
        title: str | None = None,
        past_company: str | list[str] | None = None,
        industry: str | list[str] | None = None,
        school: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        profile_language: str | list[str] | None = None,
    ) -> dict[str, Any]:
        """Search for people and extract the results pages."""
        return await self._person.search_people(
            keywords,
            location=location,
            network=network,
            current_company=current_company,
            max_pages=max_pages,
            title=title,
            past_company=past_company,
            industry=industry,
            school=school,
            first_name=first_name,
            last_name=last_name,
            profile_language=profile_language,
        )

    async def search_companies(
        self,
        keywords: str | None = None,
        industry: list[str] | None = None,
        size: list[str] | None = None,
        hq_location: str | None = None,
        has_jobs: bool | None = None,
        max_pages: int = 1,
    ) -> dict[str, Any]:
        """Search for companies and extract the results pages."""
        return await self._company.search_companies(
            keywords,
            industry=industry,
            size=size,
            hq_location=hq_location,
            has_jobs=has_jobs,
            max_pages=max_pages,
        )

    async def search_posts(
        self,
        keywords: str,
        date_posted: str | None = None,
        max_posts: int = 10,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Search LinkedIn posts and extract the results page."""
        return await self._posts.search_posts(
            keywords,
            date_posted=date_posted,
            max_posts=max_posts,
            content_type=content_type,
        )

    async def get_inbox(
        self, limit: int = 20, callbacks: ProgressCallback | None = None
    ) -> dict[str, Any]:
        """List recent conversations from the messaging inbox."""
        return await self._conversations.get_inbox(limit, callbacks)

    async def get_conversation(
        self,
        linkedin_username: str | None = None,
        thread_id: str | None = None,
        index: int = 0,
    ) -> dict[str, Any]:
        """Read a specific messaging conversation by thread ID or username."""
        return await self._conversations.get_conversation(
            linkedin_username, thread_id, index
        )

    async def search_conversations(
        self, keywords: str, limit: int = 20
    ) -> dict[str, Any]:
        """Search messages by keyword."""
        return await self._conversations.search_conversations(keywords, limit)

    async def send_message(
        self,
        linkedin_username: str,
        message: str,
        *,
        confirm_send: bool,
        profile_urn: str | None = None,
    ) -> dict[str, Any]:
        """Compose and send a new message with explicit confirmation gating."""
        return await self._message_sender.send_message(
            linkedin_username,
            message,
            confirm_send=confirm_send,
            profile_urn=profile_urn,
        )

    async def reply_to_conversation(
        self,
        thread_id: str,
        message: str,
        *,
        confirm_send: bool,
    ) -> dict[str, Any]:
        """Reply inside an existing thread with explicit confirmation gating."""
        return await self._message_sender.reply_to_conversation(
            thread_id,
            message,
            confirm_send=confirm_send,
        )

    async def list_ad_accounts(self) -> dict[str, Any]:
        """List the Campaign Manager ad accounts the member can open."""
        return await self._campaigns.list_ad_accounts()

    async def list_campaigns(self, account_id: str) -> dict[str, Any]:
        """List an ad account's campaign groups and campaigns."""
        return await self._campaigns.list_campaigns(account_id)

    async def get_campaign(self, account_id: str, campaign_id: str) -> dict[str, Any]:
        """Read one campaign with its group, targeting and creatives."""
        return await self._campaigns.get_campaign(account_id, campaign_id)

    async def get_campaign_analytics(
        self,
        account_id: str,
        *,
        level: str = "campaign",
        start_date: date | None = None,
        end_date: date | None = None,
        demographics: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Read an ad account's performance analytics at one entity level."""
        return await self._campaigns.get_campaign_analytics(
            account_id,
            level=level,
            start_date=start_date,
            end_date=end_date,
            demographics=demographics,
        )

    async def update_campaign(
        self,
        account_id: str,
        campaign_id: str,
        *,
        daily_budget: str | None = None,
        lifetime_budget: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        confirm_send: bool,
        political_ad: bool | None = None,
    ) -> dict[str, Any]:
        """Change an ad set's budget amount or schedule dates."""
        return await self._campaign_editor.update_campaign(
            account_id,
            campaign_id,
            daily_budget=daily_budget,
            lifetime_budget=lifetime_budget,
            start_date=start_date,
            end_date=end_date,
            confirm_send=confirm_send,
            political_ad=political_ad,
        )

    async def set_campaign_status(
        self, account_id: str, campaign_id: str, *, status: str, confirm_send: bool
    ) -> dict[str, Any]:
        """Pause an active ad set or resume a paused one."""
        return await self._campaign_editor.set_campaign_status(
            account_id, campaign_id, status=status, confirm_send=confirm_send
        )

    async def create_campaign_draft(
        self,
        account_id: str,
        campaign_group_id: str,
        *,
        objective: str,
        name: str,
        confirm_send: bool,
        political_ad: bool | None = None,
    ) -> dict[str, Any]:
        """Create a draft ad set in an existing campaign group."""
        return await self._campaign_editor.create_campaign_draft(
            account_id,
            campaign_group_id,
            objective=objective,
            name=name,
            confirm_send=confirm_send,
            political_ad=political_ad,
        )

    async def search_targeting_entities(
        self, account_id: str, campaign_id: str, query: str
    ) -> dict[str, Any]:
        """Targeting values Campaign Manager offers for ``query`` on an ad set."""
        return await self._campaign_targeting.search_targeting_entities(
            account_id, campaign_id, query
        )

    async def update_campaign_targeting(
        self,
        account_id: str,
        campaign_id: str,
        *,
        add: Sequence[TargetingAdd] | None = None,
        remove: Sequence[str] | None = None,
        confirm_send: bool,
        deadline: float | None = None,
        political_ad: bool | None = None,
    ) -> dict[str, Any]:
        """Add and remove targeting values of one ad set."""
        return await self._campaign_targeting.update_campaign_targeting(
            account_id,
            campaign_id,
            add=add,
            remove=remove,
            confirm_send=confirm_send,
            deadline=deadline,
            political_ad=political_ad,
        )

    async def add_post_to_campaign(
        self, account_id: str, campaign_id: str, post_urn: str, *, confirm_send: bool
    ) -> dict[str, Any]:
        """Add a company post to a draft or paused ad set as an ad."""
        return await self._campaign_content.add_post_to_campaign(
            account_id, campaign_id, post_urn, confirm_send=confirm_send
        )

    async def list_sponsorable_posts(
        self, account_id: str, campaign_id: str
    ) -> dict[str, Any]:
        """List the company posts an ad set's content library offers."""
        return await self._campaign_content.list_sponsorable_posts(
            account_id, campaign_id
        )

    async def list_audiences(self, account_id: str) -> dict[str, Any]:
        """List an ad account's matched and saved audiences."""
        return await self._audiences.list_audiences(account_id)

    async def create_company_list_audience(
        self,
        account_id: str,
        name: str,
        companies: Sequence[Mapping[str, Any]],
        *,
        confirm_send: bool,
    ) -> dict[str, Any]:
        """Upload a company list as a matched audience of the ad account."""
        return await self._audiences.create_company_list_audience(
            account_id, name, companies, confirm_send=confirm_send
        )

    async def create_saved_audience(
        self, account_id: str, campaign_id: str, name: str, *, confirm_send: bool
    ) -> dict[str, Any]:
        """Save an ad set's targeting as a saved audience."""
        return await self._audiences.create_saved_audience(
            account_id, campaign_id, name, confirm_send=confirm_send
        )

    async def search_sales_navigator_accounts(
        self,
        *,
        keywords: str | None = None,
        industry: Sequence[str] | None = None,
        hq_location: Sequence[str] | None = None,
        size: Sequence[str] | None = None,
        headcount_growth_min: int | None = None,
        headcount_growth_max: int | None = None,
        page: int = 1,
    ) -> dict[str, Any]:
        """One page of Sales Navigator account search results."""
        return await self._sales_navigator.search_accounts(
            keywords=keywords,
            industry=industry,
            hq_location=hq_location,
            size=size,
            headcount_growth_min=headcount_growth_min,
            headcount_growth_max=headcount_growth_max,
            page=page,
        )

    async def search_sales_navigator_leads(
        self,
        *,
        keywords: str | None = None,
        title: Sequence[str] | None = None,
        seniority: Sequence[str] | None = None,
        current_company: Sequence[str] | None = None,
        location: Sequence[str] | None = None,
        industry: Sequence[str] | None = None,
        network: Sequence[str] | None = None,
        function: Sequence[str] | None = None,
        company_size: Sequence[str] | None = None,
        years_in_current_role: Sequence[str] | None = None,
        years_at_current_company: Sequence[str] | None = None,
        account_list: Sequence[str] | None = None,
        lead_list: Sequence[str] | None = None,
        exclude_lead_list: Sequence[str] | None = None,
        page: int = 1,
    ) -> dict[str, Any]:
        """One page of Sales Navigator lead search results."""
        return await self._sales_navigator.search_leads(
            keywords=keywords,
            title=title,
            seniority=seniority,
            current_company=current_company,
            location=location,
            industry=industry,
            network=network,
            function=function,
            company_size=company_size,
            years_in_current_role=years_in_current_role,
            years_at_current_company=years_at_current_company,
            account_list=account_list,
            lead_list=lead_list,
            exclude_lead_list=exclude_lead_list,
            page=page,
        )

    async def get_sales_navigator_account(self, company_id: str) -> dict[str, Any]:
        """One account's firmographics from its Sales Navigator page."""
        return await self._sales_navigator.get_account(company_id)

    async def list_sales_navigator_account_lists(
        self, page: int = 1, *, deadline: float | None = None
    ) -> dict[str, Any]:
        """The member's Sales Navigator account lists, up to 1,000 per call."""
        return await self._sales_navigator.list_account_lists(page, deadline=deadline)

    async def get_sales_navigator_account_list(
        self, list_id: str, page: int = 1, *, deadline: float | None = None
    ) -> dict[str, Any]:
        """One Sales Navigator account list and up to 1,000 of its accounts."""
        return await self._sales_navigator.get_account_list(
            list_id, page, deadline=deadline
        )

    async def list_sales_navigator_lead_lists(
        self, page: int = 1, *, deadline: float | None = None
    ) -> dict[str, Any]:
        """The member's Sales Navigator lead lists, up to 1,000 per call."""
        return await self._sales_navigator.list_lead_lists(page, deadline=deadline)

    async def get_sales_navigator_lead_list(
        self, list_id: str, page: int = 1, *, deadline: float | None = None
    ) -> dict[str, Any]:
        """The leads of one Sales Navigator lead list, up to 1,000 per call."""
        return await self._sales_navigator.get_lead_list(
            list_id, page, deadline=deadline
        )

    async def list_page_follow_candidates(
        self,
        company_id: str,
        *,
        keywords: str | None = None,
        limit: int = 20,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Connections a page admin can invite to follow the page, and its credits."""
        return await self._page_invites.list_follow_candidates(
            company_id, keywords=keywords, limit=limit, deadline=deadline
        )

    async def invite_to_follow_page(
        self,
        company_id: str,
        people: list[str],
        *,
        confirm_send: bool,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Invite exactly these connections to follow the page (dry run unless confirmed)."""
        return await self._page_invites.invite_to_follow(
            company_id, people, confirm_send=confirm_send, deadline=deadline
        )

    async def get_sales_navigator_inbox(
        self,
        limit: int = 20,
        filter: InboxFilter = "all",
        *,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """The Sales Navigator inbox's most recent conversations in one view."""
        return await self._sales_inbox.get_inbox(limit, filter, deadline=deadline)

    async def get_sales_navigator_conversation(
        self,
        thread_id: str | None = None,
        lead_id: str | None = None,
        index: int = 0,
        *,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """One Sales Navigator conversation, by thread id or lead id."""
        return await self._sales_inbox.get_conversation(
            thread_id, lead_id, index, deadline=deadline
        )

    async def search_sales_navigator_conversations(
        self, keywords: str, limit: int = 20, *, deadline: float | None = None
    ) -> dict[str, Any]:
        """Sales Navigator conversations the inbox search finds for keywords."""
        return await self._sales_inbox.search_conversations(
            keywords, limit, deadline=deadline
        )

    async def send_sales_navigator_message(
        self,
        lead_id: str,
        message: str,
        *,
        confirm_send: bool,
        subject: str | None = None,
        spend_inmail_credit: bool = False,
    ) -> dict[str, Any]:
        """Send a new Sales Navigator message to a lead, gated by confirmation."""
        return await self._sales_navigator_sender.send_message(
            lead_id,
            message,
            confirm_send=confirm_send,
            subject=subject,
            spend_inmail_credit=spend_inmail_credit,
        )

    async def reply_to_sales_navigator_conversation(
        self,
        thread_id: str,
        message: str,
        *,
        confirm_send: bool,
        spend_inmail_credit: bool = False,
    ) -> dict[str, Any]:
        """Reply inside a Sales Navigator conversation, gated by confirmation."""
        return await self._sales_navigator_sender.reply_to_conversation(
            thread_id,
            message,
            confirm_send=confirm_send,
            spend_inmail_credit=spend_inmail_credit,
        )

    async def create_sales_navigator_account_list(
        self,
        name: str,
        *,
        description: str | None = None,
        accounts: Sequence[Mapping[str, Any]] = (),
        confirm_send: bool,
    ) -> dict[str, Any]:
        """Create a Sales Navigator account list, optionally with accounts.

        ``accounts`` are ``plan_accounts`` rows, kept in the result as given.
        """
        return await self._sales_navigator_lists.create_account_list(
            name, description=description, accounts=accounts, confirm_send=confirm_send
        )

    async def add_accounts_to_sales_navigator_list(
        self,
        list_id: str,
        accounts: Sequence[Mapping[str, Any]],
        *,
        confirm_send: bool,
    ) -> dict[str, Any]:
        """Add accounts (``plan_accounts`` rows) to one account list."""
        return await self._sales_navigator_lists.add_accounts(
            list_id, accounts, confirm_send=confirm_send
        )

    async def delete_sales_navigator_account_list(
        self, list_id: str, *, expected_name: str, confirm_send: bool
    ) -> dict[str, Any]:
        """Delete one Sales Navigator account list whose name is expected_name."""
        return await self._sales_navigator_lists.delete_account_list(
            list_id, expected_name=expected_name, confirm_send=confirm_send
        )
