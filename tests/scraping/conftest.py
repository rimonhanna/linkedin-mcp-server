"""Fixtures shared by the scraping unit tests."""

from __future__ import annotations

import pytest

from linkedin_mcp_server.scraping import navigation as navigation_module


@pytest.fixture(autouse=True)
def reprobe_reads_the_same_barrier(monkeypatch):
    """The /feed/ re-probe in core.auth sees whatever a test patched here.

    Tests patch the names ``navigation`` imported, while the second look runs
    inside ``core.auth.barrier_confirmed`` and would otherwise read the mock
    page for real, where a page that "keeps showing a picker" shows nothing.
    Either detector counts: the page under test is stateless, so a barrier the
    full read reports is one the re-load would show again.
    """

    async def delegate(page):
        return await navigation_module.detect_auth_barrier_quick(
            page
        ) or await navigation_module.detect_auth_barrier(page)

    monkeypatch.setattr(
        "linkedin_mcp_server.core.auth.detect_auth_barrier_quick", delegate
    )
