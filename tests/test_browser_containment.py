"""One real browser launch, contained and provably drained, per platform.

Everything else about containment is measured against process doubles or
against a plain child tree. This file draws a real Chromium through the real
``BrowserManager`` and asks the one question those cannot: does the launch's own
attribution actually cover the browser?

That question has a different answer on each platform, which is why the file
runs on all of them. POSIX attributes by an environment marker the browser
carries and scans for it. Windows has no such marker -- an environment block
belongs to its own process, and reading another one's takes the debugger APIs
-- so a Job assigned to the Node driver before it spawns anything is the whole
of the attribution there, and whether Chromium really joins that Job is a fact
about Windows and Chromium that only Windows can answer.

**Whether a browser is installed is settled before the launch, and nothing
after it may turn into a skip.** The obvious shape, a ``try``/``except`` around
``manager.start()`` that skips on any error, would have swallowed the exact
failure this file exists to block: a Windows Job that cannot be created, cannot
be assigned, or that Chromium refuses to nest under all raise out of ``start()``
and would have been reported as "no browser installed". So readiness is asked
first, through the product's own resolution of the binary, and from there every
error fails.

CI installs a browser and then runs this file in a step of its own, because the
platform legs run their process tests before the browser exists. A missing
browser is a failure there for the same reason it is a skip here: in CI the
install step is what was supposed to provide it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path
from typing import Any

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.browser_launch import build_launch_options
from linkedin_mcp_server.config.schema import BrowserConfig
from linkedin_mcp_server.core.browser import BrowserManager
from linkedin_mcp_server.drivers import browser as browser_module
from linkedin_mcp_server.process_tree import _scan_marked_posix_processes


def _running_in_ci() -> bool:
    """Whether a missing browser is a failure rather than a reason to skip."""
    return os.environ.get("CI", "").lower() in {"1", "true", "yes"}


def _unavailable(reason: str) -> None:
    """Fail in CI, skip locally. Never quietly pass."""
    if _running_in_ci():
        pytest.fail(
            f"{reason}. In CI this is a failure rather than a skip: the job "
            f"installs a browser before this step precisely so this gate can "
            f"run, and a gate that skips itself is not a gate."
        )
    pytest.skip(f"{reason}; run `uv run patchright install chromium --no-shell`")


def _manager(profile: Path) -> BrowserManager:
    """Build the browser the product builds.

    The options come from ``build_launch_options`` rather than being written out
    here, because a gate that assembles its own launch measures a browser nobody
    ships -- and in particular it would leave out ``channel="chromium"``, which
    is what stops Playwright resolving the headless shell that ``--no-shell``
    never installed. ``BrowserConfig()`` rather than ``get_config()`` keeps the
    measurement independent of process-global test state.
    """
    launch_options, viewport = build_launch_options(BrowserConfig())
    return BrowserManager(
        user_data_dir=profile,
        headless=True,
        viewport=viewport,
        **launch_options,
    )


async def _the_browser_this_launch_would_use(probe: BrowserManager) -> str | None:
    """Resolve the binary the way the launch under test will resolve it.

    ``_executable_about_to_run`` is the product's own answer, the one handed to
    ``refuse_a_downgrade`` on every start, and it honours an operator
    ``executable_path``, the configured channel and ``PLAYWRIGHT_BROWSERS_PATH``
    alike. Asking it needs a driver, so *probe* is a second manager built from
    the same options: nothing here touches the one the test is about, and a
    driver started here cannot be mistaken for the launch's own.

    Which cache it looks in is settled for both of us at once, and not by this
    function. ``reset_bootstrap_for_testing`` clears
    ``PLAYWRIGHT_BROWSERS_PATH`` before every test, so this probe and the launch
    under test both resolve patchright's default cache -- which is where a bare
    ``patchright install`` puts a browser, locally and in CI alike.
    """
    playwright = await async_playwright().start()
    try:
        probe._playwright = playwright
        return probe._executable_about_to_run()
    finally:
        probe._playwright = None
        await playwright.stop()


def _windows_job_members(job: Any) -> tuple[int, ...]:
    import importlib

    win32job = importlib.import_module("win32job")
    handle = job.job_handle
    assert handle is not None, "the launch Job was released before it was read"
    members = win32job.QueryInformationJobObject(
        handle, win32job.JobObjectBasicProcessIdList
    )
    return tuple(int(entry) for entry in members if entry is not None)


async def _require_a_browser(tmp_path: Path) -> None:
    executable = await _the_browser_this_launch_would_use(
        _manager(tmp_path / "probe-profile")
    )
    if executable is None:
        _unavailable("the browser executable could not be resolved")
    elif not Path(executable).exists():
        _unavailable(f"no browser installed at {executable}")


def _launch_is_drained(manager: BrowserManager) -> bool:
    if os.name == "nt":
        job = manager._containment
        return job is not None and job.closed and job.drained
    return not _scan_marked_posix_processes(manager._process_marker).processes


async def test_a_real_browser_launch_is_attributed_and_provably_drained(tmp_path):
    """The launch owns its processes, and the close proves they went."""
    await _require_a_browser(tmp_path)

    # Past this line nothing is allowed to skip. A Job that cannot be created,
    # cannot be assigned, or that Chromium will not nest under raises out of
    # start(), and those are the failures this file is here to report.
    manager = _manager(tmp_path / "profile")
    await manager.start()

    closed = False
    try:
        if os.name == "nt":
            containment = manager._containment
            assert containment is not None, "the launch was never contained"
            members = _windows_job_members(containment)
            # More than the Node driver. Job membership is inherited at process
            # creation, so this is the measurement that says assigning the
            # driver is enough to reach the browser it launches next.
            assert len(members) >= 2, (
                f"Chromium did not join the launch Job (members={members})"
            )
        else:
            assert manager._containment is None, "POSIX grew a Windows Job"
            marked = _scan_marked_posix_processes(manager._process_marker).processes
            assert marked, "no process carried this launch's marker"

        closed = await manager.close()
        assert closed is True, "the close could not prove the launch had gone"

        if os.name == "nt":
            assert manager._containment is not None
            assert manager._containment.closed
            assert manager._containment.drained
        else:
            assert not _scan_marked_posix_processes(manager._process_marker).processes
    finally:
        if not closed:
            await manager.close()


async def test_a_real_renderer_crash_is_closed_and_replaced(tmp_path, monkeypatch):
    """A renderer crash leaves the page open and Chromium running.

    The unit tests take that, and the ``crash`` event, on trust. This is where
    a real renderer is killed: the manager has to notice, the cached singleton
    has to read as dead although nothing disconnected, and the call that finds
    it has to drain the old launch before handing out a working replacement.
    Creation is the one substitute, because the real one signs in to LinkedIn.
    """
    await _require_a_browser(tmp_path)

    first = _manager(tmp_path / "profile")
    await first.start()
    replacements: list[BrowserManager] = []
    try:
        page = first.page
        with contextlib.suppress(Exception):
            await page.goto("chrome://crash", timeout=10_000)
        deadline = time.monotonic() + 10
        while not first.page_crashed and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

        assert first.page_crashed, "the renderer crash never reached the manager"
        assert not page.is_closed(), "the crash closed the page"
        chromium = first.context.browser
        assert chromium is None or chromium.is_connected(), (
            "the crash disconnected the browser, so it is not the case under test"
        )
        assert not _launch_is_drained(first), "the crash ended Chromium"

        async def create() -> BrowserManager:
            fresh = _manager(tmp_path / "profile")
            await fresh.start()
            replacements.append(fresh)
            browser_module._browser = fresh
            return fresh

        verdicts: list[bool] = []
        real_close = first.close

        async def close() -> bool:
            verdicts.append(await real_close())
            return verdicts[-1]

        monkeypatch.setattr(first, "close", close)
        monkeypatch.setattr(browser_module, "_browser", first)
        monkeypatch.setattr(browser_module, "_browser_lease", None)
        monkeypatch.setattr(browser_module, "_browser_cookie_export_path", None)
        monkeypatch.setattr(browser_module, "_create_browser", create)

        fresh = await browser_module.get_or_create_browser()

        assert fresh is not first
        assert verdicts == [True], "the crashed launch was not closed provably"
        assert _launch_is_drained(first), "the crashed Chromium outlived its close"
        assert fresh.page_crashed is False
        await fresh.page.goto("data:text/html,<p>alive</p>")
        assert await fresh.page.evaluate("1 + 1") == 2
    finally:
        await first.close()
        for fresh in replacements:
            assert await fresh.close() is True
