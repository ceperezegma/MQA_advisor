## Context

See proposal.md for motivation. The design doc (`docs/design/theme-keyword-proposals.md`, section 4) and ADR-0017 fix the source, the parser and the line format; this change implements them. `vocabularies/data-theme-skos.rdf` is already committed (version `20241211-0`, 14 concepts). Each `skos:Concept` has `at:deprecated="true|false"`, many `skos:prefLabel`/`skos:definition` elements distinguished by `xml:lang`, and the code is the last path segment of `rdf:about`. The version is in `owl:versionInfo` of the `skos:ConceptScheme`. `OP_DATPRO` has no English definition.

## Goals / Non-Goals

**Goals:**
- A small, dependency-free reader the later tickets (#21 themes, #22 keywords) import.
- Fail loudly on a bad file, never return a silently empty vocabulary.

**Non-Goals:**
- Non-English labels or definitions, or any language option.
- Caching, refresh tooling or downloading the vocabulary (to refresh, replace the file; the pinned-codes test notices the change).
- Using the entries (prompt rendering, validating model output): later tickets.

## Decisions

- **Module `src/mqa_advisor/vocabulary.py`, stdlib `xml.etree.ElementTree`.** Matches the design doc; `rdflib` is not needed for 14 concepts. Alternative `rdflib`: heavy dependency for no gain.
- **Entry type: a frozen dataclass `ThemeEntry(uri, code, label, definition: str | None)`**, and a frozen `Vocabulary` holding `version` and a tuple of entries in file order. Frozen values are easy to test and cannot be changed by callers (same style as `properties.py`).
- **Functions, not a class with state:** `load_vocabulary(path=DEFAULT_PATH) -> Vocabulary`; `format_entries(vocabulary) -> list[str]` (one line per entry, which is the ticket's contract; #21 joins them); `code_to_uri(vocabulary, code) -> str | None`. The default path is resolved from the module file like `DEFAULT_PROPERTY_CATALOG_PATH`, so it works from any working directory.
- **Code and English text.** Code = last segment of `rdf:about` after the final `/`. English = `xml:lang` equal to `en` (the vocabulary uses `en`, not `eng`). Label and definition are whitespace-stripped; an empty definition counts as absent.
- **Deprecated.** A concept is skipped only when `at:deprecated` equals `"true"`; a missing attribute means not deprecated.
- **Errors.** One `VocabularyError(Exception)` for: missing file, `ParseError` (chained with `from`), missing `owl:versionInfo`, a concept without an English label, and zero concepts. The message names the path. Alternative: let `FileNotFoundError`/`ParseError` escape: less clear, and no single type to catch.
- **Lookup** uses a dict built from the entries; exact and case-sensitive; None when unknown (the caller decides to drop with a warning, per the design doc).
- **Logging.** `logging.getLogger(__name__)`; one INFO line per `load_vocabulary` call: `Loaded %d data-theme concepts, version %s`.
- **Tests.** The committed-file test pins the 14 codes, the version and the ECON/OP_DATPRO entries. Small inline fixtures written to `tmp_path` cover a deprecated concept, a concept without an English definition, malformed XML, a file with no concepts and a missing file. No test reads or writes real data folders.

## Risks / Trade-offs

- [A refreshed vocabulary file changes the code list] → the pinned-codes test fails and forces a conscious update.
- [A concept without an English label] → raises `VocabularyError` rather than producing a nameless entry the model could not choose from.
