"""Tests for core utility functions (rate-limit detection, scrolling, modals)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    OffLinkedInLandingError,
    RateLimitError,
)
from linkedin_mcp_server.core.utils import detect_rate_limit, scroll_job_sidebar
from linkedin_mcp_server.pacing import JobStore, read_account_cooldown


def _last_signal(tmp_path) -> str | None:
    """The throttle signal the detector left in the (isolated) account ledger."""
    last = read_account_cooldown(JobStore(tmp_path / "jobs")).last_signal
    return last["signal"] if last else None


@pytest.fixture
def mock_page():
    """Create a mock Patchright page for rate-limit tests."""
    page = MagicMock()
    page.url = "https://www.linkedin.com/in/testuser/details/experience/"

    mock_locator = MagicMock()
    mock_locator.count = AsyncMock(return_value=0)
    mock_locator.inner_text = AsyncMock(return_value="")
    page.locator = MagicMock(return_value=mock_locator)
    return page


class TestDetectRateLimit:
    async def test_checkpoint_url_raises(self, mock_page, tmp_path):
        mock_page.url = "https://www.linkedin.com/checkpoint/challenge/123"
        with patch(
            "linkedin_mcp_server.core.utils.barrier_confirmed",
            AsyncMock(return_value=True),
        ):
            with pytest.raises(AuthenticationError, match="re-authentication"):
                await detect_rate_limit(mock_page)
        assert _last_signal(tmp_path) is None

    async def test_authwall_url_raises(self, mock_page, tmp_path):
        mock_page.url = "https://www.linkedin.com/authwall?trk=login"
        with patch(
            "linkedin_mcp_server.core.utils.barrier_confirmed",
            AsyncMock(return_value=True),
        ):
            with pytest.raises(AuthenticationError, match="re-authentication"):
                await detect_rate_limit(mock_page)
        # But records nothing: an authwall is the session having ended, not
        # LinkedIn throttling it, and a pause would only delay the re-login.
        assert _last_signal(tmp_path) is None

    async def test_normal_page_with_main_skips_body_heuristic(
        self, mock_page, tmp_path
    ):
        """A normal page with <main> should NOT trigger body text checks."""
        main_locator = MagicMock()
        main_locator.count = AsyncMock(return_value=1)

        body_locator = MagicMock()
        # Body contains a phrase that would false-positive
        body_locator.inner_text = AsyncMock(
            return_value="Helping SaaS teams slow down churn with data-driven retention"
        )

        def locator_side_effect(selector):
            if selector == "main":
                return main_locator
            if selector == "body":
                return body_locator
            return MagicMock(count=AsyncMock(return_value=0))

        mock_page.locator = MagicMock(side_effect=locator_side_effect)
        # Should NOT raise — the page has <main>, so body heuristic is skipped
        await detect_rate_limit(mock_page)
        assert _last_signal(tmp_path) is None

    async def test_error_page_without_main_triggers_heuristic(
        self, mock_page, tmp_path
    ):
        """A short error page without <main> with rate-limit text should raise."""
        main_locator = MagicMock()
        main_locator.count = AsyncMock(return_value=0)

        body_locator = MagicMock()
        body_locator.inner_text = AsyncMock(
            return_value="Too many requests. Slow down."
        )

        def locator_side_effect(selector):
            if selector == "main":
                return main_locator
            if selector == "body":
                return body_locator
            return MagicMock(count=AsyncMock(return_value=0))

        mock_page.locator = MagicMock(side_effect=locator_side_effect)
        with pytest.raises(RateLimitError, match="Rate limit message"):
            await detect_rate_limit(mock_page)
        assert _last_signal(tmp_path) == "rate_limit_page"

    async def test_long_body_without_main_does_not_trigger(self, mock_page):
        """A page without <main> but with long body text (>2000 chars) is not an error page."""
        main_locator = MagicMock()
        main_locator.count = AsyncMock(return_value=0)

        body_locator = MagicMock()
        # Long body with a matching phrase buried in content
        body_locator.inner_text = AsyncMock(
            return_value="x" * 2000 + " try again later"
        )

        def locator_side_effect(selector):
            if selector == "main":
                return main_locator
            if selector == "body":
                return body_locator
            return MagicMock(count=AsyncMock(return_value=0))

        mock_page.locator = MagicMock(side_effect=locator_side_effect)
        # Should NOT raise — body is too long to be an error page
        await detect_rate_limit(mock_page)

    @pytest.mark.parametrize(
        "url",
        [
            "https://portal.invalid/checkpoint/challenge/",
            "https://evil-linkedin.com/in/testuser/",
            "http://www.linkedin.com/in/testuser/",
        ],
    )
    async def test_a_page_linkedin_did_not_serve_is_refused(
        self, mock_page, tmp_path, url: str
    ):
        """Its text is not the section asked for, and its route is no barrier."""
        mock_page.url = url
        mock_page.locator.return_value.inner_text = AsyncMock(
            return_value="Too many requests"
        )
        with patch(
            "linkedin_mcp_server.core.utils.barrier_confirmed",
            AsyncMock(return_value=True),
        ) as confirmed:
            with pytest.raises(OffLinkedInLandingError):
                await detect_rate_limit(mock_page)
        confirmed.assert_not_awaited()
        assert _last_signal(tmp_path) is None

    async def test_normal_url_no_error_passes(self, mock_page):
        """A clean normal page passes all checks without raising."""
        main_locator = MagicMock()
        main_locator.count = AsyncMock(return_value=1)

        def locator_side_effect(selector):
            if selector == "main":
                return main_locator
            return MagicMock(count=AsyncMock(return_value=0))

        mock_page.locator = MagicMock(side_effect=locator_side_effect)
        await detect_rate_limit(mock_page)


class TestScrollDeadline:
    """A spent scroll budget must not turn into no deadline at all."""

    @staticmethod
    def _page() -> MagicMock:
        page = MagicMock()
        page.url = "https://www.linkedin.com/jobs/search/?keywords=python"
        page.wait_for_selector = AsyncMock()
        page.evaluate = AsyncMock(return_value={"status": "gone"})
        return page

    async def test_a_spent_budget_skips_the_scroll(self):
        """Patchright reads a zero timeout as no timeout.

        A search that has spent its budget would then wait on a page with no
        job card until the tool is cancelled, and cancellation throws away
        every page gathered before it.
        """
        page = self._page()

        assert await scroll_job_sidebar(page, deadline=0) is False
        page.wait_for_selector.assert_not_called()

    async def test_a_sliver_of_budget_is_still_a_timeout(self):
        """`int(0.0004 * 1000)` is zero, which the guard above does not catch."""
        page = self._page()

        await scroll_job_sidebar(page, deadline=0.0004)

        assert page.wait_for_selector.await_args.kwargs["timeout"] == 1

    async def test_a_deadline_spent_releasing_the_rail_still_ends_the_call(self):
        """Releasing the rail handle is shielded, so a tool deadline that falls
        inside it would otherwise be swallowed and the scroll return normally
        after the caller's time was up."""
        import asyncio

        import anyio

        page = self._page()
        holder = MagicMock()

        async def slow_dispose() -> None:
            await asyncio.sleep(0.3)

        holder.dispose = AsyncMock(side_effect=slow_dispose)
        page.evaluate_handle = AsyncMock(return_value=holder)

        with pytest.raises(TimeoutError):
            with anyio.fail_after(0.1):
                await scroll_job_sidebar(page)

        holder.dispose.assert_awaited_once()


class TestCancelledSidebarScroll:
    """A cancel ends the scroll between steps, after a sent scroll has run.

    The browser-DOM suite proves it against a real renderer; this pins the
    order of operations where `page.evaluate` is mocked.
    """

    async def test_a_cancel_waits_for_the_sent_scroll_and_sends_nothing_after(
        self,
    ):
        import asyncio

        page = MagicMock()
        page.url = "https://www.linkedin.com/jobs/search/?keywords=python"
        page.wait_for_selector = AsyncMock()
        holder = MagicMock()
        holder.dispose = AsyncMock()
        page.evaluate_handle = AsyncMock(return_value=holder)
        scroll_sent = asyncio.Event()
        scroll_landed = asyncio.Event()
        steps: list[bool] = []

        async def step(_program, arg):
            steps.append(arg["scroll"])
            if arg["scroll"]:
                scroll_sent.set()
                await asyncio.sleep(0.2)
                scroll_landed.set()
            return {"status": "ok", "cards": 5, "height": 200}

        page.evaluate = AsyncMock(side_effect=step)

        task = asyncio.create_task(scroll_job_sidebar(page))
        await scroll_sent.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert scroll_landed.is_set()
        assert steps == [False, True]
        holder.dispose.assert_awaited_once()


class TestNavigatedSidebarScroll:
    async def test_a_step_from_another_document_ends_the_scroll(self):
        """Detected from the document that answered, not from a step raising.

        The rail handle of the old document makes the next step raise today,
        but a step that a new document answers must stop the scroll whether
        it raises or not, and report it as the caller's page having moved.
        """
        page = MagicMock()
        page.url = "https://www.linkedin.com/jobs/search/?keywords=python"
        page.wait_for_selector = AsyncMock()
        holder = MagicMock()
        holder.dispose = AsyncMock()
        page.evaluate_handle = AsyncMock(return_value=holder)
        expected: list[float | None] = []
        document = {"origin": 1000.0}

        async def step(_program, arg):
            # The page's side of the step: a document other than the one
            # named answers without measuring or scrolling.
            expected.append(arg["expected"])
            if arg["expected"] is not None and arg["expected"] != document["origin"]:
                return {"status": "navigated", "origin": document["origin"]}
            # Navigated right after the first look.
            answer = {"status": "ok", "cards": 5, "height": 200, **document}
            document["origin"] = 2000.0
            return answer

        page.evaluate = AsyncMock(side_effect=step)

        assert await scroll_job_sidebar(page) is True
        # Every step after the first names the first document, and the first
        # one the new document answered ended the call.
        assert expected == [None, 1000.0]
        holder.dispose.assert_awaited_once()


class TestScrollToBottomToleratesAStaleRound:
    """One unchanged height is a slow batch, not the bottom of the page.

    Blocking images made this reachable rather than created it: without their
    boxes the document is shorter, so the scroll arrives at the true bottom in
    fewer rounds and asks the question while a batch is still in flight.
    """

    async def test_a_single_stale_round_does_not_stop_the_scroll(self):
        from linkedin_mcp_server.core.utils import scroll_to_bottom

        # Height stalls once, then grows again: a batch that landed late.
        heights = [1000, 1000, 1000, 1000, 2000, 2000, 3000, 3000]
        page = MagicMock()
        page.evaluate = AsyncMock(
            side_effect=lambda *_: heights.pop(0) if heights else 3000
        )

        await scroll_to_bottom(page, pause_time=0.01, max_scrolls=4)

        # Four rounds ran; a version that broke on the first stale round would
        # have stopped after the first and left the later batches unread.
        assert not heights

    async def test_two_stale_rounds_do_stop_the_scroll(self):
        from linkedin_mcp_server.core.utils import scroll_to_bottom

        page = MagicMock()
        page.evaluate = AsyncMock(return_value=1000)

        await scroll_to_bottom(page, pause_time=0.01, max_scrolls=10)

        # 2 evaluates per round (read, scroll, read = 3 calls per round).
        # Stopping after the second stale round means 6 calls, not 30.
        assert page.evaluate.await_count == 6
