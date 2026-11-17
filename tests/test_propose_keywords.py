"""Tests for the keyword proposal of one resource: reading, validation, theme context, language, retries.

No network: the model is a ``FakeBackend`` and the run log is disabled. The database test loads a small record
written here with the real loader, so the stored shape is the real one.
"""

import json
import logging
import re

import pytest

from mqa_advisor.catalogue_db import connect, load_catalogue
from mqa_advisor.llm import (
    BackendResponse,
    FakeBackend,
    LLMCallError,
    LLMClient,
    RunLog,
)
from mqa_advisor.prompts import PromptError
from mqa_advisor.propose import (
    OWN_DATASET_PREFIX,
    Issue,
    ResourceText,
    ThemeResult,
    propose_keywords,
    read_resource,
    validate_keywords,
)
from mqa_advisor.vocabulary import load_vocabulary

THEME = "http://publications.europa.eu/resource/authority/data-theme/"
OWN = OWN_DATASET_PREFIX + "dataset/d1"
CONTEXT = {
    "dct": "http://purl.org/dc/terms/",
    "dcat": "http://www.w3.org/ns/dcat#",
}


def uri(code: str) -> str:
    return THEME + code


@pytest.fixture(scope="module")
def vocabulary():
    return load_vocabulary()


def resource(
    *,
    titles=(("eng", "Air quality"),),
    descriptions=(("eng", "Hourly NO2 values"),),
    keywords=(),
    entity_id=OWN,
    problem=None,
) -> ResourceText:
    return ResourceText(
        1,
        "d1",
        "dataset",
        entity_id,
        problem,
        tuple(titles),
        tuple(descriptions),
        (),
        tuple(keywords),
    )


def kw(*values: str, lang="eng"):
    return tuple((lang, v) for v in values)


def themes(existing=(), proposed=(), status="ok") -> ThemeResult:
    return ThemeResult(
        "d1",
        status,
        tuple(uri(c) for c in proposed),
        tuple(uri(c) for c in existing),
        None,
        1,
        (),
    )


def answer(*keywords: str, language="en") -> BackendResponse:
    return BackendResponse(
        output={"keywords": list(keywords), "language": language},
        raw_response="[]",
        input_tokens=100,
        cached_tokens=0,
        output_tokens=10,
    )


def run(item, vocabulary, *script, theme_result=None):
    backend = FakeBackend(list(script))
    with RunLog("cat", enabled=False) as run_log:
        result = propose_keywords(
            item, LLMClient(backend, run_log), theme_result, vocabulary
        )
    return result, backend


def asked_for(backend, call=0) -> int:
    return int(re.search(r"up to (\d+)", backend.requests[call].user).group(1))


# --- reading -----------------------------------------------------------------------------------


def test_read_keywords_of_the_own_entity_in_any_language(tmp_path):
    related = "https://example.org/other.html"
    folder = tmp_path / "data" / "PROD" / "cat"
    folder.mkdir(parents=True)
    graph = [
        {
            "@id": OWN,
            "@type": "dcat:Dataset",
            "dct:title": {"@value": "Air quality", "@language": "eng"},
            "dcat:keyword": [
                {"@value": "air", "@language": "eng"},
                {"@value": "luft", "@language": "deu"},
            ],
        },
        {
            "@id": related,
            "@type": "dcat:Dataset",
            "dct:title": {"@value": "Other", "@language": "eng"},
            "dcat:keyword": {"@value": "other keyword", "@language": "eng"},
        },
    ]
    (folder / "dataset_d1.jsonld").write_text(
        json.dumps({"@context": CONTEXT, "@graph": graph}), encoding="utf-8"
    )
    conn = connect(tmp_path / "mqa.sqlite")
    try:
        load_catalogue("PROD", "cat", conn=conn, data_dir=folder.parents[1])
        pk = conn.execute("SELECT resource_pk FROM resource").fetchone()[0]
        text = read_resource(conn, pk)
        assert sorted(text.keywords) == [("deu", "luft"), ("eng", "air")]
        # Proposing writes nothing.
        changes = conn.total_changes
        result, _ = run(text, load_vocabulary(), answer("Noise"))
        assert result.status == "ok"
        assert conn.total_changes == changes
    finally:
        conn.close()


# --- validation --------------------------------------------------------------------------------


def test_validate_valid_keywords_keep_their_case():
    assert validate_keywords(["Air quality", "NO2"], [], 10) == (
        ("Air quality", "NO2"),
        (),
    )


def test_validate_trims_and_drops_empty_and_duplicates():
    kept, warnings = validate_keywords(
        [" air QUALITY ", "", "  ", "Noise", "noise", "NOISE "], ["Air quality"], 10
    )
    assert (kept, warnings) == (("Noise",), ())


def test_validate_cuts_at_the_remaining_count():
    kept, warnings = validate_keywords(["a", "b", "c"], [], 2)
    assert kept == ("a", "b")
    assert len(warnings) == 1 and "1 dropped" in warnings[0]


# --- the call ----------------------------------------------------------------------------------


def test_valid_answer_and_prompt_content(vocabulary):
    item = resource(keywords=kw("Air quality", "Pollution"))
    result, backend = run(item, vocabulary, answer("Noise", "NO2"))
    assert result.status == "ok"
    assert result.keywords == ("Noise", "NO2")
    assert (result.calls, result.issues, result.existing_count) == (1, (), 2)
    assert result.prompt.name == "propose_keywords"
    request = backend.requests[0]
    assert "Air quality" in request.user and "Hourly NO2 values" in request.user
    assert "Language to write the keywords in: eng" in request.user
    assert asked_for(backend) == 8
    assert "Pollution" in request.user  # existing keywords are listed


def test_duplicates_blanks_and_whitespace_are_dropped(vocabulary):
    item = resource(keywords=kw("Air quality"))
    result, _ = run(
        item, vocabulary, answer(" air quality ", "", "Noise", " NOISE", "  ")
    )
    assert result.keywords == ("Noise",)


def test_over_the_cap_is_cut_with_a_warning(vocabulary, caplog):
    item = resource(keywords=kw(*[f"k{i}" for i in range(8)]))
    with caplog.at_level(logging.WARNING):
        result, backend = run(item, vocabulary, answer("a", "b", "c"))
    assert asked_for(backend) == 2
    assert result.keywords == ("a", "b")
    assert [i.severity for i in result.issues] == ["warning"]
    assert "cut" in caplog.text


def test_all_duplicates_are_retried_then_succeed(vocabulary):
    item = resource(keywords=kw("Air quality"))
    result, backend = run(item, vocabulary, answer("air quality"), answer("Noise"))
    assert (result.status, result.calls, result.keywords) == ("ok", 2, ("Noise",))
    assert len(backend.requests) == 2


def test_retries_exhausted_is_an_error(vocabulary, caplog):
    item = resource(keywords=kw("Air quality"))
    with caplog.at_level(logging.ERROR):
        result, backend = run(item, vocabulary, *[answer("AIR quality")] * 4)
    assert (result.status, result.calls, result.keywords) == ("error", 4, ())
    assert len(backend.requests) == 4
    assert result.issues[-1] == Issue("error", "d1", result.issues[-1].message)
    assert "d1" in caplog.text


def test_call_failure_is_an_error_without_retry(vocabulary):
    result, backend = run(resource(), vocabulary, LLMCallError("boom"))
    assert (result.status, result.calls, result.keywords) == ("error", 1, ())
    assert result.issues[-1].severity == "error"
    assert len(backend.requests) == 1


# --- the cap and the skips ---------------------------------------------------------------------


def test_ten_keywords_make_no_call(vocabulary):
    item = resource(keywords=kw(*[f"k{i}" for i in range(10)]))
    result, backend = run(item, vocabulary)
    assert (result.status, result.calls, result.issues) == ("skipped", 0, ())
    assert backend.requests == []


def test_keywords_in_another_language_count_toward_the_cap(vocabulary):
    item = resource(
        keywords=kw(*[f"k{i}" for i in range(6)]) + kw("a", "b", "c", "d", lang="fra")
    )
    result, backend = run(item, vocabulary)
    assert result.status == "skipped" and backend.requests == []


def test_no_title_is_skipped_with_a_warning(vocabulary):
    result, backend = run(resource(titles=()), vocabulary)
    assert result.status == "skipped" and backend.requests == []
    assert [i.severity for i in result.issues] == ["warning"]


def test_no_own_entity_is_skipped_with_a_warning(vocabulary):
    item = resource(entity_id=None, problem="no dataset entity")
    result, backend = run(item, vocabulary)
    assert result.status == "skipped" and backend.requests == []
    assert "no dataset entity" in result.issues[0].message


# --- theme context -----------------------------------------------------------------------------


def test_effective_themes_are_in_the_prompt(vocabulary):
    context = themes(existing=["ENVI"], proposed=["AGRI"])
    _, backend = run(resource(), vocabulary, answer("Noise"), theme_result=context)
    labels = {e.code: e.label for e in vocabulary.entries}
    line = re.search(r"Themes: (.*)", backend.requests[0].user).group(1)
    assert line == f"{labels['ENVI']}, {labels['AGRI']}"


@pytest.mark.parametrize(
    "theme_result",
    [None, themes(status="error"), themes(status="skipped")],
    ids=["no-result", "theme-call-failed", "no-themes"],
)
def test_no_theme_context(vocabulary, theme_result):
    result, backend = run(
        resource(), vocabulary, answer("Noise"), theme_result=theme_result
    )
    assert result.status == "ok"
    assert "Themes: none" in backend.requests[0].user


def test_failed_theme_call_ignores_the_existing_themes(vocabulary):
    context = themes(existing=["ENVI"], status="error")
    _, backend = run(resource(), vocabulary, answer("Noise"), theme_result=context)
    assert "Themes: none" in backend.requests[0].user


# --- language ----------------------------------------------------------------------------------


def test_tagged_title_gives_its_own_tag(vocabulary):
    result, _ = run(resource(), vocabulary, answer("Noise", language="en"))
    assert (result.lang, result.lang_inferred) == ("eng", False)


def test_untagged_title_uses_the_model_language(vocabulary, caplog):
    item = resource(
        titles=((None, "Air quality"),), descriptions=(("fra", "Valeurs horaires"),)
    )
    with caplog.at_level(logging.INFO):
        result, backend = run(item, vocabulary, answer("Noise", language="en"))
    assert (result.lang, result.lang_inferred) == ("en", True)
    assert "Language to write the keywords in: unknown" in backend.requests[0].user
    assert "inferred" in caplog.text


def test_prompt_cap_mismatch_raises_before_any_call(vocabulary, monkeypatch):
    from mqa_advisor import propose

    monkeypatch.setattr(propose, "KEYWORD_CAP", 7)
    backend = FakeBackend([])
    with RunLog("cat", enabled=False) as run_log:
        with pytest.raises(PromptError):
            propose_keywords(resource(), LLMClient(backend, run_log), None, vocabulary)
    assert backend.requests == []
