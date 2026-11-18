"""The new list audience's id from the create's body, as the upload dialog reads it."""

from linkedin_mcp_server.scraping.campaign_manager_audiences import _segment_id


def test_segment_id_is_read_from_ad_segment_id():
    assert _segment_id({"adSegmentId": 100000502, "name": "x"}) == 100000502
    assert _segment_id({"adSegmentId": "urn:li:adSegment:100000502"}) == 100000502


def test_segment_id_is_none_without_a_numeric_ad_segment_id():
    assert _segment_id(None) is None
    assert _segment_id({}) is None
    assert _segment_id({"adSegmentId": True}) is None
    assert _segment_id({"adSegmentId": "pending"}) is None
