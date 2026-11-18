"""Browser-DOM tests for the audience dialog steps.

The documents are synthetic: each case is a claim about the steps, never about
LinkedIn's markup. Labels come in two languages to show that no step reads
visible text. Nothing reaches LinkedIn; skipped when chromium is not installed.
"""

from __future__ import annotations

from typing import Any

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping import campaign_manager_audiences as audiences
from linkedin_mcp_server.scraping.campaign_manager_entities_payload import (
    ACCOUNT_TEMPLATE_HEADERS,
    company_list_csv,
)

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

_LABELS = {
    "en": {
        "items": ("Predictive audience", "Matched audience", "Retargeting"),
        "types": ("Contact list", "Company list"),
        "cancel": "Cancel",
        "submit": "Agree and upload",
        "save": "Save",
    },
    "de": {
        "items": ("Vorhersage", "Abgeglichene Zielgruppe", "Retargeting"),
        "types": ("Kontaktliste", "Unternehmensliste"),
        "cancel": "Abbrechen",
        "submit": "Zustimmen und hochladen",
        "save": "Speichern",
    },
}

_SCRIPT = """<script>
function mark(name) {
  document.body.dataset.clicks = (document.body.dataset.clicks || '') + name + ' ';
}
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  for (const dialog of document.querySelectorAll('[role=dialog]')) dialog.hidden = true;
});
</script>"""


def _upload_page(locale: str, *, items: int = 3, terms: bool = True) -> str:
    labels = _LABELS[locale]
    names = (labels["items"] * 2)[:items]
    menu_items = "".join(
        f'<button role="menuitem" onclick="mark(\'item{i}\'); '
        f"document.getElementById('upload').hidden = {str(i != 1).lower()}\">"
        f"{label}</button>"
        for i, label in enumerate(names)
    )
    contact, company = labels["types"]
    link = (
        '<a data-test-create-ma-list-panel__legal-terms-link href="/legal/sas-terms">'
        "terms</a>"
        if terms
        else ""
    )
    return (
        _SCRIPT
        + '<button id="ma-header-actions-create-trigger" aria-controls="hue-menu-7" '
        "onclick=\"document.getElementById('hue-menu-7').hidden = false\">+</button>"
        + f'<div id="hue-menu-7" hidden>{menu_items}</div>'
        + '<div id="upload" role="dialog" '
        'aria-labelledby="create-matched-audience-modal-title" hidden>'
        '<h2 id="create-matched-audience-modal-title">x</h2>'
        '<button aria-label="close" onclick="mark(\'close\')">x</button>'
        '<input id="create-ma-list-name-input" name="create-ma-list-name">'
        '<select id="create-ma-list-modal-type-select">'
        f'<option value="MEMBER">{contact}</option>'
        f'<option value="COMPANY">{company}</option></select>'
        '<input id="create-ma-list-file-input" type="file" accept=".csv" '
        "onchange=\"document.getElementById('submit').disabled = false\">"
        + link
        + f"<div><button onclick=\"mark('cancel')\">{labels['cancel']}</button>"
        f'<button id="submit" disabled onclick="mark(\'submit\')">'
        f"{labels['submit']}</button></div></div>"
    )


def _save_page(locale: str, *, disabled: bool = False, extra: str = "") -> str:
    labels = _LABELS[locale]
    return (
        _SCRIPT
        + '<button id="targeting-module-save-audience-button" '
        + ("disabled " if disabled else "")
        + "onclick=\"document.getElementById('save').hidden = false\">s</button>"
        + '<div id="save" role="dialog" aria-labelledby="save-audience-modal__header" '
        'hidden><h2 id="save-audience-modal__header">x</h2>'
        "<button onclick=\"mark('close')\">x</button>"
        '<input id="audience-name" maxlength="25" '
        "oninput=\"document.getElementById('save-btn').disabled = !this.value\">"
        '<textarea id="audience-description" maxlength="125"></textarea>'
        '<div class="modal-footer--with-buttons">'
        f'<button aria-label="{labels["cancel"]}" onclick="mark(\'cancel\')">'
        f"{labels['cancel']}</button>"
        + extra
        + f'<button id="save-btn" disabled onclick="mark(\'save\')">'
        f"{labels['save']}</button></div></div>"
    )


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


async def _clicks(page: Any) -> list[str]:
    return (await page.evaluate("document.body.dataset.clicks || ''")).split()


@pytest.fixture(autouse=True)
def _short_waits(monkeypatch):
    monkeypatch.setattr(audiences, "_UI_TIMEOUT", 1.0)


CSV = company_list_csv(
    [
        {"name": "Example, Co", "linkedin_url": "https://www.linkedin.com/company/ex"},
        {"name": 'Sample "Q" Ltd', "website": "https://sample.example"},
    ],
    ACCOUNT_TEMPLATE_HEADERS,
)


@pytest.mark.parametrize("locale", sorted(_LABELS))
async def test_upload_form_is_filled_by_anchor_and_never_submitted_in_a_dry_run(
    dom_page, locale
):
    await dom_page.set_content(_upload_page(locale))

    dialog = await audiences._open_upload_dialog(dom_page)
    assert dialog is not None
    terms = await audiences._upload_terms_url(dialog)
    filled = await audiences._fill_upload_form(
        dom_page, dialog, "Example list", "example-list.csv", CSV.encode("utf-8")
    )
    await audiences._close_dialog(dom_page, dialog)

    assert terms == "https://www.linkedin.com/legal/sas-terms"
    assert filled is True
    assert await dom_page.input_value("#create-ma-list-name-input") == "Example list"
    assert await dom_page.input_value("#create-ma-list-modal-type-select") == "COMPANY"
    uploaded = await dom_page.evaluate(
        """async () => {
            const file = document.getElementById('create-ma-list-file-input').files[0];
            return { name: file.name, type: file.type, text: await file.text() };
        }"""
    )
    assert uploaded == {"name": "example-list.csv", "type": "text/csv", "text": CSV}
    assert await _clicks(dom_page) == ["item1"]
    assert await dom_page.is_hidden("#upload")


async def test_upload_submit_is_the_dialogs_last_button(dom_page):
    await dom_page.set_content(_upload_page("de"))
    dialog = await audiences._open_upload_dialog(dom_page)
    assert dialog is not None
    await audiences._fill_upload_form(
        dom_page, dialog, "Example list", "example-list.csv", CSV.encode("utf-8")
    )

    assert await audiences._submit(dialog, audiences._upload_submit(dialog))
    assert await _clicks(dom_page) == ["item1", "submit"]


async def test_upload_submit_stays_unclicked_while_disabled(dom_page):
    await dom_page.set_content(_upload_page("en"))
    dialog = await audiences._open_upload_dialog(dom_page)
    assert dialog is not None

    assert not await audiences._submit(dialog, audiences._upload_submit(dialog))
    assert await _clicks(dom_page) == ["item1"]


async def test_upload_menu_with_another_item_count_opens_nothing(dom_page):
    await dom_page.set_content(_upload_page("en", items=4))

    assert await audiences._open_upload_dialog(dom_page) is None
    assert await _clicks(dom_page) == []


async def test_upload_form_without_its_terms_link_is_refused(dom_page):
    await dom_page.set_content(_upload_page("en", terms=False))
    dialog = await audiences._open_upload_dialog(dom_page)
    assert dialog is not None

    assert await audiences._upload_terms_url(dialog) is None


@pytest.mark.parametrize("locale", sorted(_LABELS))
async def test_save_dialog_name_and_save_by_position(dom_page, locale):
    await dom_page.set_content(_save_page(locale))

    dialog = await audiences._open_save_dialog(dom_page)
    assert dialog not in (None, False)
    assert await audiences._fill_save_dialog(dom_page, dialog, "Example saved")
    assert await audiences._submit(dialog, audiences._save_submit(dialog))

    assert await dom_page.input_value("#audience-name") == "Example saved"
    assert await _clicks(dom_page) == ["save"]


async def test_save_dialog_dry_run_closes_without_any_button(dom_page):
    await dom_page.set_content(_save_page("en"))
    dialog = await audiences._open_save_dialog(dom_page)
    assert await audiences._fill_save_dialog(dom_page, dialog, "Example saved")

    await audiences._close_dialog(dom_page, dialog)

    assert await _clicks(dom_page) == []
    assert await dom_page.is_hidden("#save")


async def test_save_disabled_is_reported_without_a_click(dom_page):
    await dom_page.set_content(_save_page("en", disabled=True))

    assert await audiences._open_save_dialog(dom_page) is False
    assert await dom_page.is_hidden("#save")


async def test_save_dialog_with_an_extra_footer_button_is_refused(dom_page):
    await dom_page.set_content(
        _save_page("en", extra="<button onclick=\"mark('extra')\">More</button>")
    )

    assert await audiences._open_save_dialog(dom_page) is None
    assert await _clicks(dom_page) == []
    assert await dom_page.is_hidden("#save")


async def test_a_name_the_field_truncates_is_refused(dom_page):
    await dom_page.set_content(_save_page("en"))
    dialog = await audiences._open_save_dialog(dom_page)

    assert not await audiences._fill_save_dialog(dom_page, dialog, "x" * 26)
