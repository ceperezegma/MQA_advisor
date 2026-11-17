"""Tests for the theme proposal of one resource (``propose``): reading, language, validation, retries.

No network: the model is a ``FakeBackend`` and the run log is disabled. Databases are temporary, loaded from
small records written by the tests with the real loader, so the stored shapes are the real ones.
"""

import json
import logging

import pytest

from mqa_advisor.catalogue_db import connect, load_catalogue
from mqa_advisor.llm import (
    BackendResponse,
    FakeBackend,
    LLMCallError,
    LLMClient,
    RunLog,
)
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
    """A record holding its own Dataset and a related one, each with a title and themes."""
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
            },
            {"@id": uri("ENVI"), "@type": "skos:Concept"},
            {
                "@id": RELATED,
                "@type": "dcat:Dataset",
                "dct:title": {"@value": "Other dataset", "@language": "eng"},
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
    assert problem.startswith("2 ")


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


# --- the proposal ------------------------------------------------------------------------------


def test_valid_answer(vocabulary):
    result, backend = run(resource(), vocabulary, answer("ENVI", "AGRI"))
    assert result.status == "ok"
    assert result.themes == (uri("ENVI"), uri("AGRI"))
    assert (result.calls, result.issues, result.existing_themes) == (1, (), ())
    assert (result.prompt.name, result.prompt.version) == ("propose_themes", 1)
    assert result.prompt.model == "claude-haiku-5-5"
    user = backend.requests[0].user
    assert "<title>Air quality</title>" in user
    assert "<description>Hourly NO2 values</description>" in user
    assert "up to 5 additional themes" in user
    assert (
        "ENVI — " in backend.requests[0].system
    )  # the vocabulary is in the system part


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
    assert "up to 1 additional themes" in backend.requests[0].user


def test_remaining_count_and_existing_themes_in_the_prompt(vocabulary):
    _, backend = run(
        resource(themes=[uri("ENVI"), uri("TRAN"), OUTSIDE]), vocabulary, answer("AGRI")
    )
    user = backend.requests[0].user
    assert "up to 3 additional themes" in user
    assert "ENVI — Environment" in user
    assert "TRAN — Transport" in user


def test_cap_reached_makes_no_call(vocabulary):
    five = [uri(c) for c in ("ENVI", "TRAN", "HEAL", "SOCI", "AGRI")]
    result, backend = run(resource(themes=five), vocabulary)
    assert (result.status, result.calls, result.issues, result.prompt) == (
        "skipped",
        0,
        (),
        None,
    )
    assert backend.requests == []


def test_themes_all_outside_the_vocabulary_still_get_a_call(vocabulary):
    result, backend = run(resource(themes=[OUTSIDE]), vocabulary, answer("ENVI"))
    assert result.status == "ok"
    assert "up to 5 additional themes" in backend.requests[0].user


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


def test_no_title_is_skipped_with_a_warning(vocabulary):
    result, backend = run(resource(titles=[]), vocabulary)
    assert result.status == "skipped"
    assert [(i.severity, i.message) for i in result.issues] == [
        ("warning", "skipped: no title")
    ]
    assert backend.requests == []


def test_no_own_entity_is_skipped_with_a_warning(vocabulary):
    result, backend = run(
        resource(entity_id=None, problem="no dataset entity"), vocabulary
    )
    assert result.status == "skipped"
    assert result.issues[0].severity == "warning"
    assert "no dataset entity" in result.issues[0].message
    assert backend.requests == []


def test_third_retry_succeeds(vocabulary):
    result, backend = run(
        resource(), vocabulary, answer("X"), answer("Y"), answer("Z"), answer("ENVI")
    )
    assert (result.status, result.themes, result.calls) == ("ok", (uri("ENVI"),), 4)
    assert len(backend.requests) == 4
    assert [i.severity for i in result.issues] == ["warning"] * 3


def test_retries_exhausted(vocabulary, caplog):
    script = [answer("X")] * 4 + [
        answer("ENVI")
    ]  # a fifth answer exists but must not be asked for
    with caplog.at_level(logging.ERROR, logger="mqa_advisor.propose"):
        result, backend = run(resource(), vocabulary, *script)
    assert (result.status, result.themes, result.calls) == ("error", (), 4)
    assert len(backend.requests) == 4
    assert result.issues[-1].severity == "error"
    assert [r.levelname for r in caplog.records] == ["ERROR"]
    assert "resource d1" in caplog.records[0].getMessage()


def test_call_failure_is_an_error_without_retry(vocabulary):
    result, backend = run(
        resource(), vocabulary, LLMCallError("API down", attempt=4), answer("ENVI")
    )
    assert (result.status, result.calls) == ("error", 1)
    assert len(backend.requests) == 1
    assert "API down" in result.issues[0].message


def test_nothing_is_written_to_the_database(conn, folder, vocabulary):
    _dataset_with_related_dataset(folder)
    _load(conn, folder)

    def counts():
        return [
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 - fixed table names
            for table in ("resource", "property_value", "proposed_property_value")
        ]

    before = counts()
    result, _ = run(read_resource(conn, _pk(conn, "d1")), vocabulary, answer("AGRI"))
    assert result.status == "ok"
    assert result.existing_themes == (
        uri("ENVI"),
    )  # the related dataset's AGRI is not existing
    assert counts() == before
