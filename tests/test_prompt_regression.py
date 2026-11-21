"""Offline tests of the prompt regression examples and their checks (a fake backend, no network)."""

import pytest

from mqa_advisor.llm import BackendResponse, FakeBackend, LLMClient, RunLog
from mqa_advisor.propose import KEYWORD_CAP
from mqa_advisor.vocabulary import load_vocabulary
from tests.prompt_regression import (
    THEME_URI_PREFIX,
    check_example,
    load_examples,
    run_example,
)


@pytest.fixture(scope="module")
def vocabulary():
    return load_vocabulary()


@pytest.fixture(scope="module")
def examples():
    return {e.name: e for e in load_examples()}


def reply(output) -> BackendResponse:
    return BackendResponse(
        output=output,
        raw_response="[]",
        input_tokens=10,
        cached_tokens=0,
        output_tokens=5,
    )


def run(example, vocabulary, themes, keywords, language="en"):
    backend = FakeBackend(
        [
            reply({"themes": themes}),
            reply({"keywords": keywords, "language": language}),
        ]
    )
    with RunLog("cat", enabled=False) as run_log:
        result = run_example(example, LLMClient(backend, run_log), vocabulary)
    return result, backend


def test_there_are_eight_examples_covering_the_required_cases(examples, vocabulary):
    assert len(examples) == 8
    items = list(examples.values())
    assert any(e.resource_type == "dataservice" for e in items)
    assert any(e.existing_themes for e in items)
    assert any(not e.description for e in items)
    assert any(e.title_language is None for e in items)
    for e in items:
        assert e.accepted_themes <= set(vocabulary.uris_by_code), e.name
        assert (
            e.existing_themes
            and set(e.existing_themes) <= set(vocabulary.uris_by_code)
            or not e.existing_themes
        )


def test_a_good_answer_passes_every_check(examples, vocabulary):
    example = examples["lt-dataset-mineral-resources-no-description"]
    code = sorted(example.accepted_themes)[0]
    (themes, keywords), _ = run(example, vocabulary, [code], ["minerals", "geology"])
    assert check_example(example, themes, keywords, vocabulary) == []


def test_no_accepted_theme_names_the_example_and_property(examples, vocabulary):
    example = examples["lt-dataset-mineral-resources-no-description"]
    other = next(
        c
        for c in vocabulary.uris_by_code
        if c not in example.accepted_themes
        and c not in example.existing_themes
        and not c.startswith("OP_")
    )
    (themes, keywords), _ = run(example, vocabulary, [other], ["minerals"])
    [failure] = check_example(example, themes, keywords, vocabulary)
    assert example.name in failure and "accepted themes" in failure


def test_unknown_codes_leave_no_theme_and_fail(examples, vocabulary):
    example = examples["lt-dataset-mineral-resources-no-description"]
    backend = FakeBackend(
        [reply({"themes": ["XXXX"]})] * 4
        + [reply({"keywords": ["a"], "language": "en"})]
    )
    with RunLog("cat", enabled=False) as run_log:
        themes, keywords = run_example(example, LLMClient(backend, run_log), vocabulary)
    failures = check_example(example, themes, keywords, vocabulary)
    assert any("themes: status error" in f for f in failures)


def test_a_tagged_title_keeps_its_tag_as_the_keyword_language(examples, vocabulary):
    example = examples["lt-dataservice-bg-title"]
    code = sorted(example.accepted_themes)[0]
    (themes, keywords), _ = run(
        example, vocabulary, [code], ["данни", "услуга"], language="bg"
    )
    assert check_example(example, themes, keywords, vocabulary) == []


def test_keywords_not_in_the_title_script_fail(examples, vocabulary):
    example = examples["lt-dataservice-bg-title"]
    code = sorted(example.accepted_themes)[0]
    (themes, keywords), _ = run(
        example, vocabulary, [code], ["data", "service"], language="bg"
    )
    [failure] = check_example(example, themes, keywords, vocabulary)
    assert example.name in failure and "title's language" in failure


def test_an_untagged_title_needs_a_non_english_inferred_language(examples, vocabulary):
    example = examples["es-dataset-untagged-title"]
    code = sorted(example.accepted_themes)[0]
    ok, _ = run(example, vocabulary, [code], ["aparcamientos"], language="es")
    assert check_example(example, *ok, vocabulary) == []
    bad, _ = run(example, vocabulary, [code], ["parking"], language="en")
    [failure] = check_example(example, *bad, vocabulary)
    assert example.name in failure and "keyword language" in failure


def test_count_above_the_cap_fails(examples, vocabulary):
    # An example with 5 existing keywords leaves room for 5: a fake answer cannot exceed it after validation,
    # so check the check directly with a result built by hand.
    from dataclasses import replace

    example = examples["lt-dataset-medicinal-products"]
    code = sorted(example.accepted_themes)[0]
    (themes, keywords), _ = run(example, vocabulary, [code], ["a", "b"])
    too_many = replace(keywords, keywords=tuple("abcdefghijk"))
    [failure] = check_example(example, themes, too_many, vocabulary)
    assert (
        "number of new keywords" in failure
        and f"1..{KEYWORD_CAP - len(example.existing_keywords)}" in failure
    )


def test_the_example_resource_builds_the_prompt_input(examples):
    resource = examples["lt-dataset-mineral-resources-no-description"].resource()
    assert resource.descriptions == ()
    assert resource.theme_uris == (THEME_URI_PREFIX + "REGI",)


def test_diacritics_fail_the_ascii_check_of_an_english_title(examples, vocabulary):
    example = examples["lt-dataservice-meteorology"]
    code = sorted(example.accepted_themes)[0]
    (themes, keywords), _ = run(example, vocabulary, [code], ["žemės ūkis"])
    [failure] = check_example(example, themes, keywords, vocabulary)
    assert example.name in failure and "title's language" in failure
