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
from dataclasses import dataclass, field
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
    """One data theme. `uri` is the authority URI; `code` is the last segment of the URI."""

    uri: str
    code: str
    label: str
    definition: str | None


@dataclass(frozen=True)
class Vocabulary:
    """The non-deprecated data themes of one vocabulary file and the file's version.

    `uris_by_code` maps each code to its authority URI (built once at load, used by
    `code_to_uri`).
    """

    version: str
    entries: tuple[ThemeEntry, ...]
    uris_by_code: dict[str, str] = field(repr=False, compare=False)


def _english_text(concept: ET.Element, tag: str) -> str | None:
    """Return the stripped text of the first English `tag` child that has text, else None."""
    for child in concept.findall(tag):
        if child.get(_XML_LANG) == "en":
            text = (child.text or "").strip()
            if text:
                return text
    return None


def _parse_concept(concept: ET.Element, path: Path) -> ThemeEntry:
    uri = concept.get(f"{_RDF}about", "").strip()
    code = uri.rsplit("/", 1)[-1]
    label = _english_text(concept, f"{_SKOS}prefLabel")
    if not code:
        raise VocabularyError(
            f"Concept {label or '(no English label)'!r} has no usable rdf:about URI in {path}"
        )
    if label is None:
        raise VocabularyError(f"Concept {code!r} has no English label in {path}")
    return ThemeEntry(
        uri=uri,
        code=code,
        label=label,
        definition=_english_text(concept, f"{_SKOS}definition"),
    )


def _is_deprecated(concept: ET.Element, path: Path) -> bool:
    """`at:deprecated` must be "true", "false" or absent; anything else is an error."""
    value = concept.get(f"{_AT}deprecated")
    if value is None or value == "false":
        return False
    if value == "true":
        return True
    uri = concept.get(f"{_RDF}about", "")
    raise VocabularyError(
        f"Concept {uri!r} has at:deprecated={value!r} (expected true or false) in {path}"
    )


def load_vocabulary(path: Path = DEFAULT_VOCABULARY_PATH) -> Vocabulary:
    """Read the non-deprecated data themes and the file version from `path`."""
    try:
        root = ET.parse(path).getroot()
    except OSError as exc:  # missing file, a directory, permissions, ...
        raise VocabularyError(f"Cannot read vocabulary file {path}: {exc}") from exc
    except ET.ParseError as exc:  # includes an empty file
        raise VocabularyError(
            f"Vocabulary file is not well-formed XML: {path}"
        ) from exc

    version = (root.findtext(f".//{_OWL}versionInfo") or "").strip()
    if not version:
        raise VocabularyError(f"No owl:versionInfo in {path}")

    entries = []
    uris_by_code: dict[str, str] = {}
    deprecated_skipped = 0
    for concept in root.iter(f"{_SKOS}Concept"):
        if _is_deprecated(concept, path):
            deprecated_skipped += 1
            continue
        entry = _parse_concept(concept, path)
        # The code is the last segment of the URI, so a duplicate URI is also a duplicate code.
        if entry.code in uris_by_code:
            raise VocabularyError(
                f"Duplicate data-theme code {entry.code!r} (URI {entry.uri}) in {path}"
            )
        uris_by_code[entry.code] = entry.uri
        entries.append(entry)
    if not entries:
        raise VocabularyError(f"No data-theme concepts found in {path}")

    logger.info(
        "Loaded %d data-theme concepts (%d deprecated skipped), version %s",
        len(entries),
        deprecated_skipped,
        version,
    )
    return Vocabulary(
        version=version, entries=tuple(entries), uris_by_code=uris_by_code
    )


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
    return vocabulary.uris_by_code.get(code)
