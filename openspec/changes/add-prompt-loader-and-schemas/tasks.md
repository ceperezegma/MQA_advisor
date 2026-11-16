## 1. Prompt files

- [ ] 1.1 Create `src/mqa_advisor/prompts/propose_themes/v1.md` with the frontmatter and wording from design.md
- [ ] 1.2 Create `src/mqa_advisor/prompts/propose_keywords/v1.md` with the frontmatter and wording from design.md

## 2. Schemas

- [ ] 2.1 Write tests for `proposal_schemas.output_schema` (limits per call, independent copies, keyword
  `language`, unknown key, `max_items` < 1)
- [ ] 2.2 Implement `src/mqa_advisor/proposal_schemas.py` with the `(name, version)` builders

## 3. Loader and registry

- [ ] 3.1 Write tests with small fixture prompt files (frontmatter errors, version/file-number mismatch, extra
  key, missing placeholder value, hash changes with content, CRLF equals LF, unknown name/version, DEBUG
  record)
- [ ] 3.2 Implement `src/mqa_advisor/prompts.py`: `PromptError`, `LoadedPrompt`, `load_prompt`, `ACTIVE`,
  `active_version`, DEBUG log line

## 4. Pairing and packaging

- [ ] 4.1 Test: committed `(name, version)` files equal the schema keys; `ACTIVE` versions exist; a prompt of
  version N loaded while `ACTIVE` differs gets schema N (monkeypatch `ACTIVE`)
- [ ] 4.2 Test: both committed v1 files load with a value for every placeholder (find placeholders with
  `string.Template.get_identifiers` or a regex)
- [ ] 4.3 Build the wheel (`uv build --wheel`) and confirm both `v1.md` files are inside; change
  `pyproject.toml` only if they are not

## 5. Verification

- [ ] 5.1 `uv run ruff check src/ tests/` and `uv run ruff format src/ tests/`
- [ ] 5.2 `uv run pytest` (full unit suite)
- [ ] 5.3 Walk the acceptance criteria of issue #19 and confirm each one
