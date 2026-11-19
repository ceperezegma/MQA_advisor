## 1. Fixtures

- [x] 1.1 Generate `tests/fixtures/prompts/regression_examples.json` from the 8 chosen resources (title, tag, description, existing themes/keywords, accepted themes); verify it has 8 entries and the four required cases
- [x] 1.2 Add a loader and the pure checks in `tests/prompt_regression.py`

## 2. Offline tests

- [x] 2.1 Unit tests of the fixtures (shape, accepted codes in the vocabulary, required cases) and of each check with a fake client; verify `uv run pytest` passes

## 3. Integration tests

- [x] 3.1 Add `tests/test_prompt_regression_integration.py` (marked `integration`, skipped without `ANTHROPIC_API_KEY`); verify it is skipped by default and with a reason when the key is missing
- [x] 3.2 Run `uv run pytest -m integration tests/test_prompt_regression_integration.py` with the key; verify all pass and record cost/results for the PR
- [x] 3.3 Document the command in CLAUDE.md/README and verify the text matches the behavior
