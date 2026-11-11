"""Browser-DOM tests for reading a job posting's apply control.

The unit suite mocks ``page.evaluate``, so the program behind
``JobPageReader.read_apply_signals`` never runs there. These run it against
synthetic postings in headless chromium, with every request answered by a route
that records it, so a test can say what was never asked for. The markup carries
the attributes measured upstream on 2026-09-14, 2026-09-19 and 2026-09-25 and
none of LinkedIn's classes, so it is a claim about which signals are read, not
about LinkedIn's layout. The redesigned posting has not been measured.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from urllib.parse import quote

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping.capture import SectionCapture
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.contracts import ExtractedSection
from linkedin_mcp_server.scraping.facets import FacetResolver
from linkedin_mcp_server.scraping.job_pages import JobApplySignals, JobPageReader
from linkedin_mcp_server.scraping.jobs import JobScraper
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession
from linkedin_mcp_server.scraping.text import JOB_APPLY_EN_US

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

JOB_URL = "https://www.linkedin.com/jobs/view/123/"
#: The employer's short link, which a real one would redirect onwards.
EMPLOYER_HOST = "grnh.example"
EMPLOYER_LINK = f"https://{EMPLOYER_HOST}/short"

EASY_APPLY = (
    '<a href="https://www.linkedin.com/jobs/view/123/apply/?openSDUIApplyFlow=true"'
    ' aria-label="Easy Apply to this job">Easy Apply</a>'
)
#: The shape that replaced the anchor, as measured on 2026-09-25: a button with
#: no href, its text in a span beside an icon.
EASY_APPLY_BUTTON = (
    '<button type="button" aria-label="Easy Apply to this job">'
    '<span><svg aria-hidden="true"></svg><span>Easy Apply</span></span></button>'
)
#: A "More jobs" card as measured the same day: the words as text in its link.
OTHER_EASY_APPLY_CARD = (
    '<a href="https://www.linkedin.com/jobs/search-results/?currentJobId=999">'
    "<p>AI Engineer</p><p>Easy Apply</p></a>"
)
OTHER_EASY_APPLY = (
    '<a href="https://www.linkedin.com/jobs/view/999/apply/">Easy Apply</a>'
)
EXTERNAL_BUTTON = (
    '<button type="button" aria-label="Apply on company website">Apply</button>'
)


def safety(destination: str) -> str:
    """LinkedIn's interstitial towards ``destination``, encoded as measured."""
    return (
        "https://www.linkedin.com/safety/go/?url="
        f"{quote(destination, safe='')}&isSdui=true"
    )


def external_link(href: str) -> str:
    """The external Apply as a link into the interstitial, as measured."""
    return (
        f'<a target="_blank" aria-label="Apply on company website" href="{href}">'
        "Apply</a>"
    )


#: Records a click on any control in the posting and keeps it from navigating.
NO_CLICK = """
    for (const el of document.querySelectorAll('main a, main button')) {
        el.addEventListener('click', (event) => {
            event.preventDefault();
            document.body.dataset.clicked = 'true';
        });
    }
"""


async def clicked(page) -> bool:
    """Whether a control's click handler ran.

    Recorded on the DOM rather than in a global: a page script runs in the
    main world while ``evaluate`` may run in an isolated one. Both share the
    DOM.
    """
    return await page.evaluate("() => document.body.dataset.clicked === 'true'")


def posting(control: str, *, below: str = "") -> str:
    """A top card, the description, and what sits below it."""
    return (
        "<main>"
        f"<h1>Applied AI Engineer</h1><p>Acme</p>{control}"
        "<h2>About the job</h2><p>Build agents.</p>"
        f"{below}"
        "</main>"
        f"<script>{NO_CLICK}</script>"
    )


@pytest.fixture
def requested() -> list[str]:
    return []


@pytest.fixture
async def dom_page(requested):
    async def answer(route):
        requested.append(route.request.url)
        await route.fulfill(status=200, content_type="text/html", body="<p>ok</p>")

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                channel="chromium", headless=True
            )
            context = await browser.new_context()
            page = await context.new_page()
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"chromium unavailable: {exc}")
        await context.route("**/*", answer)
        try:
            yield page
        finally:
            await browser.close()


async def _serve(page, requested: list[str], html: str) -> None:
    await page.goto(JOB_URL)
    await page.set_content(html)
    # The posting's own load; everything after it is the read's doing.
    requested.clear()


async def read(page, requested: list[str], html: str) -> JobApplySignals | None:
    await _serve(page, requested, html)
    session = ScrapingSession(page)
    reader = JobPageReader(session, PageNavigator(session), PageContentReader(session))
    return await reader.read_apply_signals("123", JOB_APPLY_EN_US)


async def test_easy_apply_is_the_postings_own_apply_route(dom_page, requested):
    assert await read(dom_page, requested, posting(EASY_APPLY)) == JobApplySignals(
        easy_apply=True, external_link=None
    )


async def test_easy_apply_is_the_postings_own_button(dom_page, requested):
    signals = await read(dom_page, requested, posting(EASY_APPLY_BUTTON))

    assert signals == JobApplySignals(easy_apply=True, external_link=None)
    assert not await clicked(dom_page)


async def test_an_easy_apply_button_below_the_description_is_another_postings(
    dom_page, requested
):
    below = OTHER_EASY_APPLY_CARD + EASY_APPLY_BUTTON

    assert await read(dom_page, requested, posting("", below=below)) == (
        JobApplySignals(easy_apply=False, external_link=None)
    )


async def test_another_postings_apply_route_is_not_this_one(dom_page, requested):
    assert await read(dom_page, requested, posting(OTHER_EASY_APPLY)) == (
        JobApplySignals(easy_apply=False, external_link=None)
    )


async def test_an_external_apply_link_is_read_off_its_href_without_a_click(
    dom_page, requested
):
    """The top card's link wins over a same-text link below the description."""
    below = external_link(safety("https://acme.example/"))
    html = posting(external_link(safety(EMPLOYER_LINK)), below=below)

    signals = await read(dom_page, requested, html)

    assert signals == JobApplySignals(
        easy_apply=False, external_link=safety(EMPLOYER_LINK)
    )
    assert not await clicked(dom_page)
    assert requested == []


async def test_an_outbound_link_that_is_not_apply_is_not_the_answer(
    dom_page, requested
):
    link = safety("https://acme.example/")
    html = posting(f'<a href="{link}">Our website</a>')

    assert await read(dom_page, requested, html) == JobApplySignals(
        easy_apply=False, external_link=None
    )


async def test_an_external_apply_link_below_the_description_is_not_this_postings(
    dom_page, requested
):
    html = posting("", below=external_link(safety(EMPLOYER_LINK)))

    assert await read(dom_page, requested, html) == JobApplySignals(
        easy_apply=False, external_link=None
    )


async def test_an_external_apply_link_without_a_description_boundary_is_not_read(
    dom_page, requested
):
    html = posting(external_link(safety(EMPLOYER_LINK))).replace(
        "<h2>About the job</h2>", ""
    )

    assert await read(dom_page, requested, html) == JobApplySignals(
        easy_apply=False, external_link=None
    )


async def test_an_apply_button_is_not_clicked(dom_page, requested):
    """Clicking it counts as an apply on the posting, so it is never read."""
    signals = await read(dom_page, requested, posting(EXTERNAL_BUTTON))

    assert signals == JobApplySignals(easy_apply=False, external_link=None)
    assert not await clicked(dom_page)
    assert requested == []


async def test_a_page_without_main_reads_as_none(dom_page, requested):
    assert await read(dom_page, requested, "<div>Loading</div>") is None


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1:9", "%31%32%37.0.0.%31", "127.0.0.1\\@jobs.example.com"],
)
async def test_get_job_details_withholds_a_link_into_this_host(
    dom_page, requested, host
):
    """The whole fold, on the loaded page: the type stays, the address does not,
    and nothing is requested from it."""
    html = posting(external_link(safety(f"http://{host}/x")))
    await _serve(dom_page, requested, html)
    session = ScrapingSession(dom_page)
    navigator = PageNavigator(session)
    content = PageContentReader(session)
    capture = SectionCapture(session, navigator, content)
    scraper = JobScraper(
        session,
        navigator,
        capture,
        JobPageReader(session, navigator, content),
        FacetResolver(session, navigator, capture),
    )
    text = "Applied AI Engineer\nAcme\nApply\nAbout the job\nBuild agents."
    with patch.object(
        scraper._capture,
        "capture",
        new_callable=AsyncMock,
        return_value=ExtractedSection(text=text, references=[]),
    ):
        result = await scraper.scrape_job("123")

    assert result["apply"] == {"type": "external"}
    assert result["section_errors"]["apply"]["error_type"] == "apply_link_missing"
    assert requested == []
