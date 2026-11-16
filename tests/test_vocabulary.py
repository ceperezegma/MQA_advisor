"""Tests for the data-theme vocabulary reader (vocabulary.py)."""

import logging
import re

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
    message = records[0].getMessage()
    assert "14 data-theme concepts" in message
    assert "0 deprecated skipped" in message
    assert "20241211-0" in message


def test_load_log_counts_skipped_deprecated_concepts(tmp_path, caplog):
    path = _write_rdf(
        tmp_path,
        [
            _concept("AAA"),
            _concept("OLD1", deprecated="true"),
            _concept("OLD2", deprecated="true"),
        ],
    )
    with caplog.at_level(logging.INFO, logger="mqa_advisor.vocabulary"):
        load_vocabulary(path)
    records = [r for r in caplog.records if r.name == "mqa_advisor.vocabulary"]
    assert len(records) == 1
    assert "1 data-theme concepts" in records[0].getMessage()
    assert "2 deprecated skipped" in records[0].getMessage()


# --- review fixes: definitions in other languages, blanks, padding ----------


def _raw_concept(about, inner, *, deprecated=None):
    about_attr = f' rdf:about="{about}"' if about is not None else ""
    dep_attr = f' at:deprecated="{deprecated}"' if deprecated is not None else ""
    return f"<skos:Concept{about_attr}{dep_attr}>{inner}</skos:Concept>"


_EN_LABEL = '<skos:prefLabel xml:lang="en">Label</skos:prefLabel>'


def test_german_only_definition_gives_none(tmp_path):
    inner = _EN_LABEL + '<skos:definition xml:lang="de">Nur deutsch</skos:definition>'
    path = _write_rdf(tmp_path, [_raw_concept(THEME_BASE + "AAA", inner)])
    assert load_vocabulary(path).entries[0].definition is None


def test_german_definition_before_english_returns_the_english_text(tmp_path):
    inner = (
        _EN_LABEL
        + '<skos:definition xml:lang="de">Deutsch</skos:definition>'
        + '<skos:definition xml:lang="en">English</skos:definition>'
    )
    path = _write_rdf(tmp_path, [_raw_concept(THEME_BASE + "AAA", inner)])
    assert load_vocabulary(path).entries[0].definition == "English"


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_blank_definition_gives_none_and_label_only_line(tmp_path, blank):
    path = _write_rdf(tmp_path, [_concept("AAA", label="Alpha", definition=blank)])
    vocabulary = load_vocabulary(path)
    assert vocabulary.entries[0].definition is None
    assert format_entries(vocabulary) == ["AAA — Alpha"]


def test_blank_english_element_is_skipped_for_the_next_english_one(tmp_path):
    inner = (
        '<skos:prefLabel xml:lang="en">  </skos:prefLabel>'
        '<skos:prefLabel xml:lang="en">Real label</skos:prefLabel>'
        '<skos:definition xml:lang="en"> </skos:definition>'
        '<skos:definition xml:lang="en">Real definition</skos:definition>'
    )
    path = _write_rdf(tmp_path, [_raw_concept(THEME_BASE + "AAA", inner)])
    entry = load_vocabulary(path).entries[0]
    assert (entry.label, entry.definition) == ("Real label", "Real definition")


def test_padded_label_and_definition_are_stripped(tmp_path):
    path = _write_rdf(
        tmp_path, [_concept("AAA", label="  Alpha \n", definition="\n  first  ")]
    )
    entry = load_vocabulary(path).entries[0]
    assert (entry.label, entry.definition) == ("Alpha", "first")


def test_format_entries_on_committed_file_gives_14_lines():
    lines = format_entries(load_vocabulary())
    assert len(lines) == 14
    assert "OP_DATPRO — Provisional data" in lines
    assert sum(1 for line in lines if ":" not in line) == 1


# --- review fixes: bad files and concepts -----------------------------------


def test_empty_file_raises(tmp_path):
    path = tmp_path / "empty.rdf"
    path.write_bytes(b"")
    with pytest.raises(VocabularyError, match="empty.rdf"):
        load_vocabulary(path)


def test_non_rdf_xml_raises(tmp_path):
    path = tmp_path / "foo.xml"
    path.write_text("<foo/>", encoding="utf-8")
    with pytest.raises(VocabularyError, match="foo.xml"):
        load_vocabulary(path)


def test_directory_path_raises_vocabulary_error(tmp_path):
    with pytest.raises(VocabularyError, match=tmp_path.name):
        load_vocabulary(tmp_path)


def test_oserror_is_chained(tmp_path):
    with pytest.raises(VocabularyError) as excinfo:
        load_vocabulary(tmp_path / "missing.rdf")
    assert isinstance(excinfo.value.__cause__, OSError)


@pytest.mark.parametrize("about", [None, "", THEME_BASE])
def test_concept_without_usable_uri_raises_naming_its_label(tmp_path, about):
    path = _write_rdf(tmp_path, [_raw_concept(about, _EN_LABEL)])
    with pytest.raises(VocabularyError, match="Label"):
        load_vocabulary(path)


def test_duplicate_code_raises(tmp_path):
    other_uri = "http://example.org/other/AAA"
    path = _write_rdf(
        tmp_path,
        [_concept("AAA"), _raw_concept(other_uri, _EN_LABEL, deprecated="false")],
    )
    with pytest.raises(VocabularyError, match="AAA"):
        load_vocabulary(path)


def test_duplicate_uri_raises(tmp_path):
    path = _write_rdf(tmp_path, [_concept("AAA"), _concept("AAA")])
    with pytest.raises(VocabularyError, match=re.escape(THEME_BASE + "AAA")):
        load_vocabulary(path)


@pytest.mark.parametrize("value", ["True", "1", "yes", ""])
def test_invalid_deprecated_value_raises_naming_the_concept(tmp_path, value):
    path = _write_rdf(tmp_path, [_concept("AAA", deprecated=value)])
    with pytest.raises(VocabularyError, match="AAA"):
        load_vocabulary(path)


def test_deprecated_false_and_absent_are_kept(tmp_path):
    path = _write_rdf(
        tmp_path,
        [
            _concept("AAA", deprecated="false"),
            _raw_concept(THEME_BASE + "BBB", _EN_LABEL),
        ],
    )
    assert [e.code for e in load_vocabulary(path).entries] == ["AAA", "BBB"]


def test_theme_entry_is_frozen():
    entry = ThemeEntry(uri="u", code="C", label="L", definition=None)
    with pytest.raises(AttributeError):
        entry.code = "D"
