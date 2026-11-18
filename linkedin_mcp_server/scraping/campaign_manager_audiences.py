"""Campaign Manager audiences: list them, upload a company list, save an ad set's.

Every value is identified through the responses Campaign Manager itself
fetches: the audience lists by their finder and ``accountId``, a created
audience by the ``x-restli-id`` of the app's own create and a re-read of the
list. Controls are found by id, ``data-*`` attribute, role and position, never
by visible text, so the flows work in every UI language.

Submitting a company list clicks LinkedIn's agree-and-upload button, which
accepts the LinkedIn Ads Agreement for the ad account; both the dry run and the
result say so and name the terms page the dialog links. The CSV is built in
memory and handed to the file input as a buffer. The app then asks Campaign
Manager for a signed upload URL, sends the file (its own re-serialization of
our CSV) to LinkedIn's media host, which is outside the guard's pattern, and
creates the audience; only those two Campaign Manager writes are admitted.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urljoin, urlparse

from patchright.async_api import Error as PatchrightError
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
    LinkedInScraperException,
)
from linkedin_mcp_server.scraping.campaign_manager import (
    _SETUP_PATH,
    _error,
    _page_limit,
)
from linkedin_mcp_server.response_structure import (
    is_field_name,
    params_shape,
    skeleton,
)
from linkedin_mcp_server.scraping.campaign_manager_api import (
    CampaignManagerCapture,
    CmResponse,
    CmWriteResponse,
)
from linkedin_mcp_server.scraping.campaign_manager_changes import (
    TargetingKey,
    admit_list_upload,
    admit_saved_audience,
    refuse_invalid_audience_name,
    saved_audience_omitted,
    targeting_keys,
)
from linkedin_mcp_server.scraping.campaign_manager_entities_payload import (
    ACCOUNT_TEMPLATE_HEADERS,
    MatchedAudience,
    SavedAudience,
    company_list_csv,
    matched_audiences,
    render_audiences,
    saved_audiences,
)
from linkedin_mcp_server.scraping.campaign_manager_guard import (
    CmWriteRequest,
    WriteWindow,
    without_campaign_manager_writes,
)
from linkedin_mcp_server.scraping.campaign_manager_payload import (
    build_ad_account,
    build_campaign,
    envelope_elements,
    envelope_total,
)
from linkedin_mcp_server.scraping.campaign_manager_results import (
    campaign_action_result,
    retry_safe_from,
)
from linkedin_mcp_server.scraping.campaign_manager_write import (
    ANCHORS,
    CampaignWriteFlow,
)
from linkedin_mcp_server.scraping.identifiers import (
    campaign_manager_url,
    normalize_ad_account_id,
    normalize_campaign_id,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

logger = logging.getLogger(__name__)

_LIST_TOOL = "create_company_list_audience"
_SAVED_TOOL = "create_saved_audience"
_MATCHED_SUFFIX = "/audiences/matched"
_MATCHED_RESOURCE = "campaignManagerMatchedAudiences"
_SAVED_RESOURCE = "campaignManagerTargetingCriteria"
_SIGNED_URL_RESOURCE = "campaignManagerAmbrySignedUrl"
_SEGMENT_RESOURCE = "campaignManagerDmpSegments"
_LANGUAGE = "include_language"
# The app leaves the profile language out of a saved audience unless the
# account has LinkedIn's locale-saving flag, which the tool cannot see.
_LANGUAGE_MAY_BE_OMITTED = (
    "LinkedIn may save it without the profile-language targeting, which it "
    "decides for this ad account; the result says so if it does. "
)
_REFUSED_STATUSES = frozenset({403, 404})
_READY_TIMEOUT = 12.0
_UI_TIMEOUT = 5.0
# The signed URL, the media upload and the create run back to back.
_UPLOAD_TIMEOUT = 45.0
_SAVE_TIMEOUT = 20.0

# Anchors observed 2026-11-17. The create menu lists Predictive, Matched and
# Retargeting as menu items without ids, so Matched is found by position and
# the dialog it opens is then checked by its own anchors.
_SAVED_TAB = '[id$="-tab-accounts.item.audiences.saved.index"]'
_CREATE_TRIGGER = "#ma-header-actions-create-trigger"
_MENU_ITEMS = 3
_MATCHED_MENU_ITEM = 1
_UPLOAD_DIALOG = "[role=dialog][aria-labelledby=create-matched-audience-modal-title]"
_LIST_NAME_INPUT = "#create-ma-list-name-input"
_LIST_TYPE_SELECT = "select#create-ma-list-modal-type-select"
_COMPANY_LIST = "COMPANY"
_FILE_INPUT = "input#create-ma-list-file-input[type=file]"
_TERMS_LINK = "a[data-test-create-ma-list-panel__legal-terms-link]"
_SAVE_AUDIENCE_BUTTON = "#targeting-module-save-audience-button"
_SAVE_DIALOG = "[role=dialog][aria-labelledby=save-audience-modal__header]"
_SAVED_NAME_INPUT = "input#audience-name"
# The save dialog's footer holds Cancel then Save.
_SAVE_FOOTER = ".modal-footer--with-buttons"
_SAVE_FOOTER_BUTTONS = 2
_MENU_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_CREATED_ID_RE = re.compile(r"^(?:urn:li:[A-Za-z]+:)?([0-9]{1,20})$")
_LINKEDIN = "https://www.linkedin.com/"

_AGREEMENT = (
    "Submitting clicks LinkedIn's agree-and-upload button, which accepts the "
    "LinkedIn Ads Agreement ({terms}) for this ad account."
)


def _ms(seconds: float) -> float:
    return seconds * 1000


def _file_name(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()[:60]
    return f"{slug or 'company-list'}.csv"


def _row_count(text: str) -> int:
    return sum(1 for _ in csv.reader(io.StringIO(text))) - 1


def _created_number(value: str | None) -> int | None:
    match = _CREATED_ID_RE.fullmatch(value or "")
    return int(match.group(1)) if match else None


def _for_account(response: CmResponse, account_id: str) -> bool:
    return response.call.params.get("accountId") == account_id


def _list_call(
    resource: str, finder: str, account_id: str
) -> Callable[[CmResponse], bool]:
    def matches(response: CmResponse) -> bool:
        return (
            response.ok
            and response.call.resource == resource
            and response.call.finder == finder
            and _for_account(response, account_id)
        )

    return matches


def _refused_account(account_id: str) -> Callable[[CmResponse], bool]:
    def refused(response: CmResponse) -> bool:
        return (
            response.status in _REFUSED_STATUSES
            and response.call.resource in {_MATCHED_RESOURCE, _SAVED_RESOURCE}
            and _for_account(response, account_id)
        )

    return refused


def _unverified(what: str) -> LinkedInScraperException:
    return LinkedInScraperException(
        f"Campaign Manager returned {what} in a shape this tool does not "
        "recognise, so nothing was reported."
    )


# --- DOM steps (each takes the page; no visible text is read) -----------------


async def _wait_visible(locator: Any, timeout: float = _UI_TIMEOUT) -> bool:
    """Whether exactly one element matches and it becomes visible in time."""
    try:
        await locator.first.wait_for(state="visible", timeout=_ms(timeout))
    except (PlaywrightTimeoutError, PatchrightError):
        return False
    return await locator.count() == 1


async def _wait_enabled(button: Any, timeout: float = _UI_TIMEOUT) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if await button.is_visible() and await button.is_enabled():
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.2)


async def _type_into(page: Any, field: Any, text: str) -> bool:
    """One input event with ``text``; True when the field then holds exactly it."""
    await field.focus()
    await field.select_text()
    await page.keyboard.insert_text(text)
    return await field.input_value() == text


async def _close_dialog(page: Any, dialog: Any) -> None:
    """Escape, never a dialog button: closing must not be able to submit."""
    await page.keyboard.press("Escape")
    try:
        await dialog.first.wait_for(state="hidden", timeout=_ms(_UI_TIMEOUT))
    except (PlaywrightTimeoutError, PatchrightError):
        logger.debug("Audience dialog still open after Escape")


def _upload_submit(dialog: Any) -> Any:
    # The dialog's last button is its primary agree-and-upload (after Cancel).
    return dialog.locator("button").last


def _save_submit(dialog: Any) -> Any:
    return dialog.locator(_SAVE_FOOTER).locator("button").last


async def _open_upload_dialog(page: Any) -> Any | None:
    """Open the matched audience list upload dialog; None if it is not there."""
    trigger = page.locator(_CREATE_TRIGGER)
    if await trigger.count() != 1:
        return None
    await trigger.click()
    menu_id = await trigger.get_attribute("aria-controls")
    if menu_id is None or not _MENU_ID_RE.fullmatch(menu_id):
        return None
    items = page.locator(f'[id="{menu_id}"] [role=menuitem]')
    try:
        await items.first.wait_for(state="visible", timeout=_ms(_UI_TIMEOUT))
    except (PlaywrightTimeoutError, PatchrightError):
        return None
    if await items.count() != _MENU_ITEMS:
        await page.keyboard.press("Escape")
        return None
    await items.nth(_MATCHED_MENU_ITEM).click()
    dialog = page.locator(_UPLOAD_DIALOG)
    return dialog if await _wait_visible(dialog) else None


async def _upload_terms_url(dialog: Any) -> str | None:
    """The terms page the dialog links, when every upload control is present."""
    for selector in (
        _LIST_NAME_INPUT,
        f'{_LIST_TYPE_SELECT} option[value="{_COMPANY_LIST}"]',
        _FILE_INPUT,
        _TERMS_LINK,
    ):
        if await dialog.locator(selector).count() != 1:
            logger.debug("Upload dialog lacks %s", selector)
            return None
    href = await dialog.locator(_TERMS_LINK).get_attribute("href")
    if not href:
        return None
    terms = urljoin(_LINKEDIN, href)
    return terms if urlparse(terms).scheme == "https" else None


async def _fill_upload_form(
    page: Any, dialog: Any, name: str, file_name: str, data: bytes
) -> bool:
    """Name, list type and file; True once the app enables its submit."""
    if not await _type_into(page, dialog.locator(_LIST_NAME_INPUT), name):
        return False
    select = dialog.locator(_LIST_TYPE_SELECT)
    await select.select_option(value=_COMPANY_LIST)
    await dialog.locator(_FILE_INPUT).set_input_files(
        {"name": file_name, "mimeType": "text/csv", "buffer": data}
    )
    # The app detects the list type from the parsed file; it must stay COMPANY.
    if not await _wait_enabled(_upload_submit(dialog)):
        return False
    return await select.input_value() == _COMPANY_LIST


async def _open_save_dialog(page: Any) -> Any:
    """The save audience dialog, None if absent, False if saving is disabled."""
    button = page.locator(_SAVE_AUDIENCE_BUTTON)
    if await button.count() != 1:
        return None
    if not await button.is_enabled():
        return False
    await button.click()
    dialog = page.locator(_SAVE_DIALOG)
    if not await _wait_visible(dialog):
        return None
    if (
        await dialog.locator(_SAVED_NAME_INPUT).count() != 1
        or await dialog.locator(_SAVE_FOOTER).count() != 1
        or await dialog.locator(_SAVE_FOOTER).locator("button").count()
        != _SAVE_FOOTER_BUTTONS
    ):
        await _close_dialog(page, dialog)
        return None
    return dialog


async def _fill_save_dialog(page: Any, dialog: Any, name: str) -> bool:
    if not await _type_into(page, dialog.locator(_SAVED_NAME_INPUT), name):
        return False
    return await _wait_enabled(_save_submit(dialog))


async def _submit(dialog: Any, button: Any) -> bool:
    """Click ``button`` only if its dialog is still the one open and it is enabled."""
    if await dialog.count() != 1 or not await button.is_enabled():
        return False
    await button.click()
    return True


class CampaignAudiences:
    """Own every workflow whose subject is a Campaign Manager audience."""

    def __init__(self, session: ScrapingSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator
        self._flow = CampaignWriteFlow(session, navigator)

    async def _load_matched(
        self, capture: CampaignManagerCapture, account_id: str
    ) -> CmResponse | None:
        """Open the matched audiences tab latched; its list, or None.

        Only a list that arrives after this call counts. An ad account LinkedIn
        refuses raises ``InvalidReferenceError``.
        """
        url = campaign_manager_url(account_id, _MATCHED_SUFFIX)
        is_list = _list_call(_MATCHED_RESOURCE, "account", account_id)
        refused = _refused_account(account_id)
        page = self._session.page
        async with without_campaign_manager_writes(page):
            await capture.settle()
            after = len(capture.responses)
            await self._navigator._navigate_to_page(url)
            await self._session.check_rate_limit()
            await self._session.dismiss_modal()
            if _SETUP_PATH in urlparse(page.url).path:
                logger.debug("Audiences redirected to campaign setup")
                return None
            try:
                response = await capture.wait_for(
                    lambda r: is_list(r) or refused(r),
                    after=after,
                    timeout=_READY_TIMEOUT,
                    wanted=(_MATCHED_RESOURCE,),
                )
            except LinkedInScraperException:
                return None
            await capture.settle()
        if refused(response):
            raise InvalidReferenceError(
                f"Campaign Manager refused that ad_account_id (HTTP "
                f"{response.status}). Pass an id exactly as list_ad_accounts "
                "returned it, for an account this member can open."
            )
        return response

    async def _load_saved(
        self, capture: CampaignManagerCapture, account_id: str
    ) -> CmResponse | None:
        """From the matched tab, open the saved tab latched; its list, or None."""
        is_list = _list_call(_SAVED_RESOURCE, "member", account_id)
        refused = _refused_account(account_id)
        page = self._session.page
        async with without_campaign_manager_writes(page):
            tab = page.locator(_SAVED_TAB)
            if await tab.count() != 1:
                return None
            await capture.settle()
            after = len(capture.responses)
            await tab.click()
            try:
                response = await capture.wait_for(
                    lambda r: is_list(r) or refused(r),
                    after=after,
                    timeout=_READY_TIMEOUT,
                    throttle=False,
                    wanted=(_SAVED_RESOURCE,),
                )
            except LinkedInScraperException:
                return None
            await capture.settle()
        if refused(response):
            raise InvalidReferenceError(
                f"Campaign Manager refused that ad_account_id (HTTP {response.status})."
            )
        return response

    async def list_audiences(self, account_id: str) -> dict[str, Any]:
        """Matched audiences (uploaded lists, retargeting, ...) and saved audiences."""
        account = normalize_ad_account_id(account_id)
        url = campaign_manager_url(account, _MATCHED_SUFFIX)
        section_errors: dict[str, dict[str, str]] = {}
        async with self._flow.capturing() as capture:
            matched_response = await self._load_matched(capture, account)
            saved_response = (
                await self._load_saved(capture, account)
                if matched_response is not None
                else None
            )

        result: dict[str, Any] = {"url": url, "sections": {}}
        for name, response, parse in (
            ("matched_audiences", matched_response, matched_audiences),
            ("saved_audiences", saved_response, saved_audiences),
        ):
            label = name.replace("_", " ")
            if response is None:
                section_errors[name] = _error(
                    "unavailable", f"Campaign Manager did not load the {label}."
                )
                continue
            audiences = parse(response.payload)
            elements = envelope_elements(response.payload)
            if audiences is None or elements is None:
                raise _unverified(f"the {label}")
            result[name] = audiences
            if text := render_audiences(audiences):
                result["sections"][name] = text
            total = envelope_total(response.payload)
            if total is not None and total > len(elements):
                section_errors[name] = _page_limit(label, total, len(elements))
        if section_errors:
            result["section_errors"] = section_errors
        return result

    async def create_company_list_audience(
        self,
        account_id: str,
        name: str,
        companies: Sequence[Mapping[str, Any]],
        *,
        confirm_send: bool,
    ) -> dict[str, Any]:
        """Upload a company list as a matched audience of the ad account.

        Submitting clicks LinkedIn's agree-and-upload button, which accepts the
        LinkedIn Ads Agreement (``planned.termsUrl``) for this ad account.
        ``confirm_send=False`` opens and fills the upload dialog, checks the app
        accepts the file, closes the dialog with Escape and sends nothing.
        """
        account = normalize_ad_account_id(account_id)
        url = campaign_manager_url(account, _MATCHED_SUFFIX)
        target = dict(build_ad_account({"id": int(account)}) or {})
        invalid = refuse_invalid_audience_name(name, saved=False)
        if invalid is not None:
            return campaign_action_result(
                _LIST_TOOL, url, "invalid_change", invalid, retry_safe=True
            )
        try:
            text = company_list_csv(companies, ACCOUNT_TEMPLATE_HEADERS)
        except InvalidReferenceError as exc:
            return campaign_action_result(
                _LIST_TOOL, url, "invalid_change", str(exc), retry_safe=True
            )
        file_name = _file_name(name)
        planned: dict[str, Any] = {
            "name": name,
            "listType": _COMPANY_LIST,
            "companies": _row_count(text),
            "fileName": file_name,
        }
        page = self._session.page

        async with self._flow.capturing() as capture:
            try:
                before = await self._load_matched(capture, account)
            except InvalidReferenceError as exc:
                return campaign_action_result(
                    _LIST_TOOL,
                    url,
                    "target_not_found",
                    f"{exc} Nothing was uploaded.",
                    retry_safe=True,
                )
            if before is None:
                return campaign_action_result(
                    _LIST_TOOL,
                    url,
                    "upload_form_unavailable",
                    "Campaign Manager did not open this ad account's matched "
                    "audiences, so nothing was uploaded.",
                    retry_safe=True,
                    target=target,
                    planned=planned,
                )
            await self._flow.wait_for_anchor(page, ANCHORS["create_menu"])
            async with without_campaign_manager_writes(page):
                dialog = await _open_upload_dialog(page)
                terms = await _upload_terms_url(dialog) if dialog else None
                if dialog is None or terms is None:
                    if dialog is not None:
                        await _close_dialog(page, dialog)
                    return campaign_action_result(
                        _LIST_TOOL,
                        url,
                        "upload_form_unavailable",
                        "Campaign Manager did not show its company list upload "
                        "form with every expected control, so nothing was "
                        "uploaded.",
                        retry_safe=True,
                        target=target,
                        planned=planned,
                    )
                planned["termsUrl"] = terms
                planned["acceptsAdsAgreement"] = True
                agreement = _AGREEMENT.format(terms=terms)
                if not await _fill_upload_form(
                    page, dialog, name, file_name, text.encode("utf-8")
                ):
                    await _close_dialog(page, dialog)
                    return campaign_action_result(
                        _LIST_TOOL,
                        url,
                        "control_unavailable",
                        "Campaign Manager did not accept the filled upload form "
                        "as a company list, so nothing was uploaded.",
                        retry_safe=True,
                        target=target,
                        planned=planned,
                    )
                if not confirm_send:
                    await _close_dialog(page, dialog)
                    return campaign_action_result(
                        _LIST_TOOL,
                        url,
                        "confirmation_required",
                        f"Ready to upload {planned['companies']} companies as the "
                        f"company list audience {name!r}. {agreement} Call again "
                        "with confirm_send true to upload.",
                        retry_safe=True,
                        target=target,
                        planned=planned,
                    )

            account_path = urlparse(campaign_manager_url(account)).path
            async with self._flow.apply(
                page,
                admit_list_upload(account_id=account, name=name),
                expected_path=lambda path: path.startswith(f"{account_path}/audiences"),
            ) as window:
                after = capture.write_count
                clicked = await _submit(dialog, _upload_submit(dialog))
                response = (
                    await self._flow.wait_saved(
                        capture,
                        lambda w: (
                            w.call.resource == _SEGMENT_RESOURCE
                            or (w.call.resource == _SIGNED_URL_RESOURCE and not w.ok)
                        ),
                        after,
                        timeout=_UPLOAD_TIMEOUT,
                    )
                    if clicked
                    else None
                )

            outcome = _write_outcome(_LIST_TOOL, url, window, response, target, planned)
            if outcome is not None:
                return outcome
            assert response is not None
            created = _created_number(response.created_id)
            after_list = await self._reread(
                lambda: self._load_matched(capture, account), matched_audiences
            )

        found = _with_id(after_list, created)
        if created is None or found is None:
            return campaign_action_result(
                _LIST_TOOL,
                url,
                "save_unverified",
                "LinkedIn accepted the company list upload, but the audience "
                "could not be found in the ad account's matched audiences "
                f"afterwards. {agreement} Check list_audiences before uploading "
                "again.",
                retry_safe=False,
                target=target,
                planned=planned,
                created_id=response.created_id,
            )
        return campaign_action_result(
            _LIST_TOOL,
            url,
            "audience_created",
            f"Uploaded the company list audience {name!r} (id {created}); "
            "LinkedIn matches it to companies over the next hours. The LinkedIn "
            f"Ads Agreement ({terms}) was accepted for this ad account.",
            retry_safe=retry_safe_from(window),
            target=target,
            planned=planned,
            applied=found,
            created_id=str(created),
            refused=window.refused,
        )

    async def create_saved_audience(
        self,
        account_id: str,
        campaign_id: str,
        name: str,
        *,
        confirm_send: bool,
    ) -> dict[str, Any]:
        """Save an ad set's (API campaign's) targeting as a saved audience.

        ``confirm_send=False`` opens the save dialog, types the name, closes it
        with Escape and sends nothing. Only the create carrying exactly the ad
        set's targeting values is admitted.
        """
        account = normalize_ad_account_id(account_id)
        campaign = normalize_campaign_id(campaign_id)
        url = campaign_manager_url(account, f"/campaigns/{campaign}/details")
        invalid = refuse_invalid_audience_name(name, saved=True)
        if invalid is not None:
            return campaign_action_result(
                _SAVED_TOOL, url, "invalid_change", invalid, retry_safe=True
            )
        page = self._session.page

        async with self._flow.capturing() as capture:
            record = await self._flow.read_campaign(capture, account, campaign)
            if isinstance(record, tuple):
                # read_campaign refuses only with target_not_found.
                return campaign_action_result(
                    _SAVED_TOOL, url, "target_not_found", record[1], retry_safe=True
                )
            built = build_campaign(record) or {}
            target = {
                key: built[key]
                for key in ("kind", "uiLabel", "id", "accountId", "name", "status")
                if key in built
            }
            keys = targeting_keys(record.get("targetingCriteria"))
            if not keys:
                return campaign_action_result(
                    _SAVED_TOOL,
                    url,
                    "control_unavailable",
                    "Campaign Manager returned this ad set's targeting in a shape "
                    "this tool does not recognise, so no audience was saved.",
                    retry_safe=True,
                    target=target,
                )
            planned = {"name": name, "targeting": _targeting(keys)}
            await self._flow.wait_for_anchor(page, ANCHORS["save_audience"])
            async with without_campaign_manager_writes(page):
                dialog = await _open_save_dialog(page)
                if dialog is False:
                    return campaign_action_result(
                        _SAVED_TOOL,
                        url,
                        "audience_too_small",
                        "Campaign Manager does not allow saving this ad set's "
                        "audience, which it does when the audience is too small "
                        "(under 300 members). Nothing was saved.",
                        retry_safe=True,
                        target=target,
                        planned=planned,
                    )
                if dialog is None:
                    return campaign_action_result(
                        _SAVED_TOOL,
                        url,
                        "control_unavailable",
                        "Campaign Manager did not show its save audience dialog "
                        "with the expected controls, so nothing was saved.",
                        retry_safe=True,
                        target=target,
                        planned=planned,
                    )
                if not await _fill_save_dialog(page, dialog, name):
                    await _close_dialog(page, dialog)
                    return campaign_action_result(
                        _SAVED_TOOL,
                        url,
                        "control_unavailable",
                        "Campaign Manager did not accept the audience name in its "
                        "save dialog, so nothing was saved.",
                        retry_safe=True,
                        target=target,
                        planned=planned,
                    )
                if not confirm_send:
                    await _close_dialog(page, dialog)
                    return campaign_action_result(
                        _SAVED_TOOL,
                        url,
                        "confirmation_required",
                        f"Ready to save this ad set's targeting "
                        f"({len(keys)} values) as the saved audience {name!r}. "
                        + (
                            _LANGUAGE_MAY_BE_OMITTED
                            if any(key[0] == _LANGUAGE for key in keys)
                            else ""
                        )
                        + "Call again with confirm_send true to save it.",
                        retry_safe=True,
                        target=target,
                        planned=planned,
                    )

            admit = admit_saved_audience(
                account_id=account, name=name, target_keys=keys
            )
            async with self._flow.apply(page, admit) as window:
                after = capture.write_count
                clicked = await _submit(dialog, _save_submit(dialog))
                response = (
                    await self._flow.wait_saved(
                        capture,
                        lambda w: w.call.resource == _SAVED_RESOURCE,
                        after,
                        timeout=_SAVE_TIMEOUT,
                    )
                    if clicked
                    else None
                )

            outcome = _write_outcome(
                _SAVED_TOOL, url, window, response, target, planned
            )
            if outcome is not None:
                return outcome
            assert response is not None
            created = _created_number(response.created_id)
            after_list = await self._reread(
                lambda: self._saved_after_matched(capture, account), saved_audiences
            )

        found = _with_id(after_list, created)
        if created is None or found is None or found["name"] != name:
            return campaign_action_result(
                _SAVED_TOOL,
                url,
                "save_unverified",
                "LinkedIn accepted the saved audience, but it could not be found "
                "with that name in the ad account's saved audiences afterwards. "
                "Check list_audiences before saving again.",
                retry_safe=False,
                target=target,
                planned=planned,
                created_id=response.created_id,
            )
        omitted = saved_audience_omitted(response.body, keys)
        if _LANGUAGE in omitted:
            message = (
                f"Saved the saved audience {name!r} (id {created}), but LinkedIn "
                "saved it without this ad set's profile-language targeting, so "
                "it reaches a broader audience than the ad set."
            )
        else:
            message = (
                f"Saved this ad set's targeting as the saved audience {name!r} "
                f"(id {created})."
            )
        result = campaign_action_result(
            _SAVED_TOOL,
            url,
            "audience_created",
            message,
            retry_safe=retry_safe_from(window),
            target=target,
            planned=planned,
            applied=found,
            created_id=str(created),
            refused=window.refused,
        )
        if omitted:
            result["omitted_sections"] = omitted
        return result

    async def _saved_after_matched(
        self, capture: CampaignManagerCapture, account_id: str
    ) -> CmResponse | None:
        if await self._load_matched(capture, account_id) is None:
            return None
        return await self._load_saved(capture, account_id)

    @staticmethod
    async def _reread(
        load: Callable[[], Awaitable[CmResponse | None]],
        parse: Callable[[Any], Sequence[Any] | None],
    ) -> Sequence[Any] | None:
        """The audience list after a create, or None when it could not be read."""
        try:
            response = await load()
        except (InvalidReferenceError, LinkedInScraperException):
            logger.debug("Audience re-read failed", exc_info=True)
            return None
        return parse(response.payload) if response is not None else None


def _with_id(
    audiences: Sequence[MatchedAudience | SavedAudience] | None, audience_id: int | None
) -> Any:
    for audience in audiences or ():
        if audience["id"] == audience_id:
            return audience
    return None


def _targeting(keys: Sequence[TargetingKey]) -> list[dict[str, str]]:
    return [
        {"section": section, "facetUrn": facet, "segmentUrn": urn}
        for section, facet, urn in keys
    ]


def _request_structure(request: CmWriteRequest, reason: str) -> dict[str, Any]:
    """A refused request without its values: what the next fix needs to see."""
    segments = urlparse(request.url).path.split("/")
    return {
        "method": request.method,
        "restli_method": request.restli_method,
        "resource": request.call.resource,
        "finder": request.call.finder,
        "path": "/".join(s if is_field_name(s) else "{id}" for s in segments if s),
        "params": params_shape(request.call.params),
        "body": skeleton(request.body),
        "reason": reason,
    }


def _with_structure(result: dict[str, Any], window: WriteWindow) -> dict[str, Any]:
    if window.refused:
        result["refused_structure"] = [
            _request_structure(request, reason) for request, reason in window.refused
        ]
    return result


def _write_outcome(
    tool: str,
    url: str,
    window: WriteWindow,
    response: CmWriteResponse | None,
    target: Mapping[str, Any],
    planned: Mapping[str, Any],
) -> dict[str, Any] | None:
    """The result of a create that did not succeed; None when LinkedIn accepted it."""
    retry_safe = retry_safe_from(window)
    if response is None and window.refused:
        return _with_structure(
            campaign_action_result(
                tool,
                url,
                "change_blocked",
                "Campaign Manager tried to send a change outside the plan, which "
                "was stopped before it left the browser."
                + ("" if retry_safe else " Part of the plan was already sent."),
                retry_safe=retry_safe,
                target=target,
                planned=planned,
                refused=window.refused,
            ),
            window,
        )
    if response is None and retry_safe:
        return campaign_action_result(
            tool,
            url,
            "control_unavailable",
            "Campaign Manager sent nothing after the submit button, so nothing "
            "was created.",
            retry_safe=True,
            target=target,
            planned=planned,
        )
    if response is None:
        return campaign_action_result(
            tool,
            url,
            "save_unverified",
            "The request was sent but LinkedIn's answer did not arrive in time. "
            "Check list_audiences before trying again.",
            retry_safe=False,
            target=target,
            planned=planned,
        )
    if 400 <= response.status < 500:
        return campaign_action_result(
            tool,
            url,
            "save_failed",
            f"LinkedIn refused the request (HTTP {response.status}), so no "
            "audience was created.",
            retry_safe=True,
            target=target,
            planned=planned,
            refused=window.refused,
        )
    if not response.ok:
        return campaign_action_result(
            tool,
            url,
            "save_unverified",
            f"LinkedIn answered HTTP {response.status}; whether the audience "
            "was created is unknown. Check list_audiences before trying again.",
            retry_safe=False,
            target=target,
            planned=planned,
        )
    return None
