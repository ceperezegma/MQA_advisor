## 1. Vocabulary module

- [x] 1.1 Write failing tests in `tests/test_vocabulary.py` for the committed file (14 pinned codes, version `20241211-0`, ECON entry, OP_DATPRO without definition) and verify they fail because the module does not exist yet
- [x] 1.2 Create `src/mqa_advisor/vocabulary.py` with the entry and vocabulary types and `load_vocabulary`, and verify the committed-file tests pass
- [x] 1.3 Add the deprecated-concept and missing-English-definition fixture tests and verify they pass
- [x] 1.4 Add `format_entries` (`CODE — label: definition`, label alone without a definition) with tests for both line forms
- [x] 1.5 Add `code_to_uri` with tests for a known code, an unknown code and a lowercase code (nothing returned)
- [x] 1.6 Add `VocabularyError` for a missing file, malformed XML, a missing version and no concepts, with a test for each (the message names the path)
- [x] 1.7 Add the single INFO log line on load (concept count and version) and verify it with `caplog`

## 2. Verification

- [x] 2.1 Run `uv run ruff check src/ tests/` and `uv run ruff format src/ tests/` and verify they are clean
- [x] 2.2 Run `uv run pytest` and verify the whole unit suite passes

## 3. Review fixes

- [x] 3.1 Write failing tests for: non-English-only and German-before-English definitions, empty/whitespace/padded text, 14 formatted lines of the committed file, an empty file, `<foo/>` and a directory path, a concept without `rdf:about`, duplicate code, duplicate URI, `at:deprecated` `"True"`/`"1"`, and the log message with `14 data-theme concepts` and the skipped count; verify they fail
- [x] 3.2 Skip empty English elements and continue to the next English one
- [x] 3.3 Reject a concept with a missing or empty URI or empty code (naming its label), and duplicate codes or URIs
- [x] 3.4 Build the code-to-URI dict once in `Vocabulary` and use it in `code_to_uri`
- [x] 3.5 Wrap any `OSError` in `VocabularyError` (chained, with the path)
- [x] 3.6 Accept only `"true"`, `"false"` or absent for `at:deprecated`; add the skipped count to the single INFO line
- [x] 3.7 Add docstrings to `ThemeEntry` and `Vocabulary`
- [x] 3.8 Run ruff and the full `uv run pytest`, then verify all pass
