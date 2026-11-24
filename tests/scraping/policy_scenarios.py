"""Canonical semantic scraping-policy scenarios."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, asynccontextmanager, contextmanager
from datetime import date
from difflib import unified_diff
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import asyncio
import inspect
import json
import tempfile

from patchright.async_api import Page
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.callbacks import ProgressCallback
from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.pacing import JobStore
from linkedin_mcp_server.scraping import campaign_manager_editor as cm_editor_module
from linkedin_mcp_server.scraping import (
    campaign_manager_targeting as cm_targeting_module,
)
from linkedin_mcp_server.scraping import capture as capture_module
from linkedin_mcp_server.scraping import company as company_module
from linkedin_mcp_server.scraping import conversations as conversations_module
from linkedin_mcp_server.scraping import feed as feed_module
from linkedin_mcp_server.scraping import job_pages as job_pages_module
from linkedin_mcp_server.scraping import jobs as jobs_module
from linkedin_mcp_server.scraping import navigation as navigation_module
from linkedin_mcp_server.scraping import person as person_module
from linkedin_mcp_server.scraping import session as session_module
from linkedin_mcp_server.scraping.messaging_api import MessagingApiCapture
from linkedin_mcp_server.scraping.sales_navigator_lead_filters import LEAD_LIST
from linkedin_mcp_server.scraping.sales_navigator_payload import (
    COMPANY_HEADCOUNT,
    INDUSTRY,
    REGION,
    SENIORITY_LEVEL,
    SearchFilter,
    build_search_query,
)
from linkedin_mcp_server.scraping import LinkedInExtractor
from linkedin_mcp_server.scraping.fields import COMPANY_SECTIONS, PERSON_SECTIONS
from linkedin_mcp_server.server import create_mcp_server

from .support.policy_trace import (
    FakeClock,
    ScriptedCmRequest,
    ScriptedCmResponse,
    ScriptedPage,
    ScriptedResponse,
    TraceRecorder,
    bind_effective,
)


ROOT = Path(__file__).parents[2]
TRACE_ROOT = ROOT / "tests" / "fixtures" / "scraping-policy" / "v1"
_TOOL_SCHEMAS: dict[str, dict[str, Any]] | None = None

_COMMON_ALLOWED = {
    "boundary.auth",
    "boundary.auth_quick",
    "boundary.drain",
    "boundary.modal",
    "boundary.rate_limit",
    "boundary.scroll_body",
    "boundary.scroll_sidebar",
    "boundary.stabilize",
    "boundary.trace",
    "callback.complete",
    "callback.progress",
    "callback.start",
    "evaluate",
    "evaluate_handle",
    "handle.as_element",
    "handle.dispose",
    "handle.evaluate",
    "handle.json_value",
    "keyboard.down",
    "keyboard.insert_text",
    "keyboard.press",
    "keyboard.type",
    "keyboard.up",
    "listener.add",
    "listener.emit",
    "listener.remove",
    "locator.click",
    "locator.count",
    "locator.create",
    "locator.derive",
    "locator.is_visible",
    "locator.scroll_into_view",
    "locator.wait_for",
    "mouse.move",
    "mouse.wheel",
    "navigate",
    "route.add",
    "route.remove",
    "sleep",
    "wait_for_function",
    "wait_for_load_state",
    "wait_for_selector",
}


class TraceCallbacks(ProgressCallback):
    """Record progress callbacks without a mock object."""

    def __init__(self, recorder: TraceRecorder):
        self.recorder = recorder

    async def on_start(self, scraper_type: str, url: str) -> None:
        self.recorder.record("callback.start", operation=scraper_type, url=url)

    async def on_progress(self, message: str, percent: int) -> None:
        self.recorder.record("callback.progress", message=message, percent=percent)

    async def on_complete(self, scraper_type: str, result: Any) -> None:
        self.recorder.record(
            "callback.complete", operation=scraper_type, result_url=result["url"]
        )


@contextmanager
def _diagnostics_bindings(diagnostics: Any) -> Iterator[None]:
    """Bind the issue-report boundary in each exercised scraping module.

    A separate context manager rather than more items in `boundaries`, which
    sat on exactly 20 and is the whole of CPython's static block budget inside
    an async generator: the twenty-first raised `SyntaxError: too many
    statically nested blocks` at import, before any test ran. Each module
    holds its own name for the function, so patching fewer than all of them
    lets the real one write an issue report for a scripted page.
    """

    with (
        patch.object(capture_module, "build_issue_diagnostics", diagnostics),
        patch.object(feed_module, "build_issue_diagnostics", diagnostics),
        patch.object(person_module, "build_issue_diagnostics", diagnostics),
        patch.object(company_module, "build_issue_diagnostics", diagnostics),
        patch.object(job_pages_module, "build_issue_diagnostics", diagnostics),
        patch.object(jobs_module, "build_issue_diagnostics", diagnostics),
    ):
        yield


@contextmanager
def _navigation_bindings(**boundaries: Any) -> Iterator[None]:
    """Bind the navigator's boundaries, and charge its loads to a throwaway
    ledger.

    One context for the whole module because ``boundaries()`` below sits at
    the interpreter's limit of statically nested blocks, and each ``with``
    item is one. The ledger is isolated here rather than left to the autouse
    fixture because this runs outside pytest too
    (`scripts/check_scraping_policy_traces.py`), where nothing else keeps the
    charges out of the home directory -- or keeps a cap the real ledger has
    reached from refusing a scripted page.
    """
    with ExitStack() as stack:
        root = stack.enter_context(tempfile.TemporaryDirectory())
        stack.enter_context(
            patch.object(navigation_module, "JobStore", lambda *a, **kw: JobStore(root))
        )
        for name, binding in boundaries.items():
            stack.enter_context(patch.object(navigation_module, name, binding))
        yield


@asynccontextmanager
async def boundaries(
    recorder: TraceRecorder,
    clock: FakeClock,
    *,
    auth_result: str | None = None,
) -> AsyncIterator[None]:
    real_scroll_body = session_module.scroll_to_bottom
    real_scroll_sidebar = session_module.scroll_job_sidebar
    real_drain = feed_module.FeedScraper._drain_listener_tasks

    async def trace(_page: Any, label: str, *, extra: Any = None) -> None:
        recorder.record("boundary.trace", label=label, extra=extra)

    async def auth_quick(_page: Any) -> None:
        recorder.record("boundary.auth_quick", result=None)
        return None

    async def auth(_page: Any) -> str | None:
        recorder.record("boundary.auth", result=auth_result)
        return auth_result

    async def confirmed(_page: Any, barrier: str, *, detect: Any = None) -> bool:
        # The second look at a barrier is a /feed/ load of its own; here it
        # stands, so a detected barrier propagates the way it always did.
        recorder.record("boundary.barrier_confirmed", barrier=barrier, result=True)
        return True

    async def remember(_page: Any) -> bool:
        return False

    async def stabilize(description: str, _logger: Any) -> None:
        recorder.record(
            "boundary.stabilize",
            description=description,
            result=None,
        )

    async def rate_limit(_page: Any) -> None:
        recorder.record("boundary.rate_limit")

    async def modal(_page: Any) -> bool:
        recorder.record("boundary.modal", dismissed=False)
        return False

    async def scroll_body(*args: Any, **kwargs: Any) -> None:
        values = bind_effective(real_scroll_body, *args, **kwargs)
        values.pop("page")
        recorder.record("boundary.scroll_body", **values, actual_scrolls=2)
        clock.advance(values["pause_time"] * 2)

    async def scroll_sidebar(*args: Any, **kwargs: Any) -> bool:
        values = bind_effective(real_scroll_sidebar, *args, **kwargs)
        values.pop("page")
        recorder.record(
            "boundary.scroll_sidebar", **values, actual_scrolls=2, moved=False
        )
        clock.advance(0.4)
        return False

    async def drain(tasks: list[Any]) -> None:
        # The real drain is what runs; the event only marks where it happens,
        # so swapping it with listener removal shows up as a reordered trace.
        recorder.record("boundary.drain", pending=len(tasks))
        await real_drain(tasks)

    def diagnostics(error: Exception, **values: Any) -> dict[str, Any]:
        return {
            "error_type": type(error).__name__,
            "error_message": str(error),
            "context": values.get("context"),
        }

    def jitter(base: float, spread: float = 0.5) -> float:
        # Identity, so every paced sleep records its base length: the real
        # jitter draws from an unseeded RNG and no two runs would agree.
        return base

    async def humanize(_page: Any) -> None:
        # The cursor entropy after each load is random in hop count, position
        # and pause; recording it would make every navigation trace differ.
        return None

    with (
        _navigation_bindings(
            record_page_trace=trace,
            detect_auth_barrier_quick=auth_quick,
            detect_auth_barrier=auth,
            barrier_confirmed=confirmed,
            resolve_remember_me_prompt=remember,
            stabilize_navigation=stabilize,
            humanize_after_nav=humanize,
        ),
        # Every binding of each shared boundary, because the workflows that
        # reach it are split across the modules mid-relocation: generic
        # capture, the feed, conversation reader and message sender go through
        # `ScrapingSession`, while the job pages import the helper into
        # `job_pages`. Patching one side only lets the real helper loose on a
        # scripted page. The scrolls have no facade binding left at all — the job
        # reader held the last one — and neither has the modal close, whose
        # last facade caller left with the conversation reader.
        patch.object(session_module, "detect_rate_limit", rate_limit),
        patch.object(job_pages_module, "detect_rate_limit", rate_limit),
        patch.object(session_module, "handle_modal_close", modal),
        patch.object(job_pages_module, "handle_modal_close", modal),
        patch.object(session_module, "scroll_to_bottom", scroll_body),
        patch.object(job_pages_module, "scroll_to_bottom", scroll_body),
        patch.object(session_module, "scroll_job_sidebar", scroll_sidebar),
        patch.object(job_pages_module, "scroll_job_sidebar", scroll_sidebar),
        _diagnostics_bindings(diagnostics),
        # `staticmethod`, or the class attribute would bind `self` in front of
        # the pending list and the replacement would never match the call.
        patch.object(
            feed_module.FeedScraper,
            "_drain_listener_tasks",
            staticmethod(drain),
        ),
        # The frozen clock never fires a real timeout, so the wait for a next
        # inbox page that never comes would hang; report it as the list's end.
        patch.object(
            MessagingApiCapture,
            "wait_for_next_payload",
            AsyncMock(return_value=False),
        ),
        patch.object(session_module, "jitter", jitter),
        patch.object(session_module.asyncio, "sleep", clock.sleep),
        patch.object(session_module.time, "monotonic", clock.monotonic),
    ):
        yield


def _page(recorder: TraceRecorder, *, url: str = "about:blank") -> ScriptedPage:
    return ScriptedPage(recorder, url=url).script("evaluate:root_content")


def _extractor(page: ScriptedPage) -> LinkedInExtractor:
    return LinkedInExtractor(cast(Page, page))


def _complete_mapping_result(result: dict[str, Any], **derived: Any) -> dict[str, Any]:
    overlap = result.keys() & derived.keys()
    if overlap:
        raise AssertionError(
            f"derived result fields overlap raw result: {sorted(overlap)}"
        )
    return {**result, **derived}


def _root(text: str, references: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "source": "root",
        "text": text,
        "references": references or [],
    }


async def _generic_capture_scenario(
    name: str, url: str, *, max_scrolls: int | None = None
) -> dict[str, Any]:
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder).script("evaluate:root_content", _root("Policy content"))
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("extract_page", "section"):
            result = await extractor.extract_page(
                url, "section", max_scrolls=max_scrolls
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "extract_page",
            "arguments": {
                "url": url,
                "section_name": "section",
                "max_scrolls": max_scrolls,
            },
        },
        {"text": result.text, "references": result.references},
    )


async def _person_sections_scenario() -> dict[str, Any]:
    name = "scrape_person__all_sections"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    # 13 valid company anchors against the documented cap of 12 for a section,
    # so dropping the cap shows up as a fourteenth reference in the trace.
    overflowing = [
        {
            "href": f"https://www.linkedin.com/company/policy-employer-{index}/",
            "text": f"Employer {index}",
        }
        for index in range(13)
    ]
    roots = [
        _root(
            f"{section} content",
            overflowing if section == "experience" else None,
        )
        for section in PERSON_SECTIONS
    ]
    page.script("evaluate:root_content", *roots)
    _script_profile_target(page)
    page.declare_locator("main button", "show_more")
    page.declare_derived(
        "show_more",
        "filter:^Show (more|all)\\b/re.IGNORECASE|re.UNICODE",
        "show_more.filtered",
    )
    page.script("show_more.filtered.count", *([0] * 9))
    extractor = _extractor(page)
    callbacks = TraceCallbacks(recorder)
    async with boundaries(recorder, clock):
        with recorder.context("scrape_person"):
            result = await extractor.scrape_person(
                "ada-lovelace", set(PERSON_SECTIONS), callbacks=callbacks
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "scrape_person",
            "arguments": {
                "username": "ada-lovelace",
                "requested": list(PERSON_SECTIONS),
            },
        },
        _complete_mapping_result(result, section_names=list(result["sections"])),
    )


async def _company_sections_scenario() -> dict[str, Any]:
    name = "scrape_company__all_sections"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder).script(
        "evaluate:root_content",
        *[_root(f"{section} content") for section in COMPANY_SECTIONS],
    )
    extractor = _extractor(page)
    callbacks = TraceCallbacks(recorder)
    async with boundaries(recorder, clock):
        with recorder.context("scrape_company"):
            result = await extractor.scrape_company(
                "analytical-engine", set(COMPANY_SECTIONS), callbacks=callbacks
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "scrape_company",
            "arguments": {
                "company_name": "analytical-engine",
                "requested": list(COMPANY_SECTIONS),
            },
        },
        _complete_mapping_result(result, section_names=list(result["sections"])),
    )


async def _job_search_scenario(route: str = "/jobs/search/") -> dict[str, Any]:
    is_alias = "search-results" in route
    name = "search_jobs__route_alias" if is_alias else "search_jobs__baseline"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    page.goto_landings.append(f"https://www.linkedin.com{route}?keywords=python")
    ids = ["101", "102"] if is_alias else [str(101 + index) for index in range(16)]
    references = [
        {
            "href": "https://www.linkedin.com/jobs/view/101/",
            "text": "Senior policy engineer",
            "heading": "",
        },
        *[
            {
                "href": f"https://www.linkedin.com/company/company-{index}/",
                "text": f"Company {index}",
                "heading": "",
            }
            for index in range(20)
        ],
    ]
    page.script("evaluate:root_content", _root("Python jobs\n500+ results", references))
    page.script("evaluate:job_total_pages", None)
    page.script("evaluate:job_ids", {"ids": ids, "scoped": True})
    page.script("evaluate:job_promoted_ids", ["102"])
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("search_jobs", "search_results"):
            result = await extractor.search_jobs("python", max_pages=1)
    page.assert_clean()
    return recorder.trace(
        {"method": "search_jobs", "arguments": {"keywords": "python", "max_pages": 1}},
        result,
    )


async def _job_search_upgrade_scenario() -> dict[str, Any]:
    recorder = TraceRecorder(
        "search_jobs__stopping_page_metadata_upgrade", _COMMON_ALLOWED
    )
    clock = FakeClock(recorder)
    page = _page(recorder)
    first = [
        {
            "href": "https://www.linkedin.com/jobs/view/101/",
            "text": "Job",
            "heading": "",
        }
    ]
    second = [
        {
            "href": "https://www.linkedin.com/jobs/view/101/",
            "text": "Senior policy engineer with richer stopping-page metadata",
            "heading": "",
        }
    ]
    page.script(
        "evaluate:root_content",
        _root("First page", first),
        _root("Stopping page", second),
    )
    page.script("evaluate:job_total_pages", None)
    page.script(
        "evaluate:job_ids",
        {"ids": ["101"], "scoped": True},
        {"ids": ["101"], "scoped": True},
    )
    # Read for the first page only: the stopping page has no new ids to flag.
    page.script("evaluate:job_promoted_ids", [])
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("search_jobs", "search_results"):
            result = await extractor.search_jobs("python", max_pages=2)
    page.assert_clean()
    return recorder.trace(
        {"method": "search_jobs", "arguments": {"keywords": "python", "max_pages": 2}},
        result,
    )


async def _saved_jobs_scenario() -> dict[str, Any]:
    name = "get_saved_jobs__redirect_caps_and_upgrade"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    page.goto_landings.append("https://www.linkedin.com/jobs-tracker/")
    first_references = [
        *[
            {
                "href": f"https://www.linkedin.com/jobs/view/{100 + index}/",
                "text": f"Job {100 + index}",
                "heading": "",
            }
            for index in range(12)
        ],
        {
            "href": "https://www.linkedin.com/company/cap-boundary/",
            "text": "Cap boundary company",
            "heading": "",
        },
        *[
            {
                "href": f"https://www.linkedin.com/jobs/view/{113 + index}/",
                "text": f"Job {113 + index}",
                "heading": "",
            }
            for index in range(7)
        ],
    ]
    second_references = [
        {
            "href": "https://www.linkedin.com/jobs/view/100/",
            "text": "Senior policy engineer with richer duplicate metadata",
            "heading": "",
        },
        *[
            {
                "href": f"https://www.linkedin.com/jobs/view/{112 + index}/",
                "text": f"Job {112 + index}",
                "heading": "",
            }
            for index in range(19)
        ],
    ]
    page.script(
        "evaluate:root_content",
        _root("Saved jobs page one", first_references),
        _root("Saved jobs page two", second_references),
    )
    page.script("evaluate:saved_job_total_pages", 2)
    page.script(
        "evaluate:job_ids",
        {"ids": [str(100 + index) for index in range(10)], "scoped": False},
        {"ids": [str(110 + index) for index in range(10)], "scoped": False},
    )
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("get_saved_jobs", "saved_jobs"):
            result = await extractor.get_saved_jobs(max_pages=2)
    page.assert_clean()
    references = result.get("references", {}).get("saved_jobs", [])
    return recorder.trace(
        {"method": "get_saved_jobs", "arguments": {"max_pages": 2}},
        _complete_mapping_result(result, reference_count=len(references)),
    )


async def _feed_stale_scenario() -> dict[str, Any]:
    name = "extract_feed__stale_stop"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder).script("evaluate:root_content", _root("Feed content"))
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("extract_feed", "feed"):
            result = await extractor.extract_feed(num_posts=10)
    page.assert_clean()
    return recorder.trace(
        {"method": "extract_feed", "arguments": {"num_posts": 10}},
        {"references": result.references, "text": result.text},
    )


async def _feed_response_scenario(*, body_failure: bool) -> dict[str, Any]:
    suffix = "body_failure" if body_failure else "body_success"
    recorder = TraceRecorder(
        f"extract_feed__{suffix}",
        _COMMON_ALLOWED | {"response.body.start", "response.body.finish"},
    )
    clock = FakeClock(recorder)
    page = _page(recorder).script("evaluate:root_content", _root("Feed content"))
    body: bytes | BaseException
    if body_failure:
        body = RuntimeError("response body unavailable")
    else:
        body = (
            b'{"postSlugUrl":"https://www.linkedin.com/posts/'
            b'policy-ugcPost-123-example"}'
        )
    response = ScriptedResponse(
        recorder,
        "https://www.linkedin.com/feed/",
        body,
    )
    page.script("mouse.wheel", lambda: page.emit("response", response))
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("extract_feed", "feed"):
            result = await extractor.extract_feed(num_posts=1)
    page.assert_clean()
    return recorder.trace(
        {"method": "extract_feed", "arguments": {"num_posts": 1}},
        {"references": result.references, "text": result.text},
    )


_MESSAGE_PROFILE_URL = "https://www.linkedin.com/in/ada-lovelace/"
_MESSAGE_COMPOSE_URL = (
    "https://www.linkedin.com/messaging/compose/"
    "?recipient=ACoAA-policy&profileUrn=urn%3Ali%3Afsd_profile%3AACoAA-policy"
)
_MESSAGE_ROUTE = "https://www.linkedin.com/messaging/thread/2-policy-thread==/"
_MESSAGE_TARGET = {
    "profilePath": "/in/ada-lovelace/",
    "profileUrn": "ACoAA-policy",
}
_VALID_COMPOSER = {
    "status": "valid",
    "active": False,
    "empty": True,
    "submitCount": 1,
    "submitUsable": True,
}


def _profile_target(
    status: str = "resolved", *, profile_urn: str = "ACoAA-policy"
) -> dict[str, Any]:
    if status == "resolved":
        compose_url = (
            _MESSAGE_COMPOSE_URL
            if profile_urn == "ACoAA-policy"
            else "https://www.linkedin.com/messaging/compose/?recipient=" + profile_urn
        )
        return {
            "status": "resolved",
            "pageUrl": _MESSAGE_PROFILE_URL,
            "displayName": "Ada Lovelace",
            "composeHrefs": [compose_url],
        }
    if status == "unavailable":
        return {"status": "unavailable", "pageUrl": _MESSAGE_PROFILE_URL}
    return {"status": "unresolved"}


def _script_profile_target(
    page: ScriptedPage,
    status: str = "resolved",
    *,
    profile_urn: str = "ACoAA-policy",
) -> None:
    page.script(
        "wait_for_function:profile_message_target_ready",
        None
        if status == "resolved"
        else PlaywrightTimeoutError("profile Message action did not resolve"),
    )
    page.script(
        "evaluate:profile_message_target",
        _profile_target(status, profile_urn=profile_urn),
    )


def _script_message_surface(
    page: ScriptedPage,
    *,
    states: tuple[dict[str, Any], ...],
    route: str = _MESSAGE_ROUTE,
) -> None:
    page.goto_landings.append(_MESSAGE_PROFILE_URL)
    page.goto_landings.append(route)
    _script_profile_target(page)
    page.script("wait_for_function:message_composer_ready", None)
    page.script("evaluate:message_composer_state", *states)


def _script_message_owner(page: ScriptedPage, *, write: str = "written") -> None:
    page.script("evaluate_handle:message_composer_owner", True)
    page.script(
        "handle-1.evaluate:message_composer_write",
        "occupied" if write == "occupied" else "ready",
    )
    if write != "occupied":
        page.script("handle-1.evaluate:message_composer_focus", True)
        page.script("handle-1.evaluate:message_composer_settle", write)
    page.script("handle-1.evaluate:message_composer_dispose", None)


def _script_confirmation_cleanup(page: ScriptedPage) -> None:
    page.script("evaluate:message_confirmation_dispose", None)


def _script_owned_text_cleanup(page: ScriptedPage, *, removed: bool) -> None:
    page.script("handle-1.evaluate:message_composer_cleanup", removed)


async def _message_target_scenario(status: str) -> dict[str, Any]:
    recorder = TraceRecorder(f"send_message__target_{status}", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    page.goto_landings.append(_MESSAGE_PROFILE_URL)
    _script_profile_target(page, status)
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("send_message", "message"):
            result = await extractor.send_message(
                "ada-lovelace",
                "New text",
                confirm_send=True,
                profile_urn="ACoAA-policy",
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "send_message",
            "arguments": {"target_resolution": status, "confirm_send": True},
        },
        result,
    )


async def _messaging_dry_run_scenario() -> dict[str, Any]:
    recorder = TraceRecorder("send_message__dry_run", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    _script_message_surface(page, states=(_VALID_COMPOSER,))
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("send_message", "message"):
            result = await extractor.send_message(
                "ada-lovelace",
                "New text",
                confirm_send=False,
                profile_urn="ACoAA-policy",
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "send_message",
            "arguments": {
                "linkedin_username": "ada-lovelace",
                "message": "New text",
                "confirm_send": False,
                "profile_urn": "ACoAA-policy",
            },
        },
        result,
    )


async def _occupied_message_scenario(*, restored_during_write: bool) -> dict[str, Any]:
    suffix = "restored_during_write" if restored_during_write else "existing_draft"
    recorder = TraceRecorder(f"send_message__{suffix}", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    second_state = (
        _VALID_COMPOSER
        if restored_during_write
        else {**_VALID_COMPOSER, "empty": False}
    )
    _script_message_surface(page, states=(_VALID_COMPOSER, second_state))
    if restored_during_write:
        _script_message_owner(page, write="occupied")
        _script_owned_text_cleanup(page, removed=False)
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("send_message", "message"):
            result = await extractor.send_message(
                "ada-lovelace", "New text", confirm_send=True
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "send_message",
            "arguments": {
                "confirm_send": True,
                "composer_occupied": suffix,
            },
        },
        result,
    )


async def _messaging_submission_scenario(outcome: str) -> dict[str, Any]:
    recorder = TraceRecorder(f"send_message__{outcome}", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    _script_message_surface(page, states=(_VALID_COMPOSER, _VALID_COMPOSER))
    _script_message_owner(page)

    if outcome == "pre_submit_cleanup":
        page.script("handle-1.evaluate:message_submit_ready", "invalid")
        _script_owned_text_cleanup(page, removed=True)
    else:
        ready_states = ("disabled", "ready") if outcome == "sent" else ("ready",)
        page.script("handle-1.evaluate:message_submit_ready", *ready_states)
        page.script("evaluate:message_confirmation_prepare", "confirmation-1")
        if outcome == "submission_rejected":
            page.script("handle-1.evaluate:message_submit", "invalid")
            _script_confirmation_cleanup(page)
            _script_owned_text_cleanup(page, removed=True)
        elif outcome == "submission_interrupted":
            page.script(
                "handle-1.evaluate:message_submit",
                RuntimeError("submission round trip interrupted"),
            )
            _script_confirmation_cleanup(page)
        else:
            page.script("handle-1.evaluate:message_submit", "clicked")
            page.script(
                "wait_for_function:message_confirmation_ready",
                {"path": "/messaging/thread/2-policy-thread==/"}
                if outcome == "sent"
                else PlaywrightTimeoutError("same-node transition not observed"),
            )
            _script_confirmation_cleanup(page)

    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("send_message", "message"):
            result = await extractor.send_message(
                "ada-lovelace", "New text", confirm_send=True
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "send_message",
            "arguments": {"confirm_send": True, "submission_outcome": outcome},
        },
        result,
    )


async def _messaging_cancellation_scenario() -> dict[str, Any]:
    recorder = TraceRecorder("send_message__confirmation_cancelled", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    _script_message_surface(page, states=(_VALID_COMPOSER, _VALID_COMPOSER))
    _script_message_owner(page)
    page.script("handle-1.evaluate:message_submit_ready", "ready")
    page.script("evaluate:message_confirmation_prepare", "confirmation-1")
    page.script("handle-1.evaluate:message_submit", "clicked")
    page.script(
        "wait_for_function:message_confirmation_ready", asyncio.CancelledError()
    )
    _script_confirmation_cleanup(page)
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("send_message", "message"):
            try:
                await extractor.send_message(
                    "ada-lovelace", "New text", confirm_send=True
                )
            except asyncio.CancelledError:
                result = {"raised": "CancelledError"}
            else:
                raise AssertionError("message confirmation cancellation was swallowed")
    page.assert_clean()
    return recorder.trace(
        {
            "method": "send_message",
            "arguments": {"confirm_send": True, "cancelled_during": "confirmation"},
        },
        result,
    )


async def _invalid_message_scenario(message: str, label: str) -> dict[str, Any]:
    recorder = TraceRecorder(f"send_message__invalid_{label}", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("send_message", "message"):
            result = await extractor.send_message(
                "ada-lovelace",
                message,
                confirm_send=True,
                profile_urn="ACoAA-policy",
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "send_message",
            "arguments": {
                "linkedin_username": "ada-lovelace",
                "message_case": label,
                "confirm_send": True,
            },
        },
        result,
    )


_REPLY_THREAD_ROUTE = "https://www.linkedin.com/messaging/thread/2-policy-thread==/"


async def _reply_scenario(outcome: str) -> dict[str, Any]:
    recorder = TraceRecorder(f"reply_to_conversation__{outcome}", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    if outcome == "other_thread":
        page.goto_landings.append(
            "https://www.linkedin.com/messaging/thread/2-other-thread/"
        )
    else:
        page.goto_landings.append(_REPLY_THREAD_ROUTE)
        page.script("wait_for_function:message_composer_ready", None)
        states = (_VALID_COMPOSER,) * (1 if outcome == "dry_run" else 2)
        page.script("evaluate:message_composer_state", *states)
        page.script("evaluate:thread_participant", "Ada Lovelace")
    if outcome == "sent":
        _script_message_owner(page)
        page.script("handle-1.evaluate:message_submit_ready", "ready")
        page.script("evaluate:message_confirmation_prepare", "confirmation-1")
        page.script("handle-1.evaluate:message_submit", "clicked")
        page.script(
            "wait_for_function:message_confirmation_ready",
            {"path": "/messaging/thread/2-policy-thread==/"},
        )
        _script_confirmation_cleanup(page)
    extractor = _extractor(page)
    arguments = {
        "thread_id": "2-policy-thread==",
        "message": "New text",
        "confirm_send": outcome == "sent",
    }
    async with boundaries(recorder, clock):
        with recorder.context("reply_to_conversation", "message"):
            result = await extractor.reply_to_conversation(
                arguments["thread_id"],
                arguments["message"],
                confirm_send=arguments["confirm_send"],
            )
    page.assert_clean(retained_read_guard=True)
    return recorder.trace(
        {"method": "reply_to_conversation", "arguments": arguments}, result
    )


async def _single_capture_facade_scenario(method: str) -> dict[str, Any]:
    name = f"{method}__baseline"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    # A posting is only whole with its description heading; the other
    # facades accept any text.
    text = (
        "About the job\nResult content" if method == "scrape_job" else "Result content"
    )
    page = _page(recorder).script("evaluate:root_content", _root(text))
    extractor = _extractor(page)
    arguments: dict[str, Any]
    async with boundaries(recorder, clock):
        with recorder.context(method):
            if method == "get_company_employees":
                arguments = {"company_name": "analytical-engine", "keywords": "math"}
                result = await extractor.get_company_employees(**arguments)
            elif method == "scrape_job":
                page.script(
                    "evaluate:job_apply_signals",
                    {"easy_apply": True, "external_link": None},
                )
                arguments = {"job_id": "123"}
                result = await extractor.scrape_job(**arguments)
            elif method == "search_people":
                arguments = {"keywords": "analyst", "network": ["F"]}
                result = await extractor.search_people(**arguments)
            elif method == "search_companies":
                arguments = {"keywords": "engine"}
                result = await extractor.search_companies(**arguments)
            elif method == "search_posts":
                # One card before the wheel, two after: the loop reaches
                # `max_posts` on its first round and stops.
                page.script("evaluate:content_search_count", 1, 2)
                arguments = {"keywords": "mathematics", "max_posts": 2}
                result = await extractor.search_posts(**arguments)
            else:
                raise AssertionError(method)
    page.assert_clean()
    return recorder.trace(
        {"method": method, "arguments": arguments},
        _complete_mapping_result(result, section_names=list(result["sections"])),
    )


async def _single_capture_error_scenario() -> dict[str, Any]:
    recorder = TraceRecorder("scrape_job__capture_error", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder).script(
        "evaluate:root_content", RuntimeError("synthetic capture failure")
    )
    extractor = _extractor(page)
    arguments = {"job_id": "123"}
    async with boundaries(recorder, clock):
        with recorder.context("scrape_job"):
            result = await extractor.scrape_job(**arguments)
    page.assert_clean()
    return recorder.trace(
        {"method": "scrape_job", "arguments": arguments},
        _complete_mapping_result(result, section_names=list(result["sections"])),
    )


async def _description_missing_scenario() -> dict[str, Any]:
    recorder = TraceRecorder("scrape_job__description_missing", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    # The header and company details rendered, the description panel did not.
    # Without the heading only a URL-shaped Easy Apply is still read, and this
    # page has none.
    page = (
        _page(recorder)
        .script("evaluate:root_content", _root("Result content"))
        .script(
            "evaluate:job_apply_signals",
            {"easy_apply": False, "external_link": None},
        )
    )
    extractor = _extractor(page)
    arguments = {"job_id": "123"}
    async with boundaries(recorder, clock):
        with recorder.context("scrape_job"):
            result = await extractor.scrape_job(**arguments)
    page.assert_clean()
    return recorder.trace(
        {"method": "scrape_job", "arguments": arguments},
        _complete_mapping_result(result, section_names=list(result["sections"])),
    )


async def _get_my_profile_scenario() -> dict[str, Any]:
    name = "get_my_profile__baseline"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    page.goto_landings.append("https://www.linkedin.com/in/ada-lovelace/")
    page.script("evaluate:root_content", _root("Own profile"))
    _script_profile_target(page, profile_urn="ACoAA-self")
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("get_my_profile", "main_profile"):
            result = await extractor.get_my_profile()
    page.assert_clean()
    return recorder.trace(
        {"method": "get_my_profile", "arguments": {}},
        result,
    )


async def _connect_scenario() -> dict[str, Any]:
    name = "connect_with_person__self_profile"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    page.script("evaluate:root_content", _root("Own profile"))
    _script_profile_target(page, "unavailable")
    page.script(
        "evaluate:connection_action_signals",
        {
            "topCard": True,
            "editIntro": True,
            "pendingItem": False,
            "connectItem": False,
            "menuRead": False,
            "recommendationLink": False,
            "ignoreItem": False,
            "messageItemInMenu": False,
            "composeInTopCard": False,
            "moreClicked": False,
        },
    )
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("connect_with_person", "main_profile"):
            result = await extractor.connect_with_person("ada-lovelace")
    page.assert_clean()
    return recorder.trace(
        {"method": "connect_with_person", "arguments": {"username": "ada-lovelace"}},
        result,
    )


async def _withdraw_invitations_scenario() -> dict[str, Any]:
    name = "withdraw_invitations__dry_run"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    page.declare_locator('main [role="listitem"]', "sent_rows")
    page.declare_derived("sent_rows", "first", "sent_rows.first")
    page.script("sent_rows.first.wait_for", None)
    page.script(
        "evaluate:sent_invitation_rows",
        [
            {
                "href": "/in/ada-lovelace/",
                "name": "Ada Lovelace",
                "text": "Ada Lovelace\nSent 3 weeks ago\nWithdraw",
            },
            {
                "href": "/in/charles-babbage/",
                "name": "Charles Babbage",
                "text": "Charles Babbage\nSent 2 days ago\nWithdraw",
            },
            {
                "href": "/in/grace-hopper/",
                "name": "Grace Hopper",
                "text": "Grace Hopper\nWithdraw",
            },
        ],
    )
    page.script("evaluate:sent_invitation_row_count", *([3] * 37))
    page.script("evaluate:sent_invitation_scroll", *([True] * 3))
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("withdraw_invitations"):
            result = await extractor.withdraw_invitations(14)
    page.assert_clean()
    return recorder.trace(
        {"method": "withdraw_invitations", "arguments": {"older_than_days": 14}},
        result,
    )


async def _sidebar_scenario() -> dict[str, Any]:
    name = "get_sidebar_profiles__baseline"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder).script(
        "evaluate:sidebar_profiles", {"sections": {}, "showAllUrls": {}}
    )
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("get_sidebar_profiles", "sidebar"):
            result = await extractor.get_sidebar_profiles("ada-lovelace")
    page.assert_clean()
    return recorder.trace(
        {"method": "get_sidebar_profiles", "arguments": {"username": "ada-lovelace"}},
        result,
    )


async def _conversation_scenario(method: str) -> dict[str, Any]:
    name = f"{method}__baseline"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    conversation = {
        "entityUrn": "urn:li:msg_conversation:(2-abc)",
        "backendUrn": "urn:li:messagingThread:2-abc",
        "conversationUrl": "https://www.linkedin.com/messaging/thread/2-abc/",
        "conversationParticipants": [],
        "title": {"text": "Policy conversation"},
        "read": False,
    }
    inbox_payload = {
        "data": {"messengerConversationsByCategoryQuery": {"elements": [conversation]}}
    }

    class Request:
        method = "GET"
        url = (
            "https://www.linkedin.com/voyager/api/graphql?"
            "variables=(conversationUrn%3Aurn%3Ali%3Amsg_conversation%3A"
            "%282-abc%29)&queryId=messengerMessages.hash"
        )

        async def all_headers(self) -> dict[str, str]:
            return {"accept": "application/json", "csrf-token": "policy-token"}

    class Response:
        url = (
            "https://www.linkedin.com/voyager/api/graphql?"
            "queryId=messengerConversations.hash"
        )

        async def json(self) -> dict[str, Any]:
            return inbox_payload

    def emit_messaging() -> None:
        page.emit("response", Response())
        if method == "get_conversation":
            page.emit("request", Request())

    page.script("goto", emit_messaging)
    if method == "search_conversations":
        search_box = page.declare_locator(
            conversations_module._SEARCH_BOX_SELECTOR, "search_box"
        )
        page.declare_derived("search_box", "first", "search_box_first")
        del search_box

        class SearchResponse:
            url = (
                "https://www.linkedin.com/voyager/api/graphql?"
                "variables=%28query%3A%28keywords%3Aengine%29%2Ccount%3A20%29"
                "&queryId=messengerConversations.hash"
            )

            async def json(self) -> dict[str, Any]:
                return inbox_payload

        page.script("search_box_first.click", None)
        page.script("keyboard.press", lambda: page.emit("response", SearchResponse()))
    if method in {"get_inbox", "get_conversation"}:
        scrolls = 3 if method == "get_inbox" else 1
        page.script("evaluate:scroll_main_region", *[True] * scrolls)
    if method == "get_inbox":
        page.script("evaluate:root_content", _root("Conversation content"))
    if method == "get_conversation":
        page.script(
            "evaluate:messaging_api_fetch",
            {
                "status": 200,
                "payload": {
                    "data": {
                        "messengerMessagesBySyncToken": {
                            "elements": [
                                {
                                    "backendConversationUrn": conversation["entityUrn"],
                                    "deliveredAt": 1,
                                    "body": {"text": "Policy message"},
                                }
                            ]
                        }
                    }
                },
            },
        )
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context(method, "conversation"):
            if method == "get_inbox":
                arguments = {"limit": 10}
                result = await extractor.get_inbox(limit=10)
            elif method == "get_conversation":
                arguments = {"thread_id": "2-abc"}
                result = await extractor.get_conversation(thread_id="2-abc")
            elif method == "search_conversations":
                arguments = {"keywords": "engine", "limit": 10}

                async def typed(typed_page: Any, text: str) -> None:
                    # The real helper draws jittered delays from an unseeded RNG.
                    await typed_page.keyboard.type(text)

                with patch.object(conversations_module, "human_type", typed):
                    result = await extractor.search_conversations("engine", limit=10)
            else:
                raise AssertionError(method)
    page.assert_clean(retained_read_guard=True)
    return recorder.trace(
        {"method": method, "arguments": arguments},
        _complete_mapping_result(result, section_names=list(result["sections"])),
    )


_CM_API = "https://www.linkedin.com/campaign-manager-api/"
_CM_ACCOUNT = "100000001"
_CM_CAMPAIGN = "100000301"
_CM_GROUP = 100000201
_CM_ACCOUNT_URL = f"https://www.linkedin.com/campaignmanager/accounts/{_CM_ACCOUNT}"
_CM_TUNNEL = {
    "x-http-method-override": "GET",
    "content-type": "application/x-www-form-urlencoded",
}
_CM_CURRENT = "(start:(day:1,month:10,year:2026),end:(day:31,month:10,year:2026))"
_CM_PREVIOUS = "(start:(day:1,month:9,year:2026),end:(day:30,month:9,year:2026))"


def _cm_response(
    path: str, payload: Any, *, body: str | None = None
) -> ScriptedCmResponse:
    request = ScriptedCmRequest(
        _CM_API + path,
        method="GET" if body is None else "POST",
        headers={} if body is None else dict(_CM_TUNNEL),
        post_data=body,
    )
    return ScriptedCmResponse(request, payload)


def _cm_emit(page: ScriptedPage, responses: list[ScriptedCmResponse]) -> None:
    for response in responses:
        page.emit("request", response.request)
        page.emit("response", response)


def _cm_account_record() -> ScriptedCmResponse:
    return _cm_response(
        f"campaignManagerAccounts/{_CM_ACCOUNT}",
        {
            "data": {
                "id": int(_CM_ACCOUNT),
                "name": "Example account",
                "status": "ACTIVE",
                "type": "BUSINESS",
                "currencyCode": "EUR",
            },
            "included": [],
        },
    )


def _cm_groups() -> ScriptedCmResponse:
    return _cm_response(
        f"campaignManagerReportingCampaignGroups?q=accountId&accountId={_CM_ACCOUNT}",
        {
            "elements": [
                {"id": _CM_GROUP, "name": "Example group", "status": "ACTIVE"}
            ],
            "paging": {"total": 1},
        },
    )


def _cm_campaigns() -> ScriptedCmResponse:
    return _cm_response(
        "campaignManagerReportingCampaigns?q=accountAndCampaignGroups"
        f"&accountId={_CM_ACCOUNT}",
        {
            "elements": [
                {
                    "id": int(_CM_CAMPAIGN),
                    "name": "Example ad set",
                    "status": "DRAFT",
                    "campaignGroupId": _CM_GROUP,
                    "dailyBudget": {"amount": "10.00", "currencyCode": "EUR"},
                }
            ],
            "paging": {"total": 1},
        },
    )


async def _campaign_manager_run(
    name: str, page: ScriptedPage, method: str, arguments: dict[str, Any], call: Any
) -> dict[str, Any]:
    recorder = page.recorder
    clock = FakeClock(recorder)
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context(method, name):
            result = await call(extractor)
    page.assert_clean(retained_cm_guard=True)
    return recorder.trace(
        {"method": method, "arguments": arguments},
        _complete_mapping_result(result, section_names=list(result["sections"])),
    )


def _cm_page(name: str) -> ScriptedPage:
    return _page(TraceRecorder(name, _COMMON_ALLOWED | {"route.abort"}))


async def _cm_ad_accounts_scenario() -> dict[str, Any]:
    page = _cm_page("list_ad_accounts__with_boost_account")
    listed = _cm_response(
        "campaignManagerReportingAccounts?q=findAll",
        {
            "elements": [
                {
                    "id": int(_CM_ACCOUNT),
                    "name": "Example account",
                    "status": "ACTIVE",
                    "type": "BUSINESS",
                    "currencyCode": "EUR",
                }
            ],
            "paging": {"total": 1},
        },
    )
    boost = _cm_response(
        "campaignManagerAccounts?q=memberBoostAccounts",
        {
            "data": {
                "elements": [
                    {
                        "id": 100000009,
                        "name": "Example boost account",
                        "type": "PERSONAL",
                        "status": "ACTIVE",
                        "reference": "urn:li:member:100000010",
                    }
                ]
            },
            "included": [],
        },
    )
    page.script("goto", lambda: _cm_emit(page, [listed, boost]))
    return await _campaign_manager_run(
        "ad_accounts",
        page,
        "list_ad_accounts",
        {},
        lambda extractor: extractor.list_ad_accounts(),
    )


async def _cm_campaigns_scenario(*, setup_redirect: bool) -> dict[str, Any]:
    suffix = "setup_redirect" if setup_redirect else "baseline"
    page = _cm_page(f"list_campaigns__{suffix}")
    if setup_redirect:
        # A dormant account lands on campaign setup, where the app then
        # creates a billing configuration on its own: the guard must abort it.
        page.goto_landings.append(f"{_CM_ACCOUNT_URL}/campaigns/new/setup")
        billing = ScriptedCmRequest(
            f"{_CM_API}campaignManagerAccounts/{_CM_ACCOUNT}"
            "/billingConfiguration?action=findOrCreate",
            method="POST",
            headers={"content-type": "application/json"},
            post_data="{}",
        )

        def setup_wave() -> Any:
            _cm_emit(page, [_cm_account_record(), _cm_groups()])
            return page.route_request(billing)

        def routed() -> bool:
            _cm_emit(page, [_cm_campaigns()])
            return True

        page.script("goto", setup_wave)
        page.script("evaluate:cm_route", routed)
    else:
        page.script(
            "goto",
            lambda: _cm_emit(
                page, [_cm_account_record(), _cm_groups(), _cm_campaigns()]
            ),
        )
    trace = await _campaign_manager_run(
        "campaigns",
        page,
        "list_campaigns",
        {"account_id": _CM_ACCOUNT},
        lambda extractor: extractor.list_campaigns(_CM_ACCOUNT),
    )
    if setup_redirect:
        steps = [
            event.get("operation", event["kind"])
            for event in trace["events"]
            if event["kind"] in {"navigate", "route.abort", "route.fallback"}
            or event.get("operation") == "cm_route"
        ]
        # One page load, the app's write refused, then exactly one in-app hop.
        assert steps == ["navigate", "route.abort", "cm_route"], steps
    return trace


async def _cm_campaign_scenario() -> dict[str, Any]:
    page = _cm_page("get_campaign__details")
    record = _cm_response(
        f"campaignManagerCampaigns/{_CM_CAMPAIGN}",
        {
            "data": {
                "id": int(_CM_CAMPAIGN),
                "accountId": int(_CM_ACCOUNT),
                "campaignGroupId": _CM_GROUP,
                "name": "Example ad set",
                "status": "DRAFT",
                "targetingCriteria": {
                    "include": {
                        "and": [
                            {
                                "or": [
                                    {
                                        "facet": {
                                            "urn": "urn:li:adTargetingFacet:locations",
                                            "name": "Locations",
                                        },
                                        "segments": [
                                            {"urn": "urn:li:geo:100", "name": "Example"}
                                        ],
                                    }
                                ]
                            }
                        ]
                    },
                    "exclude": {"or": []},
                },
            },
            "included": [],
        },
    )
    group = _cm_response(
        f"campaignManagerCampaignGroups/{_CM_GROUP}",
        {"id": _CM_GROUP, "accountId": int(_CM_ACCOUNT), "name": "Example group"},
    )
    creative = "urn:li:sponsoredCreative:100000401"
    creatives = _cm_response(
        f"campaignManagerCreatives?q=campaignIds&campaignIds=List({_CM_CAMPAIGN})",
        {
            "data": {"elements": [creative]},
            "included": [
                {
                    "entityUrn": creative,
                    "campaign": f"urn:li:sponsoredCampaign:{_CM_CAMPAIGN}",
                    "status": "ACTIVE",
                }
            ],
        },
    )
    page.script("goto", lambda: _cm_emit(page, [record, group, creatives]))
    return await _campaign_manager_run(
        "campaign",
        page,
        "get_campaign",
        {"account_id": _CM_ACCOUNT, "campaign_id": _CM_CAMPAIGN},
        lambda extractor: extractor.get_campaign(_CM_ACCOUNT, _CM_CAMPAIGN),
    )


async def _cm_analytics_scenario() -> dict[str, Any]:
    page = _cm_page("get_campaign_analytics__replayed_period")

    def statistics(date_range: str, impressions: int) -> ScriptedCmResponse:
        return _cm_response(
            "campaignManagerAdAnalytics?fields=baseMetrics:"
            "(costInLocalCurrency,impressions,clicks),pivotValues",
            {
                "elements": [
                    {
                        "pivotValues": [
                            {
                                "id": f"urn:li:sponsoredCampaign:{_CM_CAMPAIGN}",
                                "displayText": "Example ad set",
                            }
                        ],
                        "baseMetrics": {"impressions": impressions, "clicks": 1},
                    }
                ],
                "paging": {},
            },
            body=(
                "q=statistics&pivots=List(CAMPAIGN)&timeGranularity=ALL"
                f"&dateRange={date_range}&accountIds=List({_CM_ACCOUNT})"
            ),
        )

    page.script(
        "goto",
        lambda: _cm_emit(
            page,
            [
                _cm_account_record(),
                statistics(_CM_PREVIOUS, 3),
                statistics(_CM_CURRENT, 5),
            ],
        ),
    )
    page.script(
        "evaluate:cm_analytics_replay",
        {
            "status": 200,
            "payload": {
                "elements": [
                    {
                        "pivotValues": [
                            {"id": f"urn:li:sponsoredCampaign:{_CM_CAMPAIGN}"}
                        ],
                        "baseMetrics": {
                            "impressions": 7,
                            "clicks": 2,
                            "costInLocalCurrency": 1.5,
                        },
                    }
                ]
            },
        },
    )
    return await _campaign_manager_run(
        "analytics",
        page,
        "get_campaign_analytics",
        {
            "account_id": _CM_ACCOUNT,
            "level": "campaign",
            "start_date": "2026-08-01",
            "end_date": "2026-08-31",
        },
        lambda extractor: extractor.get_campaign_analytics(
            _CM_ACCOUNT,
            level="campaign",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
        ),
    )


_SN_API = "https://www.linkedin.com/sales-api/"
_SN_COMPANY = "100000001"
_SN_LIST = "100000301"
_SN_GEO = "100000099"


def _sn_response(
    path: str, payload: Any = None, *, method: str = "GET", status: int = 200
) -> ScriptedCmResponse:
    request = ScriptedCmRequest(_SN_API + path, method=method)
    return ScriptedCmResponse(request, payload, status=status)


def _sn_boot() -> list[ScriptedCmResponse]:
    return [
        _sn_response("salesApiAccess"),
        _sn_response("salesApiEntitlements"),
        _sn_response("salesApiWebAppBoot?action=recordBoot", method="POST"),
    ]


def _sn_emit(page: ScriptedPage, responses: list[ScriptedCmResponse]) -> None:
    for response in responses:
        page.emit("response", response)


def _sn_search(
    resource: str, query: str, start: int, elements: list[Any], total: int
) -> ScriptedCmResponse:
    return _sn_response(
        f"{resource}?q=searchQuery&query={query}&start={start}&count=25"
        "&decorationId=example",
        {
            "metadata": {},
            "elements": elements,
            "paging": {"start": start, "count": 25, "total": total},
        },
    )


def _sn_lead(member: str, name: str, **extra: Any) -> dict[str, Any]:
    return {
        "entityUrn": f"urn:li:fs_salesProfile:({member},NAME_SEARCH,abc)",
        "objectUrn": "urn:li:member:100000011",
        "fullName": name,
        "geoRegion": "Example region",
        "seniorityV2s": [{"id": 310, "displayName": "CXO"}],
        "currentPositions": [
            {
                "title": "Example title",
                "companyName": "Example Company",
                "companyUrn": f"urn:li:fs_salesCompany:{_SN_COMPANY}",
                "current": True,
            }
        ],
        **extra,
    }


def _sn_list_entry(list_id: int, name: str, count: int) -> dict[str, Any]:
    return {
        "id": list_id,
        "name": name,
        "listType": "ACCOUNT",
        "entityCount": count,
    }


async def _sales_navigator_run(
    name: str, page: ScriptedPage, method: str, arguments: dict[str, Any], call: Any
) -> dict[str, Any]:
    recorder = page.recorder
    clock = FakeClock(recorder)
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context(method, name):
            try:
                result = await call(extractor)
            except LinkedInScraperException as exc:
                page.assert_clean()
                return recorder.trace(
                    {"method": method, "arguments": arguments},
                    {"raised": type(exc).__name__, "message": str(exc)},
                )
    page.assert_clean()
    return recorder.trace(
        {"method": method, "arguments": arguments},
        _complete_mapping_result(result, section_names=list(result["sections"])),
    )


def _sn_page(name: str) -> ScriptedPage:
    return _page(TraceRecorder(name, _COMMON_ALLOWED))


async def _sn_account_search_scenario() -> dict[str, Any]:
    page = _sn_page("search_sales_navigator_accounts__page_two_filters_echoed")
    keywords = "cloud ai, saas: b2b"
    filters = [
        SearchFilter(INDUSTRY, ids=("4",)),
        SearchFilter(REGION, ids=(_SN_GEO,)),
        SearchFilter(COMPANY_HEADCOUNT, ids=("B",)),
    ]
    query = build_search_query(keywords, filters)
    card = {
        "entityUrn": f"urn:li:fs_salesCompany:{_SN_COMPANY}",
        "companyName": "Example Company",
        "industry": "Example industry",
        "employeeCountRange": "11-50",
    }
    page.script(
        "goto",
        lambda: _sn_emit(
            page,
            [
                *_sn_boot(),
                # The page's first, unpaged call: page 2 must start at 25.
                _sn_search("salesApiAccountSearch", query, 0, [], 30),
                _sn_search("salesApiAccountSearch", query, 25, [card], 30),
            ],
        ),
    )
    arguments = {
        "keywords": keywords,
        "industry": ["4"],
        "hq_location": [_SN_GEO],
        "size": ["B"],
        "page": 2,
    }
    return await _sales_navigator_run(
        "account_search",
        page,
        "search_sales_navigator_accounts",
        arguments,
        lambda extractor: extractor.search_sales_navigator_accounts(**arguments),
    )


async def _sn_lead_search_scenario() -> dict[str, Any]:
    page = _sn_page("search_sales_navigator_leads__one_withheld")
    query = build_search_query(
        None,
        [
            SearchFilter(SENIORITY_LEVEL, ids=("310",)),
            SearchFilter(REGION, ids=(_SN_GEO,)),
        ],
    )
    leads = [
        _sn_lead("ACwAAA0001", "Example Person"),
        _sn_lead("ACwAAA0002", "Withheld Person", blockThirdPartyDataSharing=True),
    ]
    page.script(
        "goto",
        lambda: _sn_emit(
            page,
            [*_sn_boot(), _sn_search("salesApiLeadSearch", query, 0, leads, 2)],
        ),
    )
    arguments = {"seniority": ["cxo"], "location": [f"urn:li:geo:{_SN_GEO}"]}
    trace = await _sales_navigator_run(
        "lead_search",
        page,
        "search_sales_navigator_leads",
        arguments,
        lambda extractor: extractor.search_sales_navigator_leads(**arguments),
    )
    # The withheld lead is counted, never carried into the result.
    assert "Withheld" not in json.dumps(trace["result"]), trace["result"]
    return trace


async def _sn_account_scenario() -> dict[str, Any]:
    page = _sn_page("get_sales_navigator_account__both_decorations")
    record = f"salesApiCompanies/{_SN_COMPANY}?decoration=example"
    page.script(
        "goto",
        lambda: _sn_emit(
            page,
            [
                *_sn_boot(),
                _sn_response(
                    record,
                    {
                        "entityUrn": f"urn:li:fs_salesCompany:{_SN_COMPANY}",
                        "name": "Example Company",
                        "website": "https://example.com",
                        "employees": [{"fullName": "Example Person"}],
                    },
                ),
                _sn_response(
                    record, {"employeeCount": 42, "employeeDisplayCount": "42"}
                ),
            ],
        ),
    )
    trace = await _sales_navigator_run(
        "account",
        page,
        "get_sales_navigator_account",
        {"company_id": _SN_COMPANY},
        lambda extractor: extractor.get_sales_navigator_account(_SN_COMPANY),
    )
    assert "employees" not in trace["result"]["account"], trace["result"]
    return trace


async def _sn_account_lists_scenario() -> dict[str, Any]:
    page = _sn_page("list_sales_navigator_account_lists__one_page")
    page.script(
        "goto",
        lambda: _sn_emit(
            page,
            [
                *_sn_boot(),
                _sn_response(
                    "salesApiLists?q=listType&listType=ACCOUNT&start=0&count=25",
                    {
                        "elements": [
                            _sn_list_entry(100000301, "Example list", 26),
                            _sn_list_entry(100000302, "Example other list", 3),
                        ],
                        "paging": {"start": 0, "count": 25, "total": 2},
                    },
                ),
            ],
        ),
    )
    return await _sales_navigator_run(
        "account_lists",
        page,
        "list_sales_navigator_account_lists",
        {},
        lambda extractor: extractor.list_sales_navigator_account_lists(),
    )


async def _sn_account_list_scenario() -> dict[str, Any]:
    page = _sn_page("get_sales_navigator_account_list__two_pages")

    def table(start: int, count: int) -> ScriptedCmResponse:
        return _sn_response(
            f"salesApiDashboardAccountTable?q=list&listId={_SN_LIST}"
            f"&accountDashboardListType=CUSTOM_LISTS&start={start}&count=25"
            "&columns=List(UNIFIED_RECOMMENDED_LEAD)",
            {
                "elements": [
                    {
                        "companyInfo": {
                            "entityUrn": "urn:li:fs_salesCompany:"
                            f"{100000500 + start + index}",
                            "name": "Example Company",
                        },
                        "recommendedLead": {"fullName": "Example Person"},
                    }
                    for index in range(count)
                ],
                "paging": {"start": start, "count": 25, "total": 26},
            },
        )

    metadata = _sn_response(
        f"salesApiLists/{_SN_LIST}?decoration=example",
        {"id": int(_SN_LIST), "name": "Example list", "entityCount": 26},
    )
    page.script(
        "goto",
        lambda: _sn_emit(page, [*_sn_boot(), table(0, 25), metadata]),
        lambda: _sn_emit(page, [table(25, 1)]),
    )
    trace = await _sales_navigator_run(
        "account_list",
        page,
        "get_sales_navigator_account_list",
        {"list_id": _SN_LIST},
        lambda extractor: extractor.get_sales_navigator_account_list(_SN_LIST),
    )
    navigations = [e for e in trace["events"] if e["kind"] == "navigate"]
    # Every page of the list is its own page load.
    assert len(navigations) == 2, navigations
    assert "recommendedLead" not in json.dumps(trace["result"]), trace["result"]
    return trace


async def _sn_lead_lists_scenario() -> dict[str, Any]:
    page = _sn_page("list_sales_navigator_lead_lists__one_page")
    entry = {**_sn_list_entry(100000311, "Example lead list", 2), "listType": "LEAD"}
    page.script(
        "goto",
        lambda: _sn_emit(
            page,
            [
                *_sn_boot(),
                _sn_response(
                    "salesApiLists?q=listType&listType=LEAD&start=0&count=25",
                    {
                        "elements": [entry],
                        "paging": {"start": 0, "count": 25, "total": 1},
                    },
                ),
            ],
        ),
    )
    return await _sales_navigator_run(
        "lead_lists",
        page,
        "list_sales_navigator_lead_lists",
        {},
        lambda extractor: extractor.list_sales_navigator_lead_lists(),
    )


async def _sn_lead_list_scenario() -> dict[str, Any]:
    page = _sn_page("get_sales_navigator_lead_list__one_withheld")
    list_id = "100000311"
    query = build_search_query(None, [SearchFilter(LEAD_LIST, ids=(list_id,))])
    leads = [
        _sn_lead("ACwAAA0001", "Example Person", degree=1),
        _sn_lead("ACwAAA0002", "Withheld Person", blockThirdPartyDataSharing=True),
    ]
    entry = {**_sn_list_entry(int(list_id), "Example lead list", 2), "listType": "LEAD"}
    page.script(
        "goto",
        # The id is looked up among the member's lead lists before the search.
        lambda: _sn_emit(
            page,
            [
                *_sn_boot(),
                _sn_response(
                    "salesApiLists?q=listType&listType=LEAD&start=0&count=25",
                    {
                        "elements": [entry],
                        "paging": {"start": 0, "count": 25, "total": 1},
                    },
                ),
            ],
        ),
        lambda: _sn_emit(page, [_sn_search("salesApiLeadSearch", query, 0, leads, 2)]),
    )
    trace = await _sales_navigator_run(
        "lead_list",
        page,
        "get_sales_navigator_lead_list",
        {"list_id": list_id},
        lambda extractor: extractor.get_sales_navigator_lead_list(list_id),
    )
    assert "Withheld" not in json.dumps(trace["result"]), trace["result"]
    return trace


async def _sn_no_seat_scenario() -> dict[str, Any]:
    page = _sn_page("list_sales_navigator_account_lists__no_seat")
    page.goto_landings.append("https://www.linkedin.com/premium/products/?from=x")
    return await _sales_navigator_run(
        "no_seat",
        page,
        "list_sales_navigator_account_lists",
        {},
        lambda extractor: extractor.list_sales_navigator_account_lists(),
    )


async def _facade_contract_trace() -> dict[str, Any]:
    global _TOOL_SCHEMAS

    methods = {}
    for name in TOOL_FACADE_METHODS | COMPATIBILITY_METHODS:
        member = getattr(LinkedInExtractor, name)
        methods[name] = {
            "signature": str(inspect.signature(member)),
            "coroutine": inspect.iscoroutinefunction(member),
        }
    if _TOOL_SCHEMAS is None:
        tools = await create_mcp_server().list_tools()
        _TOOL_SCHEMAS = {
            tool.name: {
                "input": tool.parameters,
                "output": tool.output_schema,
            }
            for tool in sorted(tools, key=lambda item: item.name)
        }
    return {
        "schema_version": 1,
        "scenario": "facade_contract",
        "call": {"method": "LinkedInExtractor", "arguments": {"constructor": "Page"}},
        "events": [],
        "result": {
            "tool_methods": sorted(TOOL_FACADE_METHODS),
            "compatibility_methods": sorted(COMPATIBILITY_METHODS),
            "methods": methods,
            "tool_schemas": _TOOL_SCHEMAS,
        },
    }


TOOL_FACADE_METHODS = {
    "connect_with_person",
    "extract_feed",
    "extract_page",
    "get_campaign",
    "get_campaign_analytics",
    "get_company_employees",
    "get_conversation",
    "get_inbox",
    "get_my_profile",
    "get_saved_jobs",
    "get_sidebar_profiles",
    "list_ad_accounts",
    "list_campaigns",
    "scrape_company",
    "scrape_job",
    "scrape_person",
    "search_companies",
    "search_conversations",
    "search_jobs",
    "search_people",
    "search_posts",
    "send_message",
    "reply_to_conversation",
    "withdraw_invitations",
    "update_campaign",
    "set_campaign_status",
    "search_targeting_entities",
    "update_campaign_targeting",
    "create_campaign_draft",
    "add_post_to_campaign",
    "list_sponsorable_posts",
    "list_audiences",
    "create_company_list_audience",
    "create_saved_audience",
    "search_sales_navigator_accounts",
    "search_sales_navigator_leads",
    "get_sales_navigator_account",
    "list_sales_navigator_account_lists",
    "get_sales_navigator_account_list",
    "list_sales_navigator_lead_lists",
    "get_sales_navigator_lead_list",
}
COMPATIBILITY_METHODS = {"get_page_text", "click_button_by_text"}


_CM_WRITE_ALLOWED = _COMMON_ALLOWED | {
    "route.abort",
    "route.fallback",
    "locator.focus",
    "locator.get_attribute",
    "locator.input_value",
    "locator.is_enabled",
    "locator.select_option",
    "locator.select_text",
    "locator.set_input_files",
}
_CM_DETAILS = f"{_CM_ACCOUNT_URL}/campaigns/{_CM_CAMPAIGN}/details"
_CM_NEW_CAMPAIGN = "100000302"
# The editor's today, fixed so a trace never depends on the day it is built.
_CM_TODAY = date(2026, 11, 2)
_CM_DAY_MS = 86_400_000


def _cm_write_page(name: str, url: str = "about:blank") -> ScriptedPage:
    return _page(TraceRecorder(name, _CM_WRITE_ALLOWED), url=url)


def _cm_campaign_record(
    campaign: str = _CM_CAMPAIGN, **fields: Any
) -> ScriptedCmResponse:
    record = {
        "id": int(campaign),
        "accountId": int(_CM_ACCOUNT),
        "campaignGroupId": _CM_GROUP,
        "name": "Example ad set",
        "status": "DRAFT",
        "objectiveType": "WEBSITE_VISIT",
        "dailyBudget": {"amount": "10", "currencyCode": "EUR"},
        "runSchedule": {
            # 2026-12-01 and 2026-12-31 23:45 UTC, as Campaign Manager keeps them.
            "start": 1_796_083_200_000,
            "end": 1_798_760_700_000,
        },
        **fields,
    }
    return _cm_response(
        f"campaignManagerCampaigns/{campaign}", {"data": record, "included": []}
    )


def _cm_write(
    path: str,
    body: Any,
    *,
    restli: str | None,
    status: int = 200,
    created_id: str | None = None,
) -> ScriptedCmResponse:
    headers = {"content-type": "application/json"}
    if restli is not None:
        headers["x-restli-method"] = restli
    request = ScriptedCmRequest(
        _CM_API + path, method="POST", headers=headers, post_data=json.dumps(body)
    )
    return ScriptedCmResponse(
        request,
        {},
        status=status,
        headers={"x-restli-id": created_id} if created_id else None,
    )


def _cm_billing_write() -> ScriptedCmResponse:
    return _cm_write(
        f"campaignManagerAccounts/{_CM_ACCOUNT}/billingConfiguration"
        "?action=findOrCreate",
        {},
        restli=None,
    )


async def _cm_app_sends(page: ScriptedPage, *writes: ScriptedCmResponse) -> None:
    """The app's own writes, each through the guard and answered only if passed."""
    for write in writes:
        if await page.route_request(write.request):
            _cm_emit(page, [write])


def _cm_patch(campaign: str = _CM_CAMPAIGN, **fields: Any) -> ScriptedCmResponse:
    return _cm_write(
        f"campaignManagerCampaigns/{campaign}",
        {"patch": {"$set": fields}},
        restli="partial_update",
    )


async def _cm_write_run(
    page: ScriptedPage, method: str, arguments: dict[str, Any], call: Any
) -> dict[str, Any]:
    recorder = page.recorder
    clock = FakeClock(recorder)
    extractor = _extractor(page)

    async def typed(typed_page: Any, text: str, **_kwargs: Any) -> None:
        # The real helpers draw jittered delays from an unseeded RNG.
        await typed_page.keyboard.type(text)

    async def paused(base: float, spread: float = 0.5) -> None:
        await asyncio.sleep(base)

    with (
        patch.object(cm_editor_module, "_today", return_value=_CM_TODAY),
        patch.object(cm_targeting_module, "human_type", typed),
        patch.object(cm_targeting_module, "human_pause", paused),
    ):
        async with boundaries(recorder, clock):
            with recorder.context(method, recorder.scenario):
                result = await call(extractor)
    page.assert_clean(retained_cm_guard=True)
    return recorder.trace({"method": method, "arguments": arguments}, result)


async def _cm_update_campaign_scenario(outcome: str) -> dict[str, Any]:
    """A draft's daily budget: a dry run, the saved autosave, or an off-plan one."""
    page = _cm_write_page(f"update_campaign__{outcome}")
    goto: list[Any] = [lambda: _cm_emit(page, [_cm_campaign_record()])]
    ready = {"state": "ready"}
    editor: list[Any] = [ready]
    if outcome != "dry_run":
        if outcome == "draft":
            # The app's own billing call rides along the autosave: aborted,
            # while the planned budget alone leaves the browser.
            sent = [_cm_billing_write(), _cm_patch(dailyBudget=_cm_money("12.5"))]
            goto.append(
                lambda: _cm_emit(
                    page, [_cm_campaign_record(dailyBudget=_cm_money("12.5"))]
                )
            )
        else:
            sent = [_cm_patch(dailyBudget=_cm_money("99"))]
            goto.append(None)

        async def blur() -> dict[str, bool]:
            await _cm_app_sends(page, *sent)
            return {"blurred": True}

        editor += [ready, {"state": "typed"}, blur]
        if outcome != "draft":
            # The refused save's notes record the input's flags.
            editor.append({"count": 1, "attached": True, "focused": False})
        # The editor that autosaves a draft has no classic footer.
        page.script("evaluate:cm_footer", 0)
    page.script("goto", *goto)
    page.script("evaluate:cm_editor", *editor)
    arguments = {
        "account_id": _CM_ACCOUNT,
        "campaign_id": _CM_CAMPAIGN,
        "daily_budget": "12.5",
        "confirm_send": outcome != "dry_run",
    }
    trace = await _cm_write_run(
        page,
        "update_campaign",
        arguments,
        lambda extractor: extractor.update_campaign(
            _CM_ACCOUNT,
            _CM_CAMPAIGN,
            daily_budget="12.5",
            confirm_send=outcome != "dry_run",
        ),
    )
    kinds = [event["kind"] for event in trace["events"]]
    if outcome == "dry_run":
        assert "route.fallback" not in kinds and "keyboard.insert_text" not in kinds
    return trace


def _cm_money(amount: str) -> dict[str, str]:
    return {"amount": amount, "currencyCode": "EUR"}


async def _cm_set_status_scenario() -> dict[str, Any]:
    page = _cm_write_page("set_campaign_status__pause")

    async def toggle() -> dict[str, str]:
        await _cm_app_sends(page, _cm_patch(status="PAUSED"))
        return {"state": "ready"}

    page.script(
        "goto",
        lambda: _cm_emit(page, [_cm_campaign_record(status="ACTIVE")]),
        lambda: _cm_emit(page, [_cm_campaign_record(status="PAUSED")]),
    )
    page.script("evaluate:cm_editor", {"state": "ready"}, toggle)
    return await _cm_write_run(
        page,
        "set_campaign_status",
        {
            "account_id": _CM_ACCOUNT,
            "campaign_id": _CM_CAMPAIGN,
            "status": "PAUSED",
            "confirm_send": True,
        },
        lambda extractor: extractor.set_campaign_status(
            _CM_ACCOUNT, _CM_CAMPAIGN, status="PAUSED", confirm_send=True
        ),
    )


async def _cm_create_draft_scenario() -> dict[str, Any]:
    page = _cm_write_page("create_campaign_draft__created")
    group = _cm_response(
        f"campaignManagerCampaignGroups/{_CM_GROUP}",
        {"id": _CM_GROUP, "accountId": int(_CM_ACCOUNT), "name": "Example group"},
    )

    async def setup() -> None:
        # Latched on arrival: the app's own billing call is aborted.
        _cm_emit(page, [group])
        await _cm_app_sends(page, _cm_billing_write())

    async def save_and_exit() -> dict[str, Any]:
        # The classic editor creates the draft only here, the name in it.
        create = _cm_write(
            "campaignManagerCampaigns",
            {
                "status": "DRAFT",
                "accountId": f"urn:li:sponsoredAccount:{_CM_ACCOUNT}",
                "campaignGroupId": f"urn:li:sponsoredCampaignGroup:{_CM_GROUP}",
                "objectiveType": "WEBSITE_VISIT",
                "name": "Example draft",
            },
            restli="create",
            created_id=_CM_NEW_CAMPAIGN,
        )
        await _cm_app_sends(page, create)
        return {"count": 2, "clicked": True}

    page.script(
        "goto",
        setup,
        None,
        lambda: _cm_emit(
            page, [_cm_campaign_record(_CM_NEW_CAMPAIGN, name="Example draft")]
        ),
    )
    page.script(
        "evaluate:cm_editor",
        {"state": "ready", "available": ["WEBSITE_VISIT"]},
        {"state": "ready"},
        {"state": "typed"},
        {"blurred": True},
    )
    # The classic editor's footer, then its Save and exit.
    page.script("evaluate:cm_footer", 1, save_and_exit)
    return await _cm_write_run(
        page,
        "create_campaign_draft",
        {
            "account_id": _CM_ACCOUNT,
            "campaign_group_id": str(_CM_GROUP),
            "objective": "WEBSITE_VISIT",
            "name": "Example draft",
            "confirm_send": True,
        },
        lambda extractor: extractor.create_campaign_draft(
            _CM_ACCOUNT,
            str(_CM_GROUP),
            objective="WEBSITE_VISIT",
            name="Example draft",
            confirm_send=True,
        ),
    )


_CM_POST = "urn:li:share:100000701"
_CM_CREATIVE = "100000501"
_CM_MATCHED_URL = f"{_CM_ACCOUNT_URL}/audiences/matched"


async def _cm_add_post_scenario() -> dict[str, Any]:
    page = _cm_write_page("add_post_to_campaign__added")
    library_path = (
        f"/campaignmanager/accounts/{_CM_ACCOUNT}/campaigns/{_CM_CAMPAIGN}"
        "/creatives/sponsorable-contents?businessId=personal"
    )
    library = _cm_response(
        "campaignManagerContentLibraryEntities?q=organicCompanyContent"
        "&count=100&start=0",
        {
            "data": {"elements": [{"content": {"*sponsoredUpdateContent": _CM_POST}}]},
            "included": [
                {
                    "entityUrn": _CM_POST,
                    "sponsorable": True,
                    "nonSponsorableReasons": [],
                    "visibility": "ORGANIC",
                    "userGeneratedAdContent": {"contentType": "TEXT"},
                }
            ],
        },
    )
    creative = f"urn:li:sponsoredCreative:{_CM_CREATIVE}"
    creatives = _cm_response(
        f"campaignManagerCreatives?q=campaignIds&campaignIds=List({_CM_CAMPAIGN})",
        {
            "data": {"elements": [creative]},
            "included": [
                {
                    "entityUrn": creative,
                    "id": creative,
                    "campaign": f"urn:li:sponsoredCampaign:{_CM_CAMPAIGN}",
                    "reference": _CM_POST,
                    "status": "ACTIVE",
                }
            ],
        },
    )

    def rows(checked: int | None = None) -> dict[str, Any]:
        return {
            "rows": [
                {
                    "id": "select-row-0-checkbox",
                    "disabled": False,
                    "checked": checked == 0,
                }
            ],
            "add": {"disabled": checked is None},
        }

    def opened() -> bool:
        # The in-app route change to the ad set's content library.
        page.url = page.main_frame.url = "https://www.linkedin.com" + library_path
        _cm_emit(page, [library])
        return True

    async def add() -> dict[str, Any]:
        await _cm_app_sends(
            page,
            _cm_write(
                "campaignManagerCreatives",
                {
                    "campaignId": int(_CM_CAMPAIGN),
                    "reference": _CM_POST,
                    "status": "ACTIVE",
                    "type": "SPONSORED_STATUS_UPDATE",
                    "name": "",
                },
                restli="create",
                created_id=creative,
            ),
        )
        return {"clicked": True, "state": rows(0)}

    page.script(
        "goto",
        lambda: _cm_emit(page, [_cm_campaign_record()]),
        lambda: _cm_emit(page, [_cm_campaign_record(), creatives]),
    )
    page.script("evaluate:cm_route", opened)
    page.script(
        "evaluate:cm_content_pill", {"found": True, "checked": True, "clicked": False}
    )
    page.script("evaluate:cm_content_rows", rows())
    page.script("evaluate:cm_content_click", {"clicked": True, "state": rows(0)}, add)
    return await _cm_write_run(
        page,
        "add_post_to_campaign",
        {
            "account_id": _CM_ACCOUNT,
            "campaign_id": _CM_CAMPAIGN,
            "post_urn": _CM_POST,
            "confirm_send": True,
        },
        lambda extractor: extractor.add_post_to_campaign(
            _CM_ACCOUNT, _CM_CAMPAIGN, _CM_POST, confirm_send=True
        ),
    )


async def _cm_list_sponsorable_posts_scenario() -> dict[str, Any]:
    """The content library as LinkedIn answers it: null reasons, a text excerpt."""
    page = _cm_write_page("list_sponsorable_posts__listed")
    library_path = (
        f"/campaignmanager/accounts/{_CM_ACCOUNT}/campaigns/{_CM_CAMPAIGN}"
        "/creatives/sponsorable-contents?businessId=personal"
    )
    library = _cm_response(
        "campaignManagerContentLibraryEntities?q=organicCompanyContent"
        "&count=100&start=0",
        {
            "data": {
                "elements": [{"content": {"*sponsoredUpdateContent": _CM_POST}}],
                "paging": {"count": 100, "start": 0, "links": []},
            },
            "included": [
                {
                    "entityUrn": _CM_POST,
                    "lastModifiedAt": 1893456000000,
                    "nonSponsorableReasons": None,
                    "sponsorable": True,
                    "state": {"lifecycleState": "PUBLISHED"},
                    "userGeneratedAdContent": {
                        "contentType": "TEXT",
                        "introductoryText": {"text": "Example company post"},
                    },
                    "visibility": "ORGANIC",
                }
            ],
        },
    )

    def opened() -> bool:
        page.url = page.main_frame.url = "https://www.linkedin.com" + library_path
        _cm_emit(page, [library])
        return True

    page.script("goto", lambda: _cm_emit(page, [_cm_campaign_record()]))
    page.script("evaluate:cm_route", opened)
    page.script(
        "evaluate:cm_content_pill", {"found": True, "checked": True, "clicked": False}
    )
    trace = await _cm_write_run(
        page,
        "list_sponsorable_posts",
        {"account_id": _CM_ACCOUNT, "campaign_id": _CM_CAMPAIGN},
        lambda extractor: extractor.list_sponsorable_posts(_CM_ACCOUNT, _CM_CAMPAIGN),
    )
    trace["result"] = _complete_mapping_result(
        trace["result"], section_names=list(trace["result"]["sections"])
    )
    return trace


def _cm_matched(*elements: dict[str, Any]) -> ScriptedCmResponse:
    return _cm_response(
        f"campaignManagerMatchedAudiences?q=account&accountId={_CM_ACCOUNT}",
        {"elements": list(elements), "paging": {"count": 10, "start": 0}},
    )


def _cm_list_entry(
    audience_id: int, name: str, status: str, source_id: int = 100000590
) -> dict[str, Any]:
    return {
        "segmentId": audience_id,
        "sourceId": source_id,
        "name": name,
        "segmentType": "COMPANY",
        "listAudienceType": "ACCOUNT_LIST",
        "sourcePlatform": "LIST_UPLOAD",
        "status": status,
    }


async def _cm_list_audiences_scenario() -> dict[str, Any]:
    page = _cm_write_page("list_audiences__matched_and_saved")
    page.declare_locator(
        '[id$="-tab-accounts.item.audiences.saved.index"]', "cm_saved_tab"
    )
    saved = _cm_response(
        f"campaignManagerTargetingCriteria?q=member&accountId={_CM_ACCOUNT}",
        {
            "elements": [
                {
                    "type": "USER_AUDIENCE",
                    "id": 100000601,
                    "name": "Example saved audience",
                    "ownership": "ACCOUNT",
                    "audienceCount": 4000,
                }
            ],
            "paging": {"count": 10, "start": 0},
        },
    )
    page.script(
        "goto",
        lambda: _cm_emit(
            page, [_cm_matched(_cm_list_entry(100000501, "Example list", "READY"))]
        ),
    )
    page.script("cm_saved_tab.count", 1)
    page.script("cm_saved_tab.click", lambda: _cm_emit(page, [saved]))
    trace = await _cm_write_run(
        page,
        "list_audiences",
        {"account_id": _CM_ACCOUNT},
        lambda extractor: extractor.list_audiences(_CM_ACCOUNT),
    )
    trace["result"] = _complete_mapping_result(
        trace["result"], section_names=list(trace["result"]["sections"])
    )
    return trace


async def _cm_audience_upload_scenario() -> dict[str, Any]:
    page = _cm_write_page("create_company_list_audience__uploaded")
    owner = f"urn:li:sponsoredAccount:{_CM_ACCOUNT}"
    dialog = "[role=dialog][aria-labelledby=create-matched-audience-modal-title]"
    page.declare_locator("#ma-header-actions-create-trigger", "cm_create_trigger")
    page.declare_locator('[id="hue-menu-1"] [role=menuitem]', "cm_menu_items")
    page.declare_derived("cm_menu_items", "first", "cm_menu_item_first")
    page.declare_derived("cm_menu_items", "nth:1", "cm_menu_item_matched")
    page.declare_locator(dialog, "cm_upload_dialog")
    page.declare_derived("cm_upload_dialog", "first", "cm_upload_dialog_first")
    for selector, semantic_id in (
        ("#create-ma-list-name-input", "cm_list_name"),
        (
            'select#create-ma-list-modal-type-select option[value="COMPANY"]',
            "cm_company_option",
        ),
        ("select#create-ma-list-modal-type-select", "cm_list_type"),
        ("input#create-ma-list-file-input[type=file]", "cm_list_file"),
        ("a[data-test-create-ma-list-panel__legal-terms-link]", "cm_terms_link"),
        ("button", "cm_upload_buttons"),
    ):
        page.declare_locator(selector, semantic_id, parent="cm_upload_dialog")
    page.declare_derived("cm_upload_buttons", "last", "cm_upload_submit")

    async def submit() -> None:
        signed = _cm_write(
            "campaignManagerAmbrySignedUrl",
            {
                "owner": owner,
                "requestType": "UPLOAD",
                "uploadType": "AUDIENCE_MATCHING",
                "contentType": "text/csv",
            },
            restli="create",
        )
        segment = _cm_write(
            "campaignManagerDmpSegments",
            {
                "inputFileName": "example-list.csv",
                "inputFile": "urn:li:media:example",
                "account": owner,
                "name": "Example list",
                "sourcePlatform": "LIST_UPLOAD",
                "segmentType": "COMPANY",
            },
            restli="create",
            created_id="100000502",
        )
        await _cm_app_sends(page, signed, segment)

    page.script(
        "goto",
        lambda: _cm_emit(
            page, [_cm_matched(_cm_list_entry(100000501, "Older list", "READY"))]
        ),
        lambda: _cm_emit(
            page,
            [
                _cm_matched(
                    _cm_list_entry(100000512, "Example list", "BUILDING", 100000502),
                    _cm_list_entry(100000501, "Older list", "READY"),
                )
            ],
        ),
    )
    for operation in (
        "cm_create_trigger.click",
        "cm_menu_item_first.wait_for",
        "cm_menu_item_matched.click",
        "cm_upload_dialog_first.wait_for",
        "cm_list_name.focus",
        "cm_list_name.select_text",
        "cm_list_file.set_input_files",
    ):
        page.script(operation, None)
    page.script("cm_create_trigger.count", 1)
    page.script("cm_create_trigger.get_attribute", "hue-menu-1")
    page.script("cm_menu_items.count", 3)
    page.script("cm_upload_dialog.count", 1, 1)
    for semantic_id in (
        "cm_list_name",
        "cm_company_option",
        "cm_list_file",
        "cm_terms_link",
    ):
        page.script(f"{semantic_id}.count", 1)
    page.script("cm_terms_link.get_attribute", "/legal/sas-terms")
    page.script("cm_list_name.input_value", "Example list")
    page.script("cm_list_type.select_option", ["COMPANY"])
    page.script("cm_list_type.input_value", "COMPANY")
    page.script("cm_upload_submit.is_visible", True)
    page.script("cm_upload_submit.is_enabled", True, True)
    page.script("cm_upload_submit.click", submit)
    return await _cm_write_run(
        page,
        "create_company_list_audience",
        {
            "account_id": _CM_ACCOUNT,
            "name": "Example list",
            "companies": [
                {"name": "Example company", "website": "https://example.com"}
            ],
            "confirm_send": True,
        },
        lambda extractor: extractor.create_company_list_audience(
            _CM_ACCOUNT,
            "Example list",
            [{"name": "Example company", "website": "https://example.com"}],
            confirm_send=True,
        ),
    )


_CM_TITLES = "urn:li:adTargetingFacet:titles"


def _cm_titles(*urns: str) -> dict[str, Any]:
    return {
        "include": {
            "and": [
                {
                    "or": [
                        {
                            "facet": {"urn": _CM_TITLES, "name": "Job titles"},
                            "segments": [
                                {"urn": urn, "name": f"Example title {urn[-3:]}"}
                                for urn in urns
                            ],
                        }
                    ]
                }
            ]
        },
        "exclude": {"or": []},
    }


def _cm_typeahead(query: str) -> ScriptedCmResponse:
    return _cm_response(
        "campaignManagerAdTargetingEntities?q=queryAndMultiFacetTypeahead"
        f"&query={query}&facets=List(urn%3Ali%3AadTargetingFacet%3Atitles)",
        {
            "elements": [
                {
                    "urn": "urn:li:title:300",
                    "name": "Example title 300",
                    "facetUrn": _CM_TITLES,
                    "targetable": True,
                },
                {
                    "urn": "urn:li:title:301",
                    "name": "Example title 301",
                    "facetUrn": _CM_TITLES,
                    "targetable": False,
                },
            ]
        },
    )


_CM_TYPEAHEAD_ROWS = {
    "rows": 2,
    "ordered": True,
    "disabled": [False, True],
    "checked": [False, False],
}


async def _cm_search_targeting_scenario() -> dict[str, Any]:
    page = _cm_write_page("search_targeting_entities__titles")
    record = _cm_campaign_record(targetingCriteria=_cm_titles("urn:li:title:100"))
    page.script("goto", lambda: _cm_emit(page, [record]))
    page.script(
        "evaluate:cm_pill",
        {"state": "clicked", "index": 0},
        {"open": [0], "pills": 1},
    )
    page.script(
        "evaluate:cm_typeahead_rows",
        {"ok": True, "length": 0},
        _CM_TYPEAHEAD_ROWS,
        {"ok": True, "length": 8},
    )
    page.script("keyboard.type", lambda: _cm_emit(page, [_cm_typeahead("engineer")]))
    trace = await _cm_write_run(
        page,
        "search_targeting_entities",
        {"account_id": _CM_ACCOUNT, "campaign_id": _CM_CAMPAIGN, "query": "engineer"},
        lambda extractor: extractor.search_targeting_entities(
            _CM_ACCOUNT, _CM_CAMPAIGN, "engineer"
        ),
    )
    trace["result"] = _complete_mapping_result(
        trace["result"], section_names=list(trace["result"]["sections"])
    )
    return trace


async def _cm_update_targeting_scenario() -> dict[str, Any]:
    """Remove a pill by its record position, add a row by its answer position."""
    page = _cm_write_page("update_campaign_targeting__draft")
    original = _cm_titles("urn:li:title:100", "urn:li:title:200")
    final = _cm_titles("urn:li:title:100", "urn:li:title:300")

    def criteria(*urns: str) -> dict[str, Any]:
        # The app saves targeting undecorated: facet URN to value URNs.
        return {
            "include": {"and": [{"or": {_CM_TITLES: list(urns)}}]},
            "exclude": {"or": {}},
        }

    async def remove() -> dict[str, bool]:
        await _cm_app_sends(
            page, _cm_patch(targetingCriteria=criteria("urn:li:title:100"))
        )
        return {"clicked": True}

    async def select() -> dict[str, bool]:
        await _cm_app_sends(
            page,
            _cm_patch(
                targetingCriteria=criteria("urn:li:title:100", "urn:li:title:300")
            ),
        )
        return {"clicked": True}

    page.script(
        "goto",
        lambda: _cm_emit(page, [_cm_campaign_record(targetingCriteria=original)]),
        lambda: _cm_emit(page, [_cm_campaign_record(targetingCriteria=final)]),
    )
    page.script(
        "evaluate:cm_pill",
        # Read phase: open the block, find the value, check the block's pills.
        {"state": "clicked", "index": 0},
        {"open": [0], "pills": 2},
        {"open": [0], "pills": 2},
        {"open": [0], "pills": 2},
        # Apply phase: remove the second pill, then add the found row.
        {"open": [0], "pills": 2},
        {"open": [0], "pills": 2},
        remove,
        {"open": [0], "pills": 1},
        {"open": [0], "pills": 1},
        {"open": [0], "pills": 1},
        {"open": [0], "pills": 2},
    )
    page.script(
        "evaluate:cm_typeahead_rows",
        {"ok": True, "length": 0},
        _CM_TYPEAHEAD_ROWS,
        {"ok": True, "length": 8},
        {"ok": True, "length": 0},
        _CM_TYPEAHEAD_ROWS,
        select,
        {"ok": True, "length": 8},
    )
    page.script(
        "keyboard.type",
        lambda: _cm_emit(page, [_cm_typeahead("engineer")]),
        lambda: _cm_emit(page, [_cm_typeahead("engineer")]),
    )
    # The editor that autosaves a draft has no classic footer.
    page.script("evaluate:cm_footer", 0)
    add = [{"urn": "urn:li:title:300", "query": "engineer"}]
    return await _cm_write_run(
        page,
        "update_campaign_targeting",
        {
            "account_id": _CM_ACCOUNT,
            "campaign_id": _CM_CAMPAIGN,
            "add": add,
            "remove": ["urn:li:title:200"],
            "confirm_send": True,
        },
        lambda extractor: extractor.update_campaign_targeting(
            _CM_ACCOUNT,
            _CM_CAMPAIGN,
            add=add,
            remove=["urn:li:title:200"],
            confirm_send=True,
        ),
    )


async def _cm_saved_audience_scenario() -> dict[str, Any]:
    page = _cm_write_page("create_saved_audience__saved")
    owner = f"urn:li:sponsoredAccount:{_CM_ACCOUNT}"
    page.declare_locator("#targeting-module-save-audience-button", "cm_save_button")
    page.declare_locator(
        "[role=dialog][aria-labelledby=save-audience-modal__header]", "cm_save_dialog"
    )
    page.declare_derived("cm_save_dialog", "first", "cm_save_dialog_first")
    page.declare_locator(
        "input#audience-name", "cm_saved_name", parent="cm_save_dialog"
    )
    page.declare_locator(
        ".modal-footer--with-buttons", "cm_save_footer", parent="cm_save_dialog"
    )
    page.declare_locator("button", "cm_save_footer_buttons", parent="cm_save_footer")
    page.declare_derived("cm_save_footer_buttons", "last", "cm_save_submit")
    page.declare_locator(
        '[id$="-tab-accounts.item.audiences.saved.index"]', "cm_saved_tab"
    )

    def saved_list(*elements: dict[str, Any]) -> ScriptedCmResponse:
        return _cm_response(
            f"campaignManagerTargetingCriteria?q=member&accountId={_CM_ACCOUNT}",
            {"elements": list(elements), "paging": {"count": 10, "start": 0}},
        )

    async def submit() -> None:
        create = _cm_write(
            "campaignManagerTargetingCriteria",
            {
                "account": owner,
                "name": "Example audience",
                "ownership": "ACCOUNT",
                "include": {"and": [{"or": {_CM_TITLES: ["urn:li:title:100"]}}]},
                "exclude": {"or": {}},
            },
            restli="create",
            created_id="100000602",
        )
        await _cm_app_sends(page, create)

    record = _cm_campaign_record(targetingCriteria=_cm_titles("urn:li:title:100"))
    page.script(
        "goto",
        lambda: _cm_emit(page, [record]),
        lambda: _cm_emit(page, [_cm_matched()]),
    )
    for operation in (
        "cm_save_button.click",
        "cm_save_dialog_first.wait_for",
        "cm_saved_name.focus",
        "cm_saved_name.select_text",
    ):
        page.script(operation, None)
    page.script("cm_save_button.count", 1)
    page.script("cm_save_button.is_enabled", True)
    page.script("cm_save_dialog.count", 1, 1)
    page.script("cm_saved_name.count", 1)
    page.script("cm_save_footer.count", 1)
    page.script("cm_save_footer_buttons.count", 2)
    page.script("cm_saved_name.input_value", "Example audience")
    page.script("cm_save_submit.is_visible", True)
    page.script("cm_save_submit.is_enabled", True, True)
    page.script("cm_save_submit.click", submit)
    page.script("cm_saved_tab.count", 1)
    page.script(
        "cm_saved_tab.click",
        lambda: _cm_emit(
            page,
            [
                saved_list(
                    {
                        "type": "USER_AUDIENCE",
                        "id": 100000602,
                        "name": "Example audience",
                        "ownership": "ACCOUNT",
                        "audienceCount": 4000,
                    }
                )
            ],
        ),
    )
    return await _cm_write_run(
        page,
        "create_saved_audience",
        {
            "account_id": _CM_ACCOUNT,
            "campaign_id": _CM_CAMPAIGN,
            "name": "Example audience",
            "confirm_send": True,
        },
        lambda extractor: extractor.create_saved_audience(
            _CM_ACCOUNT, _CM_CAMPAIGN, "Example audience", confirm_send=True
        ),
    )


async def build_policy_traces() -> dict[str, dict[str, Any]]:
    traces = {
        "facade-contract.json": await _facade_contract_trace(),
        "generic-ordinary.json": await _generic_capture_scenario(
            "extract_page__ordinary", "https://www.linkedin.com/in/ada-lovelace/"
        ),
        "generic-activity.json": await _generic_capture_scenario(
            "extract_page__activity",
            "https://www.linkedin.com/in/ada-lovelace/recent-activity/all/",
        ),
        "generic-search.json": await _generic_capture_scenario(
            "extract_page__search",
            "https://www.linkedin.com/search/results/people/?keywords=ada",
        ),
        "generic-company-people.json": await _generic_capture_scenario(
            "extract_page__company_people",
            "https://www.linkedin.com/company/analytical-engine/people/",
        ),
        "person-sections.json": await _person_sections_scenario(),
        "company-sections.json": await _company_sections_scenario(),
        "job-search.json": await _job_search_scenario(),
        "job-search-route-alias.json": await _job_search_scenario(
            "/jobs/search-results/"
        ),
        "job-search-metadata-upgrade.json": await _job_search_upgrade_scenario(),
        "saved-jobs.json": await _saved_jobs_scenario(),
        "feed-stale.json": await _feed_stale_scenario(),
        "feed-response-success.json": await _feed_response_scenario(body_failure=False),
        "feed-response-failure.json": await _feed_response_scenario(body_failure=True),
        "message-target-unavailable.json": await _message_target_scenario(
            "unavailable"
        ),
        "message-target-unresolved.json": await _message_target_scenario("unresolved"),
        "message-dry-run.json": await _messaging_dry_run_scenario(),
        "message-composer-occupied.json": await _occupied_message_scenario(
            restored_during_write=False
        ),
        "message-composer-restored.json": await _occupied_message_scenario(
            restored_during_write=True
        ),
        "message-pre-submit-cleanup.json": await _messaging_submission_scenario(
            "pre_submit_cleanup"
        ),
        "message-submit-rejected.json": await _messaging_submission_scenario(
            "submission_rejected"
        ),
        "message-submit-interrupted.json": await _messaging_submission_scenario(
            "submission_interrupted"
        ),
        "message-unconfirmed.json": await _messaging_submission_scenario("unconfirmed"),
        "message-sent.json": await _messaging_submission_scenario("sent"),
        "message-cancelled.json": await _messaging_cancellation_scenario(),
        "message-blank.json": await _invalid_message_scenario("   ", "blank"),
        "message-c0.json": await _invalid_message_scenario("line\x01break", "c0"),
        "message-del.json": await _invalid_message_scenario("text\x7f", "del"),
        "reply-dry-run.json": await _reply_scenario("dry_run"),
        "reply-sent.json": await _reply_scenario("sent"),
        "reply-other-thread.json": await _reply_scenario("other_thread"),
        "connect.json": await _connect_scenario(),
        "get-my-profile.json": await _get_my_profile_scenario(),
        "withdraw-invitations.json": await _withdraw_invitations_scenario(),
        "sidebar-profiles.json": await _sidebar_scenario(),
        "company-employees.json": await _single_capture_facade_scenario(
            "get_company_employees"
        ),
        "scrape-job.json": await _single_capture_facade_scenario("scrape_job"),
        "scrape-job-error.json": await _single_capture_error_scenario(),
        "scrape-job-description-missing.json": await _description_missing_scenario(),
        "search-people.json": await _single_capture_facade_scenario("search_people"),
        "search-companies.json": await _single_capture_facade_scenario(
            "search_companies"
        ),
        "search-posts.json": await _single_capture_facade_scenario("search_posts"),
        "inbox.json": await _conversation_scenario("get_inbox"),
        "conversation.json": await _conversation_scenario("get_conversation"),
        "search-conversations.json": await _conversation_scenario(
            "search_conversations"
        ),
        "cm-ad-accounts.json": await _cm_ad_accounts_scenario(),
        "cm-campaigns.json": await _cm_campaigns_scenario(setup_redirect=False),
        "cm-campaigns-setup-redirect.json": await _cm_campaigns_scenario(
            setup_redirect=True
        ),
        "cm-campaign.json": await _cm_campaign_scenario(),
        "cm-analytics.json": await _cm_analytics_scenario(),
        "cm-update-campaign-dry-run.json": await _cm_update_campaign_scenario(
            "dry_run"
        ),
        "cm-update-campaign-draft.json": await _cm_update_campaign_scenario("draft"),
        "cm-update-campaign-blocked.json": await _cm_update_campaign_scenario(
            "blocked"
        ),
        "cm-set-status-pause.json": await _cm_set_status_scenario(),
        "cm-search-targeting.json": await _cm_search_targeting_scenario(),
        "cm-update-targeting.json": await _cm_update_targeting_scenario(),
        "cm-create-draft.json": await _cm_create_draft_scenario(),
        "cm-add-post.json": await _cm_add_post_scenario(),
        "cm-list-audiences.json": await _cm_list_audiences_scenario(),
        "cm-list-sponsorable-posts.json": await _cm_list_sponsorable_posts_scenario(),
        "cm-audience-upload.json": await _cm_audience_upload_scenario(),
        "cm-saved-audience.json": await _cm_saved_audience_scenario(),
        "sn-account-search.json": await _sn_account_search_scenario(),
        "sn-lead-search.json": await _sn_lead_search_scenario(),
        "sn-account.json": await _sn_account_scenario(),
        "sn-account-lists.json": await _sn_account_lists_scenario(),
        "sn-account-list.json": await _sn_account_list_scenario(),
        "sn-lead-lists.json": await _sn_lead_lists_scenario(),
        "sn-lead-list.json": await _sn_lead_list_scenario(),
        "sn-no-seat.json": await _sn_no_seat_scenario(),
    }
    return traces


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def policy_trace_diff(
    generated: dict[str, dict[str, Any]], trace_root: Path = TRACE_ROOT
) -> str:
    """Return one deterministic unified comparison against canonical traces."""

    generated_names = set(generated)
    fixture_names = {path.name for path in trace_root.glob("*.json")}
    chunks = [
        f"missing canonical trace: {trace_root / name}\n"
        for name in sorted(generated_names - fixture_names)
    ]
    chunks.extend(
        f"unexpected canonical trace: {trace_root / name}\n"
        for name in sorted(fixture_names - generated_names)
    )
    for name in sorted(generated_names & fixture_names):
        path = trace_root / name
        expected = path.read_text(encoding="utf-8")
        actual = canonical_json(generated[name])
        chunks.extend(
            unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=str(path),
                tofile=f"generated/{name}",
            )
        )
    return "".join(chunks)
