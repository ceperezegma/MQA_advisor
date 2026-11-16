"""Tests for the output schemas of the theme/keyword proposals."""

import re

import pytest

from mqa_advisor.proposal_schemas import output_schema


def test_themes_schema_limits_are_passed_per_call():
    schema = output_schema("propose_themes", 1, 3)
    themes = schema["properties"]["themes"]
    assert themes["minItems"] == 1
    assert themes["maxItems"] == 3
    assert schema["required"] == ["themes"]
    # A later call with another limit is not affected by the first one.
    assert (
        output_schema("propose_themes", 1, 4)["properties"]["themes"]["maxItems"] == 4
    )


def test_each_call_returns_an_independent_copy():
    first = output_schema("propose_keywords", 1, 5)
    first["properties"]["keywords"]["maxItems"] = 99
    first["required"].append("extra")
    second = output_schema("propose_keywords", 1, 5)
    assert second["properties"]["keywords"]["maxItems"] == 5
    assert second["required"] == ["keywords", "language"]


def test_keywords_schema_requires_a_three_letter_language():
    schema = output_schema("propose_keywords", 1, 10)
    assert schema["required"] == ["keywords", "language"]
    assert schema["properties"]["keywords"]["maxItems"] == 10
    pattern = schema["properties"]["language"]["pattern"]
    assert re.fullmatch(pattern, "deu")
    for bad in ("de", "deut", "DEU", "de1", ""):
        assert not re.fullmatch(pattern, bad)


@pytest.mark.parametrize(
    ("name", "version"),
    [("unknown", 1), ("propose_themes", 2), ("propose_themes", 0)],
)
def test_unknown_name_or_version_raises(name, version):
    with pytest.raises(ValueError, match="schema"):
        output_schema(name, version, 3)


@pytest.mark.parametrize("max_items", [0, -1])
def test_max_items_below_one_raises(max_items):
    with pytest.raises(ValueError, match="max_items"):
        output_schema("propose_themes", 1, max_items)
