"""A save request stopped in the browser says which filter differed."""

from __future__ import annotations

import pytest

from test_sales_navigator_saved_search_dom import _save, _spent, dom  # noqa: F401

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]


async def test_a_refused_create_names_the_filter_that_differed(dom):  # noqa: F811
    dom.create_body["searchQuery"]["leadSearchQuery"]["filters"].pop(1)

    result, _ = await _save(dom)

    assert result["status"] == "not_sent", result
    assert (
        "(body differs from the plan: it lacks the asked filters: "
        "COMPANY_HEADQUARTERS)" in result["message"]
    ), result["message"]
    assert dom.creates == [] and _spent(dom) == 0
