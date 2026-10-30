"""Browser-DOM tests for the Withdraw click on the sent-invitations page.

The document is synthetic: each case is a claim about the click program, never
about LinkedIn's markup. Nothing reaches LinkedIn; skipped when chromium is not
installed.
"""

from __future__ import annotations

from typing import Any

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping import invitation_withdrawal as iw

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

URL = "https://www.linkedin.com/mynetwork/invitation-manager/sent/"


def _row(
    owner: str,
    age: str = "Sent 2 months ago",
    extra: str = "",
    controls: int = 1,
    withdraw: bool = True,
) -> str:
    link = (
        f'<a aria-label="Withdraw invitation sent to {owner} Full" '
        f'href="https://www.linkedin.com/" onclick="openConfirm(event, \'{owner}\')">'
        "Withdraw</a>"
    )
    return (
        f'<div role="listitem"><a href="https://www.linkedin.com/in/{owner}/">{owner}</a>'
        f"{extra}<p><span>{age}</span></p>"
        "<button onclick=\"document.body.dataset.showMore='yes'\">… show more</button>"
        + (link * controls if withdraw else "")
        + "</div><hr>"
    )


# Mirrors the real page: closed ad <dialog>s, a native <dialog open> without a
# role, and infinite scroll that appends 10 rows when the container's empty
# last child (the sentinel) is scrolled into view.
_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<main><div id="list">{rows}<div id="sentinel"></div></div></main>
<dialog id="ad"><p>Ad options</p><button>Hide</button></dialog>
<dialog id="confirm"><button aria-label="Dismiss"></button>
<p>Withdraw invitation</p>
<p>If you withdraw now, you won’t be able to resend to this person for up to 3 weeks.</p>
<button onclick="this.closest('dialog').close()">Cancel</button>
<button id="confirm-btn" aria-label="Withdraw invitation sent to Dajana M."
 onclick="document.body.dataset.confirmed = document.body.dataset.target">Withdraw</button></dialog>
<script>
const list = document.getElementById('list');
const sentinel = document.getElementById('sentinel');
const total = {total};
let made = list.querySelectorAll('[role=listitem]').length;
function openConfirm(event, owner) {{
  event.preventDefault();
  document.body.dataset.target = owner;
  document.getElementById('confirm-btn').setAttribute('aria-label', event.currentTarget.getAttribute('aria-label'));
  document.getElementById('confirm').show();
}}
new IntersectionObserver((entries) => {{
  if (!entries.some((e) => e.isIntersecting)) return;
  setTimeout(() => {{
    for (let i = 0; i < 10 && made < total; i++, made++) {{
      const row = document.createElement('div');
      row.setAttribute('role', 'listitem');
      row.style.minHeight = '300px';
      row.innerHTML = '<a href="https://www.linkedin.com/in/more-' + made + '/">m</a>'
        + '<p><span>Sent 1 week ago</span></p>';
      list.insertBefore(row, sentinel);
    }}
  }}, 50);
}}).observe(sentinel);
</script></body></html>"""


@pytest.fixture
async def dom_page():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            page = await (await browser.new_context()).new_page()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        page.set_default_timeout(1500)
        try:
            yield page
        finally:
            await browser.close()


async def _load(page: Any, body: str, total: int = 0) -> None:
    html = _PAGE.format(rows=body, total=total)

    async def handle(route: Any) -> None:
        if route.request.url == URL:
            await route.fulfill(content_type="text/html; charset=utf-8", body=html)
        else:
            await route.abort()

    await page.route("**/*", handle)
    await page.goto(URL)


async def _flag(page: Any, name: str) -> Any:
    return await page.evaluate(f"document.body.dataset.{name} ?? null")


async def test_a_mutual_connection_link_in_another_row_does_not_select_it(dom_page):
    body = _row("alice", extra='<a href="/in/bob/">mutual: bob</a>') + _row("bob")
    await _load(dom_page, body)

    assert await dom_page.evaluate(iw._CLICK_WITHDRAW_JS, "bob")
    assert await _flag(dom_page, "target") == "bob"


async def test_a_target_that_only_appears_as_a_mutual_link_is_refused(dom_page):
    await _load(dom_page, _row("alice", extra='<a href="/in/bob/">mutual: bob</a>'))

    assert not await dom_page.evaluate(iw._CLICK_WITHDRAW_JS, "bob")
    assert await _flag(dom_page, "target") is None


async def test_two_rows_for_one_person_are_refused(dom_page):
    await _load(dom_page, _row("bob") + _row("bob"))

    assert not await dom_page.evaluate(iw._CLICK_WITHDRAW_JS, "bob")


async def test_a_row_with_two_withdraw_controls_is_refused(dom_page):
    await _load(dom_page, _row("bob", controls=2))

    assert not await dom_page.evaluate(iw._CLICK_WITHDRAW_JS, "bob")


async def test_the_show_more_button_is_never_the_withdraw_control(dom_page):
    await _load(dom_page, _row("bob", withdraw=False))

    assert not await dom_page.evaluate(iw._CLICK_WITHDRAW_JS, "bob")
    assert await _flag(dom_page, "showMore") is None


async def test_the_withdraw_anchor_opens_the_dialog_without_navigating(dom_page):
    await _load(dom_page, _row("bob"))

    assert await dom_page.evaluate(iw._CLICK_WITHDRAW_JS, "bob")
    assert dom_page.url == URL
    assert await _flag(dom_page, "showMore") is None
    assert len(await dom_page.evaluate(iw._OPEN_DIALOGS_JS)) == 1


async def test_a_closed_dialog_is_not_counted_as_open(dom_page):
    await _load(dom_page, _row("bob"))

    assert await dom_page.evaluate(iw._OPEN_DIALOGS_JS) == []


async def test_a_unicode_slug_is_selected_by_its_decoded_lowercase_name(dom_page):
    await _load(dom_page, _row("Alix-François-Poncet-523043135"))

    assert await dom_page.evaluate(
        iw._CLICK_WITHDRAW_JS,
        iw.username_from_href("/in/alix-fran%C3%A7ois-poncet-523043135/"),
    )


async def test_confirm_presses_the_button_whose_text_is_exactly_withdraw(dom_page):
    await _load(dom_page, _row("bob"))
    await dom_page.evaluate(iw._CLICK_WITHDRAW_JS, "bob")

    assert await dom_page.evaluate(iw._CONFIRM_WITHDRAW_JS, "bob") == "clicked"
    assert await _flag(dom_page, "confirmed") == "bob"


async def test_confirm_is_refused_without_the_withdraw_dialog(dom_page):
    await _load(dom_page, _row("bob"))

    assert await dom_page.evaluate(iw._CONFIRM_WITHDRAW_JS, "bob") == "not_found"
    assert await _flag(dom_page, "confirmed") is None


async def _set_confirm_label(page: Any, label: str) -> None:
    await page.evaluate(
        "(l) => document.getElementById('confirm-btn').setAttribute('aria-label', l)",
        label,
    )


async def test_confirm_refuses_a_dialog_naming_someone_else(dom_page):
    await _load(dom_page, _row("bob"))
    await dom_page.evaluate(iw._CLICK_WITHDRAW_JS, "bob")
    await _set_confirm_label(dom_page, "Withdraw invitation sent to Carol D.")

    assert await dom_page.evaluate(iw._CONFIRM_WITHDRAW_JS, "bob") == "name_mismatch"
    assert await _flag(dom_page, "confirmed") is None


async def test_confirm_needs_the_whole_first_name_not_a_prefix(dom_page):
    await _load(dom_page, _row("bob"))
    await dom_page.evaluate(iw._CLICK_WITHDRAW_JS, "bob")
    await _set_confirm_label(dom_page, "Withdraw invitation sent to Bobby T.")

    assert await dom_page.evaluate(iw._CONFIRM_WITHDRAW_JS, "bob") == "name_mismatch"


async def test_confirm_matches_an_abbreviated_name_case_insensitively(dom_page):
    await _load(dom_page, _row("dm"))
    await dom_page.evaluate(iw._CLICK_WITHDRAW_JS, "dm")
    await _set_confirm_label(dom_page, "Withdraw invitation sent to Dajana M.")

    assert await dom_page.evaluate(iw._CONFIRM_WITHDRAW_JS, "dajana") == "clicked"


async def test_confirm_matches_a_name_with_an_emoji(dom_page):
    await _load(dom_page, _row("av"))
    await dom_page.evaluate(iw._CLICK_WITHDRAW_JS, "av")
    await _set_confirm_label(dom_page, "Withdraw invitation sent to Armaan V. ☁️")

    assert await dom_page.evaluate(iw._CONFIRM_WITHDRAW_JS, "armaan") == "clicked"


async def test_confirm_with_no_planned_name_is_refused(dom_page):
    await _load(dom_page, _row("bob"))
    await dom_page.evaluate(iw._CLICK_WITHDRAW_JS, "bob")

    assert await dom_page.evaluate(iw._CONFIRM_WITHDRAW_JS, "") == "name_mismatch"


async def test_rows_for_user_ignores_a_profile_link_outside_the_rows(dom_page):
    await _load(dom_page, _row("bob"))
    await dom_page.evaluate(
        "() => document.body.insertAdjacentHTML('beforeend',"
        ' \'<div role="status"><a href="/in/zed/">zed</a></div>\')'
    )

    assert await dom_page.evaluate(iw._ROWS_FOR_USER_JS, "bob") == 1
    assert await dom_page.evaluate(iw._ROWS_FOR_USER_JS, "zed") == 0


async def test_rows_for_user_uses_the_same_slug_as_the_click(dom_page):
    await _load(dom_page, _row("Alix-François-Poncet-523043135"))
    name = iw.username_from_href("/in/alix-fran%C3%A7ois-poncet-523043135/")

    assert await dom_page.evaluate(iw._ROWS_FOR_USER_JS, name) == 1


async def test_rows_read_name_href_and_only_the_age_leaf(dom_page):
    await _load(
        dom_page,
        _row("bob", age="Sent 11 hours ago") + _row("amy", age="Sent yesterday"),
    )

    rows = await dom_page.evaluate(iw._READ_ROWS_JS)
    assert [r["href"] for r in rows] == [
        "https://www.linkedin.com/in/bob/",
        "https://www.linkedin.com/in/amy/",
    ]
    assert [r["name"] for r in rows] == ["bob Full", "amy Full"]
    assert [iw.age_text(r["text"]) for r in rows] == [
        "Sent 11 hours ago",
        "Sent yesterday",
    ]
    assert "show more" not in rows[0]["text"]


async def test_an_age_inside_a_longer_element_is_not_the_age(dom_page):
    body = _row("bob", age="Sent 3 weeks ago to me by a recruiter")
    await _load(dom_page, body)

    (row,) = await dom_page.evaluate(iw._READ_ROWS_JS)
    assert iw.parse_age_days(row["text"]) is None


async def test_scrolling_the_sentinel_loads_ten_more_rows_until_the_end(dom_page):
    await _load(dom_page, "".join(_row(f"u{i}") for i in range(10)), total=25)
    for row in await dom_page.query_selector_all("[role=listitem]"):
        await row.evaluate("(r) => { r.style.minHeight = '300px'; }")

    seen = [await dom_page.evaluate(iw._COUNT_ROWS_JS)]
    for _ in range(4):
        assert await dom_page.evaluate(iw._SCROLL_LIST_JS)
        await dom_page.wait_for_timeout(300)
        seen.append(await dom_page.evaluate(iw._COUNT_ROWS_JS))

    assert seen == [10, 20, 25, 25, 25]


async def test_no_rows_means_nothing_to_count_or_scroll(dom_page):
    await _load(dom_page, "")

    assert await dom_page.evaluate(iw._COUNT_ROWS_JS) == 0
    assert not await dom_page.evaluate(iw._SCROLL_LIST_JS)
