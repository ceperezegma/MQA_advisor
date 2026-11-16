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
