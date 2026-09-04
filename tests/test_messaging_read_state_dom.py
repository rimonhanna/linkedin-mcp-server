"""Run the actual click/capture/restore path against a synthetic messaging UI.

These fixtures test the algorithm, not a guarantee about LinkedIn's markup.
Live verification is still required when its DOM changes.
"""

import pytest
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor
from patchright.async_api import async_playwright

pytestmark = [pytest.mark.browser_dom, pytest.mark.xdist_group("browser_runtime")]


@pytest.fixture
async def page():
    """Launch isolated Chromium with all LinkedIn traffic served locally."""
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                channel="chromium", headless=True
            )
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            page = await browser.new_page()
            await page.route(
                "https://www.linkedin.com/**",
                lambda route: route.fulfill(
                    body="<html></html>", content_type="text/html"
                ),
            )
            await page.goto("https://www.linkedin.com/messaging/")
            yield page
        finally:
            await browser.close()


async def messaging(page, *, resolve=True, delay=30, acknowledge=True):
    """Build a synthetic sidebar with independent row state and delayed reads."""
    await page.set_content("""<main><ul>
        <li id="row0"><label aria-label="Select conversation with Ada"></label>
          <div class="msg-conversation-listitem__link">Ada</div>
          <span class="msg-conversation-card__message-snippet--unread">Preview</span>
          <button class="msg-thread-actions__control">Options 0</button></li>
        <li id="row1"><label aria-label="Select conversation with Ada"></label>
          <div class="msg-conversation-listitem__link">Ada</div>
          <span>Preview</span>
          <button class="msg-thread-actions__control">Options 1</button></li>
      </ul><div class="msg-title-bar"><button class="msg-thread-actions__control">Thread options</button></div>
      <div id="menu" hidden><button id="toggle"></button></div></main>
    """)
    await page.evaluate(
        """({resolve, delay, acknowledge}) => {
      const rows = [...document.querySelectorAll('li')];
      window.clicks = [];
      let selected = 0, menuIndex = 0;
      const unreadClass = 'msg-conversation-card__message-snippet--unread';
      const unread = i => rows[i].querySelector('span').classList.contains(unreadClass);
      const menu = document.querySelector('#menu');
      const toggle = document.querySelector('#toggle');
      const open = i => { menuIndex = i; toggle.textContent = unread(i) ? 'Mark as read' : 'Mark as unread'; menu.hidden = false; };
      rows.forEach((row, i) => {
        row.querySelector('div').onclick = () => {
          selected = i; window.clicks.push(i);
          if (resolve) history.pushState({}, '', '/messaging/thread/thread-' + i + '/');
          setTimeout(() => row.querySelector('span').classList.remove(unreadClass), delay);
        };
        row.querySelector('button').onclick = () => open(i);
      });
      document.querySelector('.msg-title-bar button').onclick = () => open(selected);
      toggle.onclick = () => {
        if (acknowledge) rows[menuIndex].querySelector('span').classList.toggle(unreadClass);
        menu.hidden = true;
      };
      document.addEventListener('keydown', e => { if (e.key === 'Escape') menu.hidden = true; });
    }""",
        {"resolve": resolve, "delay": delay, "acknowledge": acknowledge},
    )


async def states(page):
    """Read each fixture row's unread bit in current DOM order."""
    return await page.evaluate(
        "[...document.querySelectorAll('li span')].map(e => e.classList.contains('msg-conversation-card__message-snippet--unread'))"
    )


async def test_duplicate_participants_keep_their_individual_state(page):
    """Preserve individual unread bits when participant names are identical."""
    await messaging(page)
    extractor = LinkedInExtractor(page)
    async with extractor._preserve_messaging_read_state():
        refs = await extractor._extract_conversation_thread_refs(None, "inbox")
    assert [r["url"] for r in refs] == [
        "/messaging/thread/thread-0/",
        "/messaging/thread/thread-1/",
    ]
    assert await states(page) == [True, False]
    assert await page.evaluate("window.clicks") == [0, 1]


async def test_unresolved_url_repairs_delayed_mutation(page):
    """Wait for a read mutation that arrives after URL capture times out."""
    await messaging(page, resolve=False, delay=2300)
    extractor = LinkedInExtractor(page)
    async with extractor._preserve_messaging_read_state():
        refs = await extractor._extract_conversation_thread_refs(1, "search")
    assert refs == []
    assert await states(page) == [True, False]


async def test_failed_click_cannot_borrow_previous_threads_url(page):
    """Reject an unchanged URL instead of assigning the previous thread."""
    await messaging(page, resolve=False)
    await page.evaluate("history.replaceState({}, '', '/messaging/thread/previous/')")
    extractor = LinkedInExtractor(page)
    async with extractor._preserve_messaging_read_state():
        refs = await extractor._extract_conversation_thread_refs(1, "inbox")
    assert refs == []
    assert await states(page) == [True, False]


@pytest.mark.parametrize("resolve", [True, False])
async def test_reordered_rows_restore_the_original_row_not_its_old_index(page, resolve):
    """Follow the original row even if it moves during URL capture."""
    await messaging(page, resolve=resolve)
    await page.evaluate("""() => {
      const row = document.querySelector('#row0');
      const target = row.querySelector('div');
      const click = target.onclick;
      target.onclick = () => { click(); row.parentElement.append(row); };
    }""")
    extractor = LinkedInExtractor(page)
    async with extractor._preserve_messaging_read_state():
        refs = await extractor._extract_conversation_thread_refs(
            2 if resolve else 1, "inbox"
        )
    assert [r["url"] for r in refs] == (
        ["/messaging/thread/thread-0/", "/messaging/thread/thread-1/"]
        if resolve
        else []
    )
    assert (
        await page.evaluate(
            "document.querySelector('#row0 span').classList.contains('msg-conversation-card__message-snippet--unread')"
        )
        is True
    )
    assert (
        await page.evaluate(
            "document.querySelector('#row1 span').classList.contains('msg-conversation-card__message-snippet--unread')"
        )
        is False
    )


async def test_replaced_row_fails_without_mutating_another_row(page):
    """Refuse recovery when the original label no longer exists."""
    await messaging(page, resolve=False)
    await page.evaluate("""() => {
      const row = document.querySelector('#row0');
      const click = row.querySelector('div').onclick;
      row.querySelector('div').onclick = () => { click(); row.remove(); };
    }""")
    extractor = LinkedInExtractor(page)
    with pytest.raises(Exception, match="Could not locate unresolved"):
        async with extractor._preserve_messaging_read_state():
            await extractor._extract_conversation_thread_refs(1, "inbox")
    assert await states(page) == [False]


async def test_unacknowledged_restore_is_an_error(page):
    """Do not report a read as successful when LinkedIn ignores restoration."""
    await messaging(page, acknowledge=False)
    extractor = LinkedInExtractor(page)
    with pytest.raises(Exception, match="Could not verify"):
        await extractor._extract_conversation_thread_refs(1, "inbox")
    assert await states(page) == [False, False]
