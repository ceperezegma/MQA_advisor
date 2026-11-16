"""Read the EU data-theme vocabulary from vocabularies/data-theme-skos.rdf.

Gives the rest of the code the allowed data themes (code, authority URI, English label and
definition) and the file's version, using only the standard library. Deprecated concepts are
skipped. A missing or malformed file raises `VocabularyError`; an empty vocabulary is never
returned.

Usage:

    vocabulary = load_vocabulary()
    lines = format_entries(vocabulary)  # "CODE — label: definition"
    uri = code_to_uri(vocabulary, "TECH")  # None for an unknown code
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_VOCABULARY_PATH = (
    Path(__file__).resolve().parents[2] / "vocabularies" / "data-theme-skos.rdf"
)

_RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
_SKOS = "{http://www.w3.org/2004/02/skos/core#}"
_OWL = "{http://www.w3.org/2002/07/owl#}"
_AT = "{http://publications.europa.eu/ontology/authority/}"
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


class VocabularyError(Exception):
    """The vocabulary file is missing, malformed or unusable."""


@dataclass(frozen=True)
class ThemeEntry:
    uri: str
    code: str
    label: str
    definition: str | None


@dataclass(frozen=True)
class Vocabulary:
    version: str
    entries: tuple[ThemeEntry, ...]


def _english_text(concept: ET.Element, tag: str) -> str | None:
    """Return the stripped English text of the first `tag` child, or None if absent/empty."""
    for child in concept.findall(tag):
        if child.get(_XML_LANG) == "en":
            return (child.text or "").strip() or None
    return None


def _parse_concept(concept: ET.Element, path: Path) -> ThemeEntry:
    uri = concept.get(f"{_RDF}about", "")
    code = uri.rsplit("/", 1)[-1]
    label = _english_text(concept, f"{_SKOS}prefLabel")
    if label is None:
        raise VocabularyError(f"Concept {code!r} has no English label in {path}")
    return ThemeEntry(
        uri=uri,
        code=code,
        label=label,
        definition=_english_text(concept, f"{_SKOS}definition"),
    )


def load_vocabulary(path: Path = DEFAULT_VOCABULARY_PATH) -> Vocabulary:
    """Read the non-deprecated data themes and the file version from `path`."""
    try:
        root = ET.parse(path).getroot()
    except FileNotFoundError as exc:
        raise VocabularyError(f"Vocabulary file not found: {path}") from exc
    except ET.ParseError as exc:
        raise VocabularyError(
            f"Vocabulary file is not well-formed XML: {path}"
        ) from exc

    version = (root.findtext(f".//{_OWL}versionInfo") or "").strip()
    if not version:
        raise VocabularyError(f"No owl:versionInfo in {path}")

    # A concept is skipped only when explicitly deprecated; a missing attribute keeps it.
    entries = tuple(
        _parse_concept(concept, path)
        for concept in root.iter(f"{_SKOS}Concept")
        if concept.get(f"{_AT}deprecated") != "true"
    )
    if not entries:
        raise VocabularyError(f"No data-theme concepts found in {path}")

    logger.info("Loaded %d data-theme concepts, version %s", len(entries), version)
    return Vocabulary(version=version, entries=entries)


def format_entries(vocabulary: Vocabulary) -> list[str]:
    """One prompt line per entry: `CODE — label: definition`, or `CODE — label`."""
    return [
        f"{e.code} — {e.label}: {e.definition}"
        if e.definition
        else f"{e.code} — {e.label}"
        for e in vocabulary.entries
    ]


def code_to_uri(vocabulary: Vocabulary, code: str) -> str | None:
    """Return the authority URI for an exact code, or None. Never guesses or normalizes."""
    for entry in vocabulary.entries:
        if entry.code == code:
            return entry.uri
    return None
