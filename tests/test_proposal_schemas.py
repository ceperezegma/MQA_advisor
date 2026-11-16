"""Tests for the output schemas of the theme/keyword proposals."""

import re

import pytest

from mqa_advisor import proposal_schemas
from mqa_advisor.proposal_schemas import output_schema, schema_keys

THEMES_V1 = {
    "type": "object",
    "properties": {
        "themes": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    },
    "required": ["themes"],
    "additionalProperties": False,
}

KEYWORDS_V1 = {
    "type": "object",
    "properties": {
        "keywords": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "language": {"type": "string", "pattern": "^[a-z]{2,3}$"},
    },
    "required": ["keywords", "language"],
    "additionalProperties": False,
}


def test_themes_v1_is_exactly_the_expected_schema():
    assert output_schema("propose_themes", 1) == THEMES_V1


def test_keywords_v1_is_exactly_the_expected_schema():
    assert output_schema("propose_keywords", 1) == KEYWORDS_V1


def test_schemas_have_no_max_items():
    # Structured outputs support minItems only as 0 or 1: the cap is enforced in code, not here.
    for key in schema_keys():
        assert "maxItems" not in str(output_schema(*key))


def test_language_accepts_two_or_three_lowercase_letters():
    pattern = output_schema("propose_keywords", 1)["properties"]["language"]["pattern"]
    for good in ("en", "eng", "de"):
        assert re.fullmatch(pattern, good)
    for bad in ("EN", "e", "engl", "e1", "", "en "):
        assert not re.fullmatch(pattern, bad)


def test_each_call_returns_an_independent_copy():
    first = output_schema("propose_keywords", 1)
    first["properties"]["keywords"]["minItems"] = 99
    first["required"].append("extra")
    assert output_schema("propose_keywords", 1) == KEYWORDS_V1


@pytest.mark.parametrize(
    ("name", "version"),
    [("unknown", 1), ("propose_themes", 2), ("propose_themes", 0)],
)
def test_unknown_name_or_version_raises(name, version):
    with pytest.raises(ValueError, match="schema"):
        output_schema(name, version)


def test_schema_keys_are_the_registered_pairs():
    assert schema_keys() == {("propose_themes", 1), ("propose_keywords", 1)}


def test_a_new_version_gets_its_own_schema_and_v1_is_unchanged(monkeypatch):
    v2_shape = {
        "type": "object",
        "properties": {"words": {"type": "array"}},
        "required": ["words"],
    }
    monkeypatch.setitem(
        proposal_schemas._BUILDERS, ("propose_keywords", 2), lambda: dict(v2_shape)
    )
    assert output_schema("propose_keywords", 2) == v2_shape
    assert output_schema("propose_keywords", 1) == KEYWORDS_V1
