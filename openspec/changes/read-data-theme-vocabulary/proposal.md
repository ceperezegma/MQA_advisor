## Why

The theme/keyword proposal feature (ADR-0017) asks an LLM to answer with codes from the EU `data-theme` vocabulary, and code must validate those codes. Nothing in the repo reads the committed `vocabularies/data-theme-skos.rdf` yet. Ticket #17 is the first of eight: the vocabulary reader the later tickets (prompt rendering, theme validation) build on.

## What Changes

- Add a `vocabulary` module that parses `vocabularies/data-theme-skos.rdf` with the stdlib `xml.etree` (no `rdflib`) and returns the data-theme entries: authority URI, code, English `skos:prefLabel`, English `skos:definition` (None when absent) and the file's `owl:versionInfo`.
- Skip concepts with `at:deprecated="true"`.
- Add a function that formats the entries as prompt lines `CODE — English label: English definition` (label alone when there is no definition).
- Add a function that maps a code to its authority URI and returns nothing for an unknown code (case-sensitive, never guesses).
- A missing or malformed file raises a clear error instead of returning an empty vocabulary.
- Log at INFO, once per load, the number of concepts and the version.
- No change to the pipeline, the database or existing modules.

## Capabilities

### New Capabilities
- `data-theme-vocabulary`: reading the committed EU data-theme SKOS file into entries, prompt lines and code-to-URI lookup, with load errors and logging.

### Modified Capabilities

## Impact

- New file `src/mqa_advisor/vocabulary.py` and its tests under `tests/` (plus a small fixture RDF with a deprecated concept and one without an English definition).
- No new dependency. The committed `vocabularies/data-theme-skos.rdf` is read, not changed.
- Consumed later by tickets #21 (themes) and #22 (keywords).
