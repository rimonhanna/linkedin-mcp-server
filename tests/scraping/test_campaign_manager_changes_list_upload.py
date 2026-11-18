"""The company list upload admission against the app's real requests.

Bodies are the ones the upload dialog builds (app bundle, 2026-11:
``ambry.uploadCsvFileToAmbry`` and ``createListAudienceTask``), both sent with
``xhr.post``, so without a Rest.li method header.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from linkedin_mcp_server.scraping.campaign_manager_changes import admit_list_upload
from linkedin_mcp_server.scraping.campaign_manager_payload import CmCall

ACCOUNT = "100000001"
OWNER = f"urn:li:sponsoredAccount:{ACCOUNT}"
NAME = "Example list"
FILE = "example-list.csv"
SIGNED = "campaignManagerAmbrySignedUrl"
SEGMENT = "campaignManagerDmpSegments"


@dataclass(frozen=True)
class Req:
    call: CmCall
    url: str
    method: str
    restli_method: str | None
    body: Any
    method_override: str | None = None


def _req(resource: str, body: Any) -> Req:
    url = f"https://www.linkedin.com/campaign-manager-api/{resource}"
    return Req(CmCall(resource, None, None, {}), url, "POST", None, body)


def _signed(**changes: Any) -> Req:
    body = {
        "owner": OWNER,
        "requestType": "UPLOAD",
        "uploadType": "AUDIENCE_MATCHING",
        "contentType": "text/csv",
    }
    body.update(changes)
    return _req(SIGNED, {k: v for k, v in body.items() if v is not None})


def _segment(**changes: Any) -> Req:
    body = {
        "inputFileName": FILE,
        "inputFile": "urn:li:media:example",
        "account": OWNER,
        "name": NAME,
        "sourcePlatform": "LIST_UPLOAD",
        "segmentType": "COMPANY",
    }
    body.update(changes)
    return _req(SEGMENT, {k: v for k, v in body.items() if v is not None})


def _admit():
    return admit_list_upload(account_id=ACCOUNT, name=NAME, file_name=FILE)


def _reason(value: str | None) -> str:
    assert value is not None, "the request was admitted"
    return value


def test_the_dialogs_real_requests_are_admitted_once_in_order():
    admit = _admit()
    assert admit(_signed()) is None
    assert admit(_segment()) is None
    assert "second" in _reason(admit(_segment()))


def test_the_earlier_guessed_upload_type_is_refused_and_named():
    # The live refusal: the rule expected the constant's name, not its value.
    reason = _reason(_admit()(_signed(uploadType="MATCHED_AUDIENCE")))
    assert reason.startswith("upload URL request is not this ad account's")
    assert "uploadType: AUDIENCE_MATCHING expected, MATCHED_AUDIENCE seen" in reason
    assert "owner" not in reason and "contentType" not in reason


def test_signed_url_mismatches_name_the_field_without_its_id():
    reason = _reason(
        _admit()(_signed(owner="urn:li:sponsoredAccount:100000099", contentType="csv"))
    )
    assert (
        "owner: urn of type sponsoredAccount for this ad account expected, "
        "urn of type sponsoredAccount for another ad account seen"
    ) in reason
    assert "contentType: text/csv expected, another constant seen" in reason
    assert "csv seen" not in reason
    assert "100000099" not in reason
    org = _reason(_admit()(_signed(owner="urn:li:organization:100000099")))
    assert "urn of type organization seen" in org
    assert "100000099" not in org
    missing = _reason(_admit()(_signed(contentType=None, extra="x")))
    assert "contentType: text/csv expected, nothing seen" in missing
    assert "extra: not sent by the app" in missing


def test_segment_must_follow_its_signed_url():
    assert "before its upload URL" in _reason(_admit()(_segment()))


def test_segment_is_tied_to_this_account_list_and_file():
    def refused(**changes: Any) -> str:
        admit = _admit()
        assert admit(_signed()) is None
        return _reason(admit(_segment(**changes)))

    assert "account: urn of type sponsoredAccount" in refused(
        account="urn:li:sponsoredAccount:100000099"
    )
    other_name = refused(name="Private name")
    assert "name: the planned name expected" in other_name
    assert "Private name" not in other_name
    assert "inputFileName: the planned file expected" in refused(
        inputFileName="other.csv"
    )
    assert "inputFile: urn of type media expected" in refused(
        inputFile="https://example.com/x"
    )
    member = refused(segmentType="MEMBER")
    assert "segmentType: COMPANY expected, another constant seen" in member
    assert "MEMBER" not in member
    assert "sourceType: not sent by the app" in refused(sourceType="CONVERSION")
