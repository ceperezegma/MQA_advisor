## 1. Setup

- [ ] 1.1 `uv add anthropic jsonschema`; add `ANTHROPIC_API_KEY=` with a comment to `.env.example`; add `logs/` to `.gitignore`

## 2. Prompt files: system part and user part

- [ ] 2.1 `prompts.py`: split the rendered body on the `---USER---` line (exactly one, both parts non-empty); `LoadedPrompt.system` and `.user` replace `.text`
- [ ] 2.2 Rewrite `propose_themes/v1.md` and `propose_keywords/v1.md` with the approved wording
- [ ] 2.3 Update `tests/test_prompts.py` (fixtures with the separator; new scenarios: no/duplicate separator, empty part, placeholder in either part, wording split between parts)
- [ ] 2.4 Update ADR-0018 (format line, the one-time in-place edit of v1) and design doc section 6

## 3. Errors, price table and cost

- [ ] 3.1 `llm.py`: `LLMError`, `LLMInputError`, `LLMCallError`, `LLMResponseError(raw_response)`
- [ ] 3.2 `ModelPrice`, `PRICES`, `PRICES_AS_OF` for `claude-haiku-5-5` and `claude-sonnet-5-5` (confirm against published pricing)
- [ ] 3.3 `estimate_cost(model, input, cached, output)`; unknown model raises `LLMInputError`
- [ ] 3.4 Tests: cost arithmetic incl. cached tokens; unknown model raises

## 4. Schema validation

- [ ] 4.1 Validate the answer with `jsonschema` (`Draft202012Validator`); errors become `LLMResponseError` with the raw response
- [ ] 4.2 Tests: valid, empty list, missing key, wrong type, extra key, bad language pattern; both v1 schemas pass `check_schema`

## 5. Backend and client

- [ ] 5.1 `BackendRequest`, `BackendResponse`, `Backend` protocol, `FakeBackend` (scripted responses/errors, records requests)
- [ ] 5.2 `LLMClient.call`: price check, backend call, validation, cost, latency, result, log block
- [ ] 5.3 `load_api_key()` (dotenv, blank = missing) and `AnthropicBackend` (forced tool call, `max_retries=3`, 60 s timeout, httpx hooks for attempt count and WARNING per retry, errors wrapped in `LLMCallError`)
- [ ] 5.4 Tests with the fake: success, system and user sent separately, schema mismatch raises with raw response, unknown model makes no call, missing key raises before any call, SDK client configuration (retries 3, timeout 60), retry WARNING and attempt number (httpx mock transport, no network)

## 6. Run log

- [ ] 6.1 `RunLog`: per-run file `logs/propose/{catalogue}-{UTC timestamp}.log`, unique name, `logging` handler, block format, INFO cost line, run total on close, disabled mode writes nothing
- [ ] 6.2 Tests (use `tmp_path`): block has every field; two runs two files; total equals sum; dry run creates nothing; key never in file or records; schema-mismatch raw response is in the file

## 7. Finish

- [ ] 7.1 Update `CLAUDE.md` architecture notes (`llm.py`) and the design doc status if needed
- [ ] 7.2 `uv run ruff check`, `uv run ruff format`, `uv run pytest`
