## 1. Setup

- [x] 1.1 `uv add anthropic jsonschema`; add `ANTHROPIC_API_KEY=` with a comment to `.env.example`; add `logs/` to `.gitignore`

## 2. Prompt files: system part and user part

- [x] 2.1 `prompts.py`: split the rendered body on the `---USER---` line (exactly one, both parts non-empty); `LoadedPrompt.system` and `.user` replace `.text`
- [x] 2.2 Rewrite `propose_themes/v1.md` and `propose_keywords/v1.md` with the approved wording
- [x] 2.3 Update `tests/test_prompts.py` (fixtures with the separator; new scenarios: no/duplicate separator, empty part, placeholder in either part, wording split between parts)
- [x] 2.4 Update ADR-0018 (format line, the one-time in-place edit of v1) and design doc section 6
- [x] 2.5 Remove `temperature` from the frontmatter (`Frontmatter`, `LoadedPrompt`, loader validation), both v1 files, the tests, ADR-0018 and design section 6; add the "temperature is not allowed" test

## 3. Errors, price table and cost

- [x] 3.1 `llm.py`: `LLMError`, `LLMInputError`, `LLMCallError`, `LLMResponseError(raw_response)`
- [x] 3.2 `ModelPrice`, `PRICES`, `PRICES_AS_OF` for `claude-haiku-5-5` and `claude-sonnet-5-5` (confirm against published pricing)
- [x] 3.3 `estimate_cost(model, input, cached, output)`; unknown model raises `LLMInputError`; more than 100,000 input tokens raises
- [x] 3.4 Tests: cost arithmetic incl. cached tokens; unknown model raises

## 4. Schema validation

- [x] 4.1 Validate the answer with `jsonschema` (`Draft202012Validator`); errors become `LLMResponseError` with the raw response
- [x] 4.2 Tests: valid, empty list, missing key, wrong type, extra key, bad language pattern; both v1 schemas pass `check_schema`

## 5. Backend and client

- [x] 5.1 `BackendRequest`, `BackendResponse`, `Backend` protocol, `FakeBackend` (scripted responses/errors, records requests)
- [x] 5.2 `LLMClient.call`: price check, backend call, validation, cost, latency, result, log block
- [x] 5.3 `load_api_key()` (dotenv, blank = missing) and `AnthropicBackend` (forced tool call, `max_retries=3`, 60 s timeout, httpx2 hooks for attempt count and WARNING per retry, errors wrapped in `LLMCallError`)
- [x] 5.4 Tests with the fake: success, system and user sent separately, schema mismatch raises with raw response, unknown model makes no call, missing key raises before any call, SDK client configuration (retries 3, timeout 60), retry WARNING and attempt number (httpx2 mock transport, no network), no `temperature`/`top_p`/`top_k` sent

## 6. Run log

- [x] 6.1 `RunLog`: per-run file `logs/propose/{catalogue}-{UTC timestamp}.log`, unique name, `logging` handler, block format, INFO cost line, run total on close, disabled mode writes nothing
- [x] 6.2 Tests (use `tmp_path`): block has every field; two runs two files; total equals sum; dry run creates nothing; key never in file or records; schema-mismatch raw response is in the file

## 7. Review fixes (code review, python-reviewer, silent-failure-hunter, pr-test-analyzer)

- [x] 7.1 `LLMError` carries `attempt`, `raw_response`, `usage`; `LLMClient.call` has one failure handler (also for unexpected exceptions); `stop_reason` in a schema-mismatch message; validator built once per schema with `check_schema` before the request
- [x] 7.2 Size guard before the call (100,000 bytes) and billed-but-unpriced logging (`unpriced_calls`); `PRICES` read-only with Haiku only
- [x] 7.3 `RunLog`: lock, idempotent `close`, error on `record` after close, mode `x` file creation, UTC timestamps, unregistered loggers, write errors logged and raised at close
- [x] 7.4 `AnthropicBackend.close()` and context manager; retry warning reworded; `httpx2` types; comments (cache-creation cost is a lower bound, 60 s is per phase)
- [x] 7.5 Loader: empty rendered system or user part raises
- [x] 7.6 Tests for every point above and the test gaps of the review (literal costs, real-backend failure paths, 401 body with the key, truncated and mixed responses, failed-call blocks, keywords schema, RunLog lifecycle and threads, unicode, INFO level, prompt placement)

## 8. Finish

- [x] 8.1 Update `CLAUDE.md` architecture notes (`llm.py`) and the design doc status if needed
- [x] 8.2 `uv run ruff check`, `uv run ruff format`, `uv run pytest`
