## 1. Prompt files

- [x] 1.1 Edit `src/mqa_advisor/prompts/propose_themes/v1.md` to the wording in design.md (data tags, "up to
  $remaining additional (at least 1)")
- [x] 1.2 Edit `src/mqa_advisor/prompts/propose_keywords/v1.md` to the wording in design.md (data tags, language
  label and 2-letter code, count wording)

## 2. Schemas

- [x] 2.1 Write tests for `proposal_schemas.output_schema` (exact v1 dicts with no `maxItems`, language pattern
  with 2 or 3 letters, independent copies, unknown key, version N uses schema N with a throwaway v2 builder)
- [x] 2.2 Implement `output_schema(name, version)` and `schema_keys()` without any item limit; comment why
  `language` is required and why there is no `maxItems`

## 3. Loader and registry

- [x] 3.1 Write tests with small fixture prompt files (frontmatter errors, ranges and boundaries, NaN, float
  temperature, version/file-number mismatch, extra key, invalid and `$$` placeholders, missing, unused and
  non-string values, invalid UTF-8, BOM, bad name, hash changes with content, CRLF equals LF, unknown
  name/version, DEBUG record)
- [x] 3.2 Implement `src/mqa_advisor/prompts.py`: `PromptError`, `Frontmatter`, `LoadedPrompt` (with docstring),
  `UNKNOWN_LANGUAGE`, `load_prompt`, `ACTIVE`, `active_version`, DEBUG log line

## 4. Pairing, wording and packaging

- [x] 4.1 Tests: committed `(name, version)` files equal the schema keys; `set(ACTIVE.items())` is a subset of
  the schema keys; `set(ACTIVE)` equals the committed prompt names
- [x] 4.2 Tests: both committed v1 files load with a value for every placeholder; tags, data line, count
  wording and the `unknown` sentinel are pinned
- [x] 4.3 Integration-marked test: build the wheel into `tmp_path` and check both `v1.md` files are inside
- [x] 4.4 Run that test and rebuild the wheel; change `pyproject.toml` only if the files are not inside

## 5. Documents

- [x] 5.1 ADR-0018 and design doc section 6: no `maxItems` in the schema, the cap is enforced in code
- [x] 5.2 Issue #19: acceptance criterion about `maxItems` updated

## 6. Verification

- [x] 6.1 `uv run ruff check src/ tests/` and `uv run ruff format src/ tests/`
- [x] 6.2 `uv run pytest` (full unit suite)
- [x] 6.3 Walk the acceptance criteria of issue #19 and confirm each one
