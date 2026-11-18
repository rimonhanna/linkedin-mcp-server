"""Browser-DOM tests: Confirm is found by the footer's structure, not DOM order.

The app's footer gives Cancel the class ``mr2`` and Confirm none. The
stand-ins are synthetic; nothing reaches LinkedIn; skipped when chromium is
not installed.
"""

from __future__ import annotations

from typing import Any

import pytest

from linkedin_mcp_server.scraping.campaign_manager_political import political_dialog

from .test_campaign_manager_political_dom import _log, dom_page  # noqa: F401

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]


def _dialog(footer: str) -> str:
    return f"""
<div role="dialog" aria-labelledby="political-ad-self-declaration-modal">
  <h2 id="political-ad-self-declaration-modal">Question</h2>
  <input type="checkbox" id="political-ad-self-declaration">
  <div class="modal-footer--with-buttons">{footer}</div>
</div>
<script>
const box = document.querySelector('[role=dialog] input');
function log(entry) {{
    document.body.dataset.log = (document.body.dataset.log || '') + entry + '|';
}}
document.getElementById('cancel').addEventListener('click', () => log('cancel'));
document.getElementById('confirm').addEventListener('click', () => {{
    log('confirm:' + (box.checked ? 'NOT_POLITICAL' : 'POLITICAL'));
}});
</script>
"""


async def test_confirm_is_the_button_outside_cancel_whatever_its_position(
    dom_page: Any,  # noqa: F811
):
    await dom_page.set_content(
        _dialog(
            '<button id="confirm">Confirm</button>'
            '<button class="mr2" id="cancel">Cancel</button>'
        )
    )

    assert await political_dialog(dom_page, False) == "answered"
    assert await _log(dom_page) == ["confirm:NOT_POLITICAL"]


async def test_a_footer_without_a_known_cancel_is_never_clicked(
    dom_page: Any,  # noqa: F811
):
    await dom_page.set_content(
        _dialog(
            '<button id="cancel">Cancel</button><button id="confirm">Confirm</button>'
        )
    )

    assert await political_dialog(dom_page, False) == "layout"
    assert await _log(dom_page) == []
