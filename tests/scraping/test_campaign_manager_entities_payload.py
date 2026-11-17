"""Tests for Campaign Manager entity payloads and the company list CSV (synthetic)."""

import csv
import io

import pytest

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.scraping import campaign_manager_entities_payload as module
from linkedin_mcp_server.scraping.campaign_manager_entities_payload import (
    ACCOUNT_TEMPLATE_HEADERS,
    MAX_COMPANY_ROWS,
    company_list_csv,
    content_library_entries,
    matched_audiences,
    render_audiences,
    render_entities,
    saved_audiences,
    typeahead_entities,
)


def _parse(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text, newline="")))


class TestTypeahead:
    def test_rows(self):
        payload = {
            "elements": [
                {
                    "urn": "urn:li:title:100",
                    "facetUrn": "urn:li:adTargetingFacet:titles",
                    "name": "Example title",
                    "subLine": "Example line",
                    "targetable": True,
                },
                {
                    "urn": "urn:li:title:101",
                    "facetUrn": "urn:li:adTargetingFacet:titles",
                    "name": "Other title",
                },
            ]
        }
        entities = typeahead_entities(payload)
        assert entities == [
            {
                "urn": "urn:li:title:100",
                "name": "Example title",
                "facetUrn": "urn:li:adTargetingFacet:titles",
                "description": "Example line",
                "targetable": True,
            },
            {
                "urn": "urn:li:title:101",
                "name": "Other title",
                "facetUrn": "urn:li:adTargetingFacet:titles",
                "targetable": False,
            },
        ]
        assert "not targetable" in render_entities(entities or [])

    def test_empty_and_wrong_shapes(self):
        assert typeahead_entities({"elements": []}) == []
        assert typeahead_entities([]) is None
        assert typeahead_entities({"elements": "x"}) is None
        assert typeahead_entities({"elements": [{"name": "no urn"}]}) is None


def _library(elements, included):
    return {"data": {"elements": elements}, "included": included}


class TestContentLibrary:
    def test_resolves_in_order(self):
        payload = _library(
            [
                {"content": {"*sponsoredUpdateContent": "urn:li:share:2"}},
                {"content": {"*sponsoredUpdateContent": "urn:li:ugcPost:1"}},
            ],
            [
                {
                    "entityUrn": "urn:li:ugcPost:1",
                    "sponsorable": False,
                    "nonSponsorableReasons": ["SOME_REASON"],
                    "visibility": "ORGANIC",
                },
                {
                    "entityUrn": "urn:li:share:2",
                    "sponsorable": True,
                    "visibility": "DARK",
                    "userGeneratedAdContent": {"contentType": "IMAGE"},
                    "state": {"lifecycleState": "PUBLISHED"},
                },
            ],
        )
        assert content_library_entries(payload) == [
            {
                "urn": "urn:li:share:2",
                "sponsorable": True,
                "nonSponsorableReasons": [],
                "visibility": "DARK",
                "contentType": "IMAGE",
                "lifecycleState": "PUBLISHED",
            },
            {
                "urn": "urn:li:ugcPost:1",
                "sponsorable": False,
                "nonSponsorableReasons": ["SOME_REASON"],
                "visibility": "ORGANIC",
            },
        ]

    def test_empty_and_wrong_shapes(self):
        assert content_library_entries(_library([], [])) == []
        assert content_library_entries({"elements": []}) is None
        assert content_library_entries("x") is None
        unresolved = _library(
            [{"content": {"*sponsoredUpdateContent": "urn:li:share:9"}}], []
        )
        assert content_library_entries(unresolved) is None
        bad = _library(
            [{"content": {"*sponsoredUpdateContent": "urn:li:share:9"}}],
            [{"entityUrn": "urn:li:share:9", "sponsorable": "yes"}],
        )
        assert content_library_entries(bad) is None


class TestAudiences:
    def test_matched(self):
        payload = {
            "elements": [
                {
                    "id": 700000001,
                    "name": "Example list",
                    "segmentType": "COMPANY",
                    "maSegmentType": "BULK",
                    "maSegmentStatus": "READY",
                    "audienceCount": 1200,
                },
                {"id": 700000002, "name": "Bare"},
            ]
        }
        audiences = matched_audiences(payload)
        assert audiences == [
            {
                "kind": "matched_audience",
                "id": 700000001,
                "segmentUrn": "urn:li:adSegment:700000001",
                "name": "Example list",
                "listType": "COMPANY",
                "source": "BULK",
                "status": "READY",
                "audienceCount": 1200,
            },
            {
                "kind": "matched_audience",
                "id": 700000002,
                "segmentUrn": "urn:li:adSegment:700000002",
                "name": "Bare",
            },
        ]
        assert "urn:li:adSegment:700000001" in render_audiences(audiences or [])

    def test_saved_skips_linkedin_templates(self):
        payload = {
            "elements": [
                {"type": "LINKEDIN_AUDIENCE", "id": -5, "name": "Template"},
                {
                    "type": "USER_AUDIENCE",
                    "id": 800000001,
                    "name": "Example saved",
                    "description": "Example description",
                    "audienceCount": 5000,
                    "lastModifiedAt": 1700000000000,
                    "lastAppliedAt": 1700000100000,
                },
            ]
        }
        audiences = saved_audiences(payload)
        assert audiences == [
            {
                "kind": "saved_audience",
                "id": 800000001,
                "name": "Example saved",
                "description": "Example description",
                "audienceCount": 5000,
                "lastModifiedAt": 1700000000000,
                "lastAppliedAt": 1700000100000,
            }
        ]
        assert "Example saved (id 800000001)" in render_audiences(audiences or [])

    def test_empty_and_wrong_shapes(self):
        assert matched_audiences({"elements": []}) == []
        assert saved_audiences({"elements": []}) == []
        assert matched_audiences(None) is None
        assert saved_audiences({"elements": 3}) is None
        assert matched_audiences({"elements": [{"id": "x", "name": "n"}]}) is None
        assert (
            saved_audiences({"elements": [{"type": "USER_AUDIENCE", "id": 1}]}) is None
        )


class TestCompanyListCsv:
    def test_header_row_first_and_columns(self):
        text = company_list_csv(
            [
                {
                    "name": "Example Co",
                    "website": "https://example.com",
                    "linkedin_url": "https://www.linkedin.com/company/example-co",
                },
                {"name": "Only Name"},
            ],
            ACCOUNT_TEMPLATE_HEADERS,
        )
        assert text.endswith("\r\n")
        assert text.splitlines()[0] == ",".join(ACCOUNT_TEMPLATE_HEADERS)
        rows = _parse(text)
        assert rows[1][:4] == [
            "Example Co",
            "https://example.com",
            "",
            "https://www.linkedin.com/company/example-co/",
        ]
        assert rows[2] == ["Only Name"] + [""] * 9
        assert all(len(row) == len(ACCOUNT_TEMPLATE_HEADERS) for row in rows)

    def test_template_headers_match_linkedins_file(self):
        assert ACCOUNT_TEMPLATE_HEADERS[:4] == (
            "companyname",
            "companywebsite",
            "companyemaildomain",
            "linkedincompanypageurl",
        )
        assert len(ACCOUNT_TEMPLATE_HEADERS) == 10

    def test_quoting_round_trips(self):
        text = company_list_csv(
            [{"name": 'Example, "Quoted" Co'}], ACCOUNT_TEMPLATE_HEADERS
        )
        assert _parse(text)[1][0] == 'Example, "Quoted" Co'
        assert '"Example, ""Quoted"" Co"' in text

    @pytest.mark.parametrize("prefix", ["=", "+", "-", "@"])
    def test_formula_prefix_is_neutralised(self, prefix):
        text = company_list_csv([{"name": f"{prefix}SUM(1)"}], ACCOUNT_TEMPLATE_HEADERS)
        assert _parse(text)[1][0] == f"'{prefix}SUM(1)"

    def test_dedupe_by_name_and_page(self):
        text = company_list_csv(
            [
                {"name": "Example Co"},
                {"name": "EXAMPLE CO"},
                {"name": "Example Co", "linkedin_url": "example-co"},
                {
                    "name": "example co",
                    "linkedin_url": "https://www.linkedin.com/company/example-co/",
                },
            ],
            ACCOUNT_TEMPLATE_HEADERS,
        )
        assert len(_parse(text)) == 3

    @pytest.mark.parametrize(
        "row",
        [
            {"name": ""},
            {"name": "   "},
            {"name": "Bad\x00Name"},
            {"name": "Bad\nName"},
            {"name": "Bad\x7fName"},
            {"name": "Ok", "website": "javascript:alert(1)"},
            {"name": "Ok", "website": "https://user:pw@example.com"},
            {"name": "Ok", "linkedin_url": "https://www.linkedin.com/in/someone/"},
            {"name": 5},
            {},
        ],
    )
    def test_refuses_bad_rows(self, row):
        with pytest.raises(InvalidReferenceError):
            company_list_csv([row], ACCOUNT_TEMPLATE_HEADERS)

    def test_refuses_empty_input(self):
        with pytest.raises(InvalidReferenceError, match="empty"):
            company_list_csv([], ACCOUNT_TEMPLATE_HEADERS)

    def test_row_limit(self):
        rows = [{"name": f"Example {i}"} for i in range(MAX_COMPANY_ROWS)]
        assert len(_parse(company_list_csv(rows, ACCOUNT_TEMPLATE_HEADERS))) == (
            MAX_COMPANY_ROWS + 1
        )
        with pytest.raises(InvalidReferenceError, match="at most"):
            company_list_csv([*rows, {"name": "One more"}], ACCOUNT_TEMPLATE_HEADERS)

    def test_size_limit(self, monkeypatch):
        monkeypatch.setattr(module, "MAX_CSV_BYTES", 200)
        with pytest.raises(InvalidReferenceError, match="too large"):
            company_list_csv([{"name": "x" * 300}], ACCOUNT_TEMPLATE_HEADERS)

    def test_headers_must_have_name_column(self):
        with pytest.raises(InvalidReferenceError, match="name column"):
            company_list_csv([{"name": "A"}], ("other",))
