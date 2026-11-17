## 1. Reading

- [x] 1.1 `ResourceText.keywords` and `read_resource` reading own-entity `dcat:keyword` literals — verified by a test on a loaded fixture (keywords in two languages, a related dataset's keywords ignored)

## 2. Pure helpers

- [x] 2.1 `validate_keywords(keywords, existing, remaining)`: trim, drop empty, case-insensitive duplicates, keep case, cut with a warning — verified by tests: valid, whitespace/case duplicates, empty strings, over-cap

## 3. The keyword proposal

- [x] 3.1 `KeywordResult` and `propose_keywords(resource, client, theme_result, vocabulary)`: skip rules, theme context labels, language (title tag / inferred), call, validation, retries, `LLMError` -> `error`, logs — verified by `FakeBackend` tests: valid, duplicates, empty strings, over-cap, all-duplicate retry then success, retries exhausted, untagged title with inferred language (INFO), 10 keywords no call, other-language keywords count, theme context present and absent in the rendered prompt, theme result `error`
- [x] 3.2 Nothing written: row counts unchanged after the call

## 4. Docs and checks

- [x] 4.1 `CLAUDE.md` `propose.py` line mentions keywords
- [x] 4.2 `uv run ruff check src/ tests/`, `uv run ruff format src/ tests/`, `uv run pytest` pass
