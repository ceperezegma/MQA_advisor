## Why

The theme/keyword proposal feature (ADR-0017) needs to call an LLM and get back a validated, structured answer
for each resource. ADR-0019 requires every call to be reconstructible and costed. Tickets #21 (themes), #22
(keywords) and #23 (the `propose` step) all make model calls, so they need one shared, testable wrapper first.
This is ticket #20 (Proposals 4/8); it builds on the prompt loader and schemas from #19.

## What Changes

- Prompt files gain a **system part** (role, rules, fixed lists) before a user part (the resource data and the
  counts), so the model is told properly what to do. `LoadedPrompt` exposes `system` and `user` instead of one
  `text`; the two v1 prompt files are rewritten accordingly (a modification of `prompt-versioning`).
- Add `llm.py`: an `LLMClient` that takes a loaded prompt (#19), makes the call as a forced tool call using the
  prompt version's output schema, validates the answer against that schema (with `jsonschema`), and returns the parsed output with
  input/output/cached token counts, estimated cost in USD, latency and attempt number.
- Add a committed price table (USD per million tokens per model, with an `as_of` date); an unknown model raises.
- Add a backend interface with two implementations: the real one on the `anthropic` SDK (`max_retries=3`, 60 s
  timeout) and a fake for tests, so no unit test touches the network.
- Add the per-run interaction log `logs/propose/{catalogue}-{UTC timestamp}.log` through the standard `logging`
  module: one delimited block per call, an INFO line per call with the cost, WARNING per retried transient error,
  and a run total at the end. No file for a dry run. `logs/` goes into `.gitignore`.
- Read `ANTHROPIC_API_KEY` from `.env`; a missing key raises a clear input error before any call. The key is
  never logged. Placeholder added to `.env.example`.
- A response that does not match the schema raises an error carrying the raw response, which is also logged.
- New runtime dependencies: `anthropic` and `jsonschema`.

## Capabilities

### New Capabilities
- `llm-client`: the model-call wrapper: structured output, retries, usage and cost, price table, fake client,
  and the per-run interaction log.

### Modified Capabilities
- `prompt-versioning`: a prompt file has a system part and a user part, the loader returns both, and the v1
  wording is split between them.

## Impact

- New code: `src/mqa_advisor/llm.py`, tests under `tests/`. Changed: `prompts.py`, the two v1 prompt files,
  `tests/test_prompts.py`, ADR-0018 and design doc section 6 (file format).
- `pyproject.toml` / `uv.lock`: add `anthropic` and `jsonschema`. `.gitignore`: add `logs/`. `.env.example`: add the key placeholder.
- No database, pipeline or CLI change. Consumers (#21-#23) call the client; wiring the log file into the
  `propose` step and `--dry-run` is #23 (this change provides the run-log object and its "no file" mode).
