"""Tests for the theme proposal of one resource (``propose``): reading, language, validation, retries.

No network: the model is a ``FakeBackend`` and the run log is disabled. Databases are temporary, loaded from
small records written by the tests with the real loader, so the stored shapes are the real ones.
"""

import json
import logging
import re

import pytest

from mqa_advisor import propose
from mqa_advisor.catalogue_db import connect, load_catalogue
from mqa_advisor.llm import (
    BackendResponse,
    FakeBackend,
    LLMCallError,
    LLMClient,
    LLMInputError,
    LLMResponseError,
    RunLog,
)
from mqa_advisor.prompts import PromptError, active_version, load_prompt
from mqa_advisor.propose import (
    OWN_DATASET_PREFIX,
    ResourceText,
    choose_language,
    existing_themes,
    own_entity,
    propose_themes,
    read_resource,
    validate_codes,
)
from mqa_advisor.vocabulary import load_vocabulary

THEME = "http://publications.europa.eu/resource/authority/data-theme/"
OWN = OWN_DATASET_PREFIX + "dataset/d1"
RELATED = "https://example.org/catalogue/d/other.html"
OUTSIDE = "http://example.org/theme/local"
CONTEXT = {
    "dct": "http://purl.org/dc/terms/",
    "dcat": "http://www.w3.org/ns/dcat#",
    "skos": "http://www.w3.org/2004/02/skos/core#",
}


def uri(code: str) -> str:
    return THEME + code


@pytest.fixture(scope="module")
def vocabulary():
    return load_vocabulary()


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "mqa.sqlite")
    yield connection
    connection.close()


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "data" / "PROD" / "cat"
    path.mkdir(parents=True)
    return path


def _load(conn, folder):
    load_catalogue("PROD", "cat", conn=conn, data_dir=folder.parents[1])


def _pk(conn, resource_id):
    return conn.execute(
        "SELECT resource_pk FROM resource WHERE resource_id = ?", (resource_id,)
    ).fetchone()[0]


def _write_dataset(folder, resource_id, graph):
    path = folder / f"dataset_{resource_id}.jsonld"
    path.write_text(
        json.dumps({"@context": CONTEXT, "@graph": graph}), encoding="utf-8"
    )


def _write_service(folder, record):
    path = folder / f"dataservice_{record['id']}.json"
    path.write_text(
        json.dumps({"status": "success", "result": record}), encoding="utf-8"
    )


def _dataset_with_related_dataset(folder):
    """A record holding its own Dataset, a related Dataset and a Distribution, each with its own text."""
    _write_dataset(
        folder,
        "d1",
        [
            {
                "@id": OWN,
                "@type": "dcat:Dataset",
                "dct:title": {"@value": "Air quality", "@language": "eng"},
                "dct:description": {"@value": "Valeurs horaires", "@language": "fra"},
                "dcat:theme": [{"@id": uri("ENVI")}, {"@id": OUTSIDE}],
                "dcat:distribution": {"@id": "https://example.org/dist/1"},
            },
            {"@id": uri("ENVI"), "@type": "skos:Concept"},
            {
                "@id": "https://example.org/dist/1",
                "@type": "dcat:Distribution",
                "dct:title": {"@value": "CSV file", "@language": "eng"},
                "dct:description": {"@value": "Download", "@language": "eng"},
            },
            {
                "@id": RELATED,
                "@type": "dcat:Dataset",
                "dct:title": {"@value": "Other dataset", "@language": "eng"},
                "dct:description": {"@value": "Other text", "@language": "eng"},
                "dcat:theme": {"@id": uri("AGRI")},
            },
        ],
    )


def resource(
    *,
    titles=(("eng", "Air quality"),),
    descriptions=(("eng", "Hourly NO2 values"),),
    themes=(),
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
        tuple(themes),
    )


def answer(*codes: str) -> BackendResponse:
    return BackendResponse(
        output={"themes": list(codes)},
        raw_response="[]",
        input_tokens=100,
        cached_tokens=0,
        output_tokens=10,
    )


def run(item, vocabulary, *script):
    """Propose themes for ``item`` with a fake model answering ``script``; return the result and the backend."""
    backend = FakeBackend(list(script))
    with RunLog("cat", enabled=False) as run_log:
        result = propose_themes(item, LLMClient(backend, run_log), vocabulary)
    return result, backend


def asked_for(backend, call=0) -> int:
    """The number of new themes a request asked for (the ``$remaining`` value in the user part)."""
    return int(re.search(r"up to (\d+)", backend.requests[call].user).group(1))


# --- own entity --------------------------------------------------------------------------------


def test_own_entity_of_a_dataset_ignores_the_related_dataset():
    assert own_entity("dataset", [RELATED, OWN]) == (OWN, None)


def test_dataset_without_an_own_entity():
    entity, problem = own_entity("dataset", [RELATED])
    assert entity is None
    assert OWN_DATASET_PREFIX in problem


def test_dataset_with_two_own_entities():
    entity, problem = own_entity("dataset", [OWN, OWN_DATASET_PREFIX + "dataset/d2"])
    assert entity is None
    assert problem.startswith("2 dataset entities with an id starting with")


def test_own_entity_of_a_data_service():
    assert own_entity("dataservice", ["svc1"]) == ("svc1", None)


# --- reading the database ----------------------------------------------------------------------


def test_read_dataset_uses_only_its_own_entity(conn, folder):
    _dataset_with_related_dataset(folder)
    _load(conn, folder)
    text = read_resource(conn, _pk(conn, "d1"))
    assert (text.resource_id, text.resource_type, text.entity_id) == (
        "d1",
        "dataset",
        OWN,
    )
    assert text.titles == (("eng", "Air quality"),)
    assert text.descriptions == (("fra", "Valeurs horaires"),)
    # ENVI is stored as a node (the concept is in the graph), the other theme as a uri.
    kinds = conn.execute(
        "SELECT value_kind FROM property_value WHERE entity_id = ? AND property_uri LIKE '%#theme'"
        " ORDER BY value_kind",
        (OWN,),
    ).fetchall()
    assert kinds == [("node",), ("uri",)]
    assert sorted(text.theme_uris) == sorted([uri("ENVI"), OUTSIDE])


def test_read_dataset_without_an_own_entity(conn, folder):
    _write_dataset(
        folder,
        "d2",
        [{"@id": RELATED, "@type": "dcat:Dataset", "dct:title": "Other"}],
    )
    _load(conn, folder)
    text = read_resource(conn, _pk(conn, "d2"))
    assert text.entity_id is None
    assert text.titles == ()
    assert OWN_DATASET_PREFIX in text.problem


def test_read_data_service_theme_shape(conn, folder):
    _write_service(
        folder,
        {
            "id": "svc1",
            "title": {"eng": "Traffic API"},
            "description": {"eng": "Live traffic counts"},
            "theme": [{"resource": uri("TRAN")}],
        },
    )
    _load(conn, folder)
    text = read_resource(conn, _pk(conn, "svc1"))
    assert (text.entity_id, text.titles, text.theme_uris) == (
        "svc1",
        (("eng", "Traffic API"),),
        (uri("TRAN"),),
    )


def test_read_dataset_with_two_own_entities(conn, folder):
    _write_dataset(
        folder,
        "d3",
        [
            {"@id": OWN, "@type": "dcat:Dataset", "dct:title": "One"},
            {"@id": OWN_DATASET_PREFIX + "dataset/d3", "@type": "dcat:Dataset"},
        ],
    )
    _load(conn, folder)
    text = read_resource(conn, _pk(conn, "d3"))
    assert text.entity_id is None
    assert text.problem.startswith("2 dataset entities")
    assert text.titles == ()


def test_read_data_service_with_two_entities(conn, folder):
    _write_service(folder, {"id": "svc1", "title": {"eng": "Traffic API"}})
    _load(conn, folder)
    pk = _pk(conn, "svc1")
    # The mapper writes one entity per Data Service; a second one can only come from a broken database.
    conn.execute(
        "INSERT INTO property_value (resource_pk, entity_id, entity_kind, property_uri, value, value_kind)"
        " VALUES (?, 'svc2', 'dataservice', ?, 'Other', 'literal')",
        (pk, propose.DCT_TITLE),
    )
    text = read_resource(conn, pk)
    assert text.entity_id is None
    assert "2 dataservice entities" in text.problem


def test_read_ignores_non_text_titles_and_literal_themes(conn, folder, caplog):
    _write_dataset(
        folder,
        "d4",
        [
            {
                "@id": OWN_DATASET_PREFIX + "dataset/d4",
                "@type": "dcat:Dataset",
                "dct:title": [
                    {"@value": "Air quality", "@language": ""},
                    {"@id": "https://example.org/title"},
                ],
                "dcat:theme": "ENVI",
            }
        ],
    )
    _load(conn, folder)
    with caplog.at_level(logging.DEBUG, logger="mqa_advisor.propose"):
        text = read_resource(conn, _pk(conn, "d4"))
    assert text.titles == ((None, "Air quality"),)  # an empty tag is no tag
    assert text.theme_uris == ()
    assert sum("ignored" in r.getMessage() for r in caplog.records) == 2


def test_read_unknown_resource_raises(conn):
    with pytest.raises(ValueError, match="no resource"):
        read_resource(conn, 999)


# --- language ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("titles", "descriptions", "expected"),
    [
        pytest.param(
            [("fra", "Qualité de l'air"), ("eng", "Air quality")],
            [("fra", "Valeurs"), ("eng", "Values")],
            ("Air quality", "Values", "eng"),
            id="english-present",
        ),
        pytest.param(
            [("en", "Air quality")],
            [("eng", "Values")],
            ("Air quality", "Values", "en"),
            id="en-and-eng-are-english",
        ),
        pytest.param(
            [("fra", "Qualité de l'air")],
            [("fra", "Valeurs")],
            ("Qualité de l'air", "Valeurs", "fra"),
            id="non-english-title",
        ),
        pytest.param(
            [("eng", "Air quality")],
            [("fra", "Valeurs")],
            ("Air quality", "(language: fra) Valeurs", "eng"),
            id="description-in-another-language",
        ),
        pytest.param(
            [("fra", "Qualité"), ("deu", "Qualität")],
            [("deu", "Werte"), ("fra", "Valeurs")],
            ("Qualität", "Werte", "deu"),
            id="two-title-tags",
        ),
        pytest.param(
            [(None, "Untagged"), ("fra", "Qualité")],
            [("fra", "Valeurs")],
            ("Qualité", "Valeurs", "fra"),
            id="untagged-title-only-as-last-resort",
        ),
        pytest.param(
            [(None, "Air quality")],
            [("deu", "Werte")],
            ("Air quality", "(language: deu) Werte", None),
            id="untagged-title",
        ),
        pytest.param(
            [("eng", "Air quality")],
            [(None, "Values")],
            ("Air quality", "Values", "eng"),
            id="untagged-description-has-no-prefix",
        ),
        pytest.param(
            [("eng", "Air quality")],
            [],
            ("Air quality", "", "eng"),
            id="no-description",
        ),
        pytest.param(
            [("ENG", "Air quality")],
            [("en", "Values")],
            ("Air quality", "Values", "ENG"),
            id="upper-case-english-tag",
        ),
        pytest.param(
            [("FRA", "Qualité")],
            [("fra", "Valeurs")],
            ("Qualité", "Valeurs", "FRA"),
            id="tags-compared-without-case",
        ),
        pytest.param(
            [("eng", "  "), ("fra", "Qualité")],
            [("fra", "Valeurs")],
            ("Qualité", "Valeurs", "fra"),
            id="blank-english-title-ignored",
        ),
        pytest.param(
            [("eng", "Air quality")],
            [("eng", "B text"), ("eng", "A text")],
            ("Air quality", "A text", "eng"),
            id="two-descriptions-in-the-language",
        ),
        pytest.param(
            [("eng", "Air quality")],
            [("fra", "Valeurs"), ("deu", "Werte")],
            ("Air quality", "(language: deu) Werte", "eng"),
            id="several-other-languages-first-tag",
        ),
        pytest.param(
            [("eng", "Air quality")],
            [("eng", " ")],
            ("Air quality", "", "eng"),
            id="blank-description",
        ),
    ],
)
def test_choose_language(titles, descriptions, expected):
    text = choose_language(titles, descriptions)
    assert (text.title, text.description, text.language) == expected


@pytest.mark.parametrize("titles", [[], [("eng", "  ")]], ids=["none", "blank"])
def test_no_title(titles):
    assert choose_language(titles, [("eng", "Values")]) is None


# --- existing themes and validation ------------------------------------------------------------


def test_existing_themes_keep_vocabulary_uris_once(vocabulary):
    assert existing_themes(
        [OUTSIDE, uri("ENVI"), uri("ENVI"), uri("AGRI")], vocabulary
    ) == (
        uri("ENVI"),
        uri("AGRI"),
    )


def test_validate_valid_codes(vocabulary):
    assert validate_codes(["ENVI", "AGRI"], [], 5, vocabulary) == (
        (uri("ENVI"), uri("AGRI")),
        (),
    )


def test_validate_drops_unknown_codes_with_a_warning_each(vocabulary):
    themes, warnings = validate_codes(
        ["ENVI", "Environment", "envi"], [], 5, vocabulary
    )
    assert themes == (uri("ENVI"),)
    assert len(warnings) == 2
    assert "'Environment'" in warnings[0]
    assert "'envi'" in warnings[1]


def test_validate_drops_duplicates_without_a_warning(vocabulary):
    themes, warnings = validate_codes(
        ["ENVI", "AGRI", "AGRI"], [uri("ENVI")], 4, vocabulary
    )
    assert (themes, warnings) == ((uri("AGRI"),), ())


def test_validate_cuts_at_the_remaining_count(vocabulary):
    themes, warnings = validate_codes(["AGRI", "ECON", "EDUC"], [], 1, vocabulary)
    assert themes == (uri("AGRI"),)
    assert len(warnings) == 1
    assert "cut" in warnings[0]


def test_validate_unknown_codes_do_not_use_the_cap(vocabulary):
    themes, warnings = validate_codes(["BOGUS", "AGRI", "ECON"], [], 1, vocabulary)
    assert themes == (uri("AGRI"),)
    assert "'BOGUS'" in warnings[0]
    assert "cut" in warnings[1]
    assert len(warnings) == 2


# --- the proposal ------------------------------------------------------------------------------


def test_valid_answer(vocabulary):
    result, backend = run(resource(), vocabulary, answer("ENVI", "AGRI"))
    assert result.status == "ok"
    assert result.themes == (uri("ENVI"), uri("AGRI"))
    assert (result.calls, result.issues, result.existing_themes) == (1, (), ())
    # The provenance is the active prompt's, whatever its version or model.
    version = active_version("propose_themes")
    loaded = load_prompt(
        "propose_themes",
        version,
        {
            "vocabulary": "v",
            "title": "t",
            "description": "d",
            "existing_themes": "none",
            "remaining": "5",
        },
    )
    assert result.prompt == propose.PromptInfo(
        "propose_themes", version, loaded.sha256, loaded.model
    )
    request = backend.requests[0]
    assert "Air quality" in request.user
    assert "Hourly NO2 values" in request.user
    assert asked_for(backend) == 5
    assert "none" in request.user  # no existing theme
    # The whole vocabulary is in the system part.
    assert all(line in request.system for line in propose.format_entries(vocabulary))


def test_mixed_valid_and_invalid_codes(vocabulary, caplog):
    with caplog.at_level(logging.WARNING, logger="mqa_advisor.propose"):
        result, _ = run(resource(), vocabulary, answer("ENVI", "Environment", "XXXX"))
    assert result.themes == (uri("ENVI"),)
    assert [i.severity for i in result.issues] == ["warning", "warning"]
    assert all(i.resource_id == "d1" for i in result.issues)
    assert sum("resource d1" in r.getMessage() for r in caplog.records) == 2


def test_duplicate_of_an_existing_theme(vocabulary):
    result, _ = run(resource(themes=[uri("ENVI")]), vocabulary, answer("ENVI", "AGRI"))
    assert result.themes == (uri("AGRI"),)
    assert result.existing_themes == (uri("ENVI"),)


def test_over_the_cap(vocabulary):
    four = [uri(c) for c in ("ENVI", "TRAN", "HEAL", "SOCI")]
    result, backend = run(resource(themes=four), vocabulary, answer("AGRI", "ECON"))
    assert result.themes == (uri("AGRI"),)
    assert "cut" in result.issues[0].message
    assert asked_for(backend) == 1


def test_remaining_count_and_existing_themes_in_the_prompt(vocabulary):
    _, backend = run(
        resource(themes=[uri("ENVI"), uri("TRAN"), OUTSIDE]), vocabulary, answer("AGRI")
    )
    user = backend.requests[0].user
    assert asked_for(backend) == 3
    assert "ENVI — Environment" in user
    assert "TRAN — Transport" in user


def test_duplicate_existing_uris_count_once(vocabulary):
    themes = [uri(c) for c in ("ENVI", "ENVI", "TRAN", "HEAL", "SOCI")]
    result, backend = run(resource(themes=themes), vocabulary, answer("AGRI"))
    assert result.status == "ok"
    assert asked_for(backend) == 1


def test_four_themes_and_outside_ones_ask_for_one(vocabulary):
    themes = [uri(c) for c in ("ENVI", "TRAN", "HEAL", "SOCI")] + [
        OUTSIDE,
        OUTSIDE + "2",
    ]
    _, backend = run(resource(themes=themes), vocabulary, answer("AGRI"))
    assert asked_for(backend) == 1


@pytest.mark.parametrize("count", [5, 6], ids=["five", "more-than-five"])
def test_cap_reached_makes_no_call(vocabulary, count, caplog):
    codes = ("ENVI", "TRAN", "HEAL", "SOCI", "AGRI", "ECON")[:count]
    with caplog.at_level(logging.INFO, logger="mqa_advisor.propose"):
        result, backend = run(resource(themes=[uri(c) for c in codes]), vocabulary)
    assert (result.status, result.calls, result.issues, result.prompt) == (
        "skipped",
        0,
        (),
        None,
    )
    assert backend.requests == []
    # Only this module's records: the run log writes its own lines under a child logger.
    records = [r for r in caplog.records if r.name == "mqa_advisor.propose"]
    assert [r.levelname for r in records] == ["INFO"]
    assert "resource d1" in records[0].getMessage()


def test_themes_all_outside_the_vocabulary_still_get_a_call(vocabulary):
    result, backend = run(resource(themes=[OUTSIDE]), vocabulary, answer("ENVI"))
    assert result.status == "ok"
    assert asked_for(backend) == 5


def test_description_in_another_language_is_labelled(vocabulary):
    item = resource(descriptions=[("fra", "Valeurs horaires")])
    _, backend = run(item, vocabulary, answer("ENVI"))
    assert (
        "<description>(language: fra) Valeurs horaires</description>"
        in backend.requests[0].user
    )


def test_no_description_sends_the_title_alone(vocabulary):
    _, backend = run(resource(descriptions=[]), vocabulary, answer("ENVI"))
    assert "<description></description>" in backend.requests[0].user


def test_no_title_is_skipped_with_a_warning(vocabulary, caplog):
    with caplog.at_level(logging.WARNING, logger="mqa_advisor.propose"):
        result, backend = run(resource(titles=[]), vocabulary)
    assert result.status == "skipped"
    assert [(i.severity, i.message) for i in result.issues] == [
        ("warning", "skipped: no title")
    ]
    assert backend.requests == []
    assert [r.levelname for r in caplog.records] == ["WARNING"]
    assert "resource d1" in caplog.records[0].getMessage()


def test_no_own_entity_is_skipped_with_a_warning(vocabulary, caplog):
    with caplog.at_level(logging.WARNING, logger="mqa_advisor.propose"):
        result, backend = run(
            resource(entity_id=None, problem="no dataset entity"), vocabulary
        )
    assert result.status == "skipped"
    assert result.issues[0].severity == "warning"
    assert "no dataset entity" in result.issues[0].message
    assert backend.requests == []
    assert [r.levelname for r in caplog.records] == ["WARNING"]
    assert "resource d1" in caplog.records[0].getMessage()


def test_data_service_end_to_end(conn, folder, vocabulary):
    _write_service(
        folder,
        {
            "id": "svc1",
            "title": {"eng": "Open data platform API"},
            "theme": [{"resource": uri("TECH")}],
        },
    )
    _load(conn, folder)
    result, backend = run(
        read_resource(conn, _pk(conn, "svc1")), vocabulary, answer("GOVE")
    )
    assert (result.status, result.themes) == ("ok", (uri("GOVE"),))
    assert result.existing_themes == (uri("TECH"),)
    assert "TECH — " in backend.requests[0].user
    assert asked_for(backend) == 4


def test_third_retry_succeeds(vocabulary):
    result, backend = run(
        resource(), vocabulary, answer("X"), answer("Y"), answer("Z"), answer("ENVI")
    )
    assert (result.status, result.themes, result.calls) == ("ok", (uri("ENVI"),), 4)
    assert len(backend.requests) == 4
    assert [i.severity for i in result.issues] == ["warning"] * 3


def test_retries_exhausted(vocabulary, caplog):
    # A fifth answer exists but must not be asked for.
    script = [answer("X")] * 4 + [answer("ENVI")]
    with caplog.at_level(logging.ERROR, logger="mqa_advisor.propose"):
        result, backend = run(resource(themes=[uri("TRAN")]), vocabulary, *script)
    assert (result.status, result.themes, result.calls) == ("error", (), 4)
    assert len(backend.requests) == 4
    assert [i.severity for i in result.issues] == ["warning"] * 4 + ["error"]
    error = result.issues[-1]
    assert error.resource_id == "d1"
    assert "4 calls" in error.message
    assert "'X'" in error.message  # the last answer
    assert result.existing_themes == (uri("TRAN"),)
    assert result.prompt is not None
    assert [r.levelname for r in caplog.records] == ["ERROR"]
    assert "resource d1" in caplog.records[0].getMessage()


@pytest.mark.parametrize(
    "script",
    [
        pytest.param([answer("ENVI")] * 4, id="only-existing-themes"),
    ],
)
def test_empty_answers_are_retried_without_a_warning(vocabulary, script):
    # An empty list never gets here: the schema's minItems makes it a schema mismatch (an error, no retry).
    result, backend = run(resource(themes=[uri("ENVI")]), vocabulary, *script)
    assert (result.status, result.calls, len(backend.requests)) == ("error", 4, 4)
    assert [i.severity for i in result.issues] == ["error"]


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(LLMCallError("API down", attempt=4), id="api-failure"),
        pytest.param(LLMResponseError("API down: schema", "{}"), id="schema-mismatch"),
        pytest.param(LLMInputError("API down: prompt too long"), id="call-not-made"),
    ],
)
def test_call_failure_is_an_error_without_retry(vocabulary, error, caplog):
    with caplog.at_level(logging.ERROR, logger="mqa_advisor.propose"):
        result, backend = run(resource(), vocabulary, error, answer("ENVI"))
    assert (result.status, result.calls, result.themes) == ("error", 1, ())
    assert len(backend.requests) == 1
    assert [i.severity for i in result.issues] == ["error"]
    assert "API down" in result.issues[0].message
    assert [r.levelname for r in caplog.records] == ["ERROR"]
    assert "resource d1" in caplog.records[0].getMessage()


def test_call_failure_after_an_empty_answer(vocabulary):
    result, backend = run(
        resource(), vocabulary, answer("X"), LLMCallError("API down"), answer("ENVI")
    )
    assert (result.status, result.calls, len(backend.requests)) == ("error", 2, 2)
    assert [i.severity for i in result.issues] == ["warning", "error"]


def test_prompt_cap_mismatch_raises_before_any_call(vocabulary, monkeypatch):
    monkeypatch.setattr(propose, "THEME_CAP", 4)
    backend = FakeBackend([answer("ENVI")])
    with RunLog("cat", enabled=False) as run_log:
        with pytest.raises(PromptError, match="max_items"):
            propose_themes(resource(), LLMClient(backend, run_log), vocabulary)
    assert backend.requests == []


def test_nothing_is_written_to_the_database(conn, folder, vocabulary):
    _dataset_with_related_dataset(folder)
    _load(conn, folder)
    changes = conn.total_changes
    result, _ = run(read_resource(conn, _pk(conn, "d1")), vocabulary, answer("AGRI"))
    assert result.status == "ok"
    # The related dataset's AGRI is not an existing theme of d1.
    assert result.existing_themes == (uri("ENVI"),)
    assert conn.total_changes == changes
