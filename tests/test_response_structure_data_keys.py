"""A map keyed by data shows only its size, never its keys (synthetic data)."""

from __future__ import annotations

import json

from linkedin_mcp_server.response_structure import skeleton

NAMES = ("Jane", "Example", "Python", "Leadership")


def test_a_map_keyed_by_personal_looking_words_names_no_key():
    payload = {
        "endorsements": {name: {"count": 3} for name in NAMES},
        "paging": {"total": 4},
    }

    rendered = json.dumps(skeleton(payload))

    assert [name for name in NAMES if name in rendered] == []
    assert skeleton(payload) == {
        "endorsements": "dict(4 keys)",
        "paging": {"total": "int"},
    }


def test_many_lowercase_single_word_keys_are_summarised():
    words = [f"member{chr(97 + i)}{chr(97 + i // 26)}" for i in range(30)]
    payload = {"byName": dict.fromkeys(words, 1)}

    assert skeleton(payload) == {"byName": "dict(30 keys)"}


def test_known_field_skeletons_stay_intact():
    payload = {
        "$type": "com.linkedin.Example",
        "*elements": ["urn:li:x:1"],
        "com.linkedin.voyager.Foo": {"entityUrn": "urn:li:x:1", "first_name": "x"},
    }

    assert skeleton(payload) == {
        "$type": "str",
        "*elements": {"list[1]": "str"},
        "com.linkedin.voyager.Foo": {"entityUrn": "str", "first_name": "str"},
    }
