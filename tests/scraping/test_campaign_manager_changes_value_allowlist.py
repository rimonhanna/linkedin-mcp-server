"""A refused list upload repeats only the app's known constants, never a word it saw.

A word-like value can still be a name or other private text, so only a fixed
allowlist per field is echoed. Synthetic data only.
"""

from __future__ import annotations

from .test_campaign_manager_changes_list_upload import (
    _admit,
    _reason,
    _segment,
    _signed,
)


def test_an_unknown_word_value_is_described_not_echoed():
    reason = _reason(_admit()(_signed(requestType="Examplename")))
    assert "requestType: UPLOAD expected, another constant seen" in reason
    assert "Examplename" not in reason

    admit = _admit()
    assert admit(_signed()) is None
    segment = _reason(admit(_segment(sourcePlatform="Private")))
    assert "sourcePlatform: LIST_UPLOAD expected, another constant seen" in segment
    assert "Private" not in segment


def test_a_known_constant_of_another_field_is_not_echoed():
    reason = _reason(_admit()(_signed(contentType="UPLOAD")))
    assert "contentType: text/csv expected, another constant seen" in reason


def test_a_known_constant_of_the_field_is_echoed():
    reason = _reason(_admit()(_signed(uploadType="MATCHED_AUDIENCE")))
    assert "uploadType: AUDIENCE_MATCHING expected, MATCHED_AUDIENCE seen" in reason
