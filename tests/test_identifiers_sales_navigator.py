"""Tests for the Sales Navigator company and list identifier helpers."""

import pytest

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.scraping.identifiers import (
    SALES_ACCOUNT_LISTS_URL,
    normalize_sales_company_id,
    normalize_sales_list_id,
    sales_account_list_url,
    sales_company_url,
)


class TestSalesNavigatorIdentifiers:
    @pytest.mark.parametrize(
        "value",
        [
            "100000001",
            " 100000001 ",
            "urn:li:fs_salesCompany:100000001",
            "urn:li:organization:100000001",
            "https://www.linkedin.com/sales/company/100000001",
            "https://www.linkedin.com/sales/company/100000001/",
            "linkedin.com/sales/company/100000001?trk=x",
            "/sales/company/100000001/people",
        ],
    )
    def test_company_id_accepts(self, value):
        assert normalize_sales_company_id(value) == "100000001"

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "example-company",
            "https://www.linkedin.com/company/example-company/",
            "https://www.linkedin.com/sales/lead/ACwAA1,NAME_SEARCH,x",
            "https://www.linkedin.com/sales/company/example-company",
            "https://www.linkedin.com/sales/company/../1",
            "https://example.com/sales/company/100000001",
            "urn:li:fs_salesCompany:",
            "urn:li:fs_salesCompany:abc",
            "urn:li:fs_salesProfile:100000001",
            "urn:li:organization:1/../2",
            "../1",
        ],
    )
    def test_company_id_rejects(self, value):
        with pytest.raises(InvalidReferenceError, match="company_id"):
            normalize_sales_company_id(value)

    @pytest.mark.parametrize(
        "value",
        [
            "100000002",
            "https://www.linkedin.com/sales/accounts/dashboard?listGroup=CUSTOM_LISTS&listId=100000002",
            "https://www.linkedin.com/sales/accounts/dashboard?listId=100000002&page=2",
            "https://www.linkedin.com/sales/lists/company?listId=100000002#top",
        ],
    )
    def test_list_id_accepts(self, value):
        assert normalize_sales_list_id(value) == "100000002"

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "abc",
            "../1",
            "https://www.linkedin.com/sales/accounts/dashboard?listId=abc",
            "https://www.linkedin.com/sales/accounts/dashboard",
            "https://www.linkedin.com/sales/accounts/dashboard?listId=1&listId=2",
            "https://www.linkedin.com/sales/lead/x?listId=100000002",
            "https://example.com/sales/accounts/dashboard?listId=100000002",
        ],
    )
    def test_list_id_rejects(self, value):
        with pytest.raises(InvalidReferenceError, match="list_id"):
            normalize_sales_list_id(value)

    def test_urls_round_trip(self):
        assert (
            sales_company_url("100000001")
            == "https://www.linkedin.com/sales/company/100000001"
        )
        assert normalize_sales_company_id(sales_company_url("100000001")) == "100000001"
        url = sales_account_list_url("100000002")
        assert url.endswith(
            "/sales/accounts/dashboard?listGroup=CUSTOM_LISTS&listId=100000002"
        )
        assert normalize_sales_list_id(url) == "100000002"
        assert SALES_ACCOUNT_LISTS_URL == "https://www.linkedin.com/sales/lists/company"
