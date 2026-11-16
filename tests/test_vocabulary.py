"""Tests for the data-theme vocabulary reader (vocabulary.py)."""

import logging

import pytest

from mqa_advisor.vocabulary import (
    DEFAULT_VOCABULARY_PATH,
    ThemeEntry,
    VocabularyError,
    code_to_uri,
    format_entries,
    load_vocabulary,
)

THEME_BASE = "http://publications.europa.eu/resource/authority/data-theme/"

# Pinned so that a refresh of the vocabulary file that changes the list is noticed.
EXPECTED_CODES = [
    "AGRI",
    "ECON",
    "EDUC",
    "ENER",
    "ENVI",
    "GOVE",
    "HEAL",
    "INTR",
    "JUST",
    "OP_DATPRO",
    "REGI",
    "SOCI",
    "TECH",
    "TRAN",
]

RDF_HEADER = """<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:skos="http://www.w3.org/2004/02/skos/core#"
         xmlns:at="http://publications.europa.eu/ontology/authority/">
"""


def _concept(code, *, label="Label", definition=None, deprecated="false"):
    parts = [
        f'<skos:Concept rdf:about="{THEME_BASE}{code}" at:deprecated="{deprecated}">',
        '<skos:prefLabel xml:lang="de">Etikett</skos:prefLabel>',
    ]
    if label is not None:
        parts.append(f'<skos:prefLabel xml:lang="en">{label}</skos:prefLabel>')
    if definition is not None:
        parts.append(f'<skos:definition xml:lang="en">{definition}</skos:definition>')
    parts.append("</skos:Concept>")
    return "\n".join(parts)


def _write_rdf(tmp_path, concepts, *, version="1-test"):
    version_xml = f"<owl:versionInfo>{version}</owl:versionInfo>" if version else ""
    scheme = f"<skos:ConceptScheme>{version_xml}</skos:ConceptScheme>"
    path = tmp_path / "vocab.rdf"
    path.write_text(
        RDF_HEADER + scheme + "\n".join(concepts) + "</rdf:RDF>", encoding="utf-8"
    )
    return path


# --- committed file ---------------------------------------------------------


def test_committed_file_yields_the_14_pinned_codes():
    vocabulary = load_vocabulary()
    assert sorted(e.code for e in vocabulary.entries) == EXPECTED_CODES


def test_committed_file_version():
    assert load_vocabulary().version == "20241211-0"


def test_default_path_is_independent_of_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert DEFAULT_VOCABULARY_PATH.is_file()
    assert len(load_vocabulary().entries) == 14


def test_econ_entry_has_uri_label_and_definition():
    entry = {e.code: e for e in load_vocabulary().entries}["ECON"]
    assert entry.uri == THEME_BASE + "ECON"
    assert entry.label == "Economy and finance"
    assert entry.definition.startswith("dataset theme covering the domains of economic")


def test_op_datpro_has_label_but_no_definition():
    entry = {e.code: e for e in load_vocabulary().entries}["OP_DATPRO"]
    assert entry.label == "Provisional data"
    assert entry.definition is None


# --- fixtures: deprecated, missing definition -------------------------------


def test_deprecated_concept_is_skipped(tmp_path):
    path = _write_rdf(tmp_path, [_concept("AAA"), _concept("OLD", deprecated="true")])
    assert [e.code for e in load_vocabulary(path).entries] == ["AAA"]


def test_concept_without_deprecated_attribute_is_kept(tmp_path):
    concept = (
        f'<skos:Concept rdf:about="{THEME_BASE}AAA">'
        '<skos:prefLabel xml:lang="en">L</skos:prefLabel></skos:Concept>'
    )
    path = _write_rdf(tmp_path, [concept])
    assert [e.code for e in load_vocabulary(path).entries] == ["AAA"]


def test_concept_without_english_definition_has_none(tmp_path):
    path = _write_rdf(tmp_path, [_concept("AAA", label="Only label")])
    assert load_vocabulary(path).entries[0].definition is None


# --- format_entries ---------------------------------------------------------


def test_format_entries_both_line_forms(tmp_path):
    path = _write_rdf(
        tmp_path,
        [
            _concept("AAA", label="Alpha", definition="the first"),
            _concept("BBB", label="Beta"),
        ],
    )
    assert format_entries(load_vocabulary(path)) == [
        "AAA — Alpha: the first",
        "BBB — Beta",
    ]


# --- code_to_uri ------------------------------------------------------------


def test_code_to_uri_known_code():
    assert code_to_uri(load_vocabulary(), "TECH") == THEME_BASE + "TECH"


@pytest.mark.parametrize("code", ["XXXX", "tech", "", " TECH"])
def test_code_to_uri_rejects_unknown_and_non_exact_codes(code):
    assert code_to_uri(load_vocabulary(), code) is None


# --- errors -----------------------------------------------------------------


def test_missing_file_raises_naming_the_path(tmp_path):
    path = tmp_path / "nope.rdf"
    with pytest.raises(VocabularyError, match="nope.rdf"):
        load_vocabulary(path)


def test_malformed_xml_raises_naming_the_path(tmp_path):
    path = tmp_path / "bad.rdf"
    path.write_text("<rdf:RDF><unclosed>", encoding="utf-8")
    with pytest.raises(VocabularyError, match="bad.rdf"):
        load_vocabulary(path)


def test_missing_version_raises(tmp_path):
    path = _write_rdf(tmp_path, [_concept("AAA")], version=None)
    with pytest.raises(VocabularyError, match="owl:versionInfo"):
        load_vocabulary(path)


def test_no_concepts_raises(tmp_path):
    path = _write_rdf(tmp_path, [])
    with pytest.raises(VocabularyError, match="vocab.rdf"):
        load_vocabulary(path)


def test_only_deprecated_concepts_raises(tmp_path):
    path = _write_rdf(tmp_path, [_concept("OLD", deprecated="true")])
    with pytest.raises(VocabularyError):
        load_vocabulary(path)


def test_concept_without_english_label_raises(tmp_path):
    path = _write_rdf(tmp_path, [_concept("AAA", label=None)])
    with pytest.raises(VocabularyError, match="AAA"):
        load_vocabulary(path)


# --- logging ----------------------------------------------------------------


def test_load_logs_one_info_line_with_count_and_version(caplog):
    with caplog.at_level(logging.INFO, logger="mqa_advisor.vocabulary"):
        load_vocabulary()
    records = [r for r in caplog.records if r.name == "mqa_advisor.vocabulary"]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert "14" in records[0].getMessage()
    assert "20241211-0" in records[0].getMessage()


def test_theme_entry_is_frozen():
    entry = ThemeEntry(uri="u", code="C", label="L", definition=None)
    with pytest.raises(AttributeError):
        entry.code = "D"
