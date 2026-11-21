## Why

A new prompt version for `propose_themes` or `propose_keywords` can silently get worse. ADR-0018 requires a small set of
hand-picked resources checked against the live model before a version becomes active; issue #24 delivers it.

## What Changes

- Add 8 regression examples as JSON fixtures under `tests/fixtures/prompts/`: title, description, language, existing
  themes and keywords, and the accepted themes chosen for each.
- Add `integration` tests that run each example against the live model with the active prompts and apply loose,
  deterministic checks (codes in the vocabulary, 1..cap new themes and keywords, keywords in the title's language,
  at least one accepted theme).
- Offline unit tests for the checks themselves (with a fake client).
- The tests skip with a clear reason when `ANTHROPIC_API_KEY` is missing and are excluded by default.
- The first live run (cost, results) is recorded in the PR; it confirms v1 as the active version.

## Capabilities

### New Capabilities
- `prompt-regression-examples`: the committed examples and the integration tests that check a prompt version against
  the live model.

### Modified Capabilities

## Impact

Tests and fixtures only; no change to `src/`. The integration tests cost a few cents per run (8 resources, about 16
calls on `claude-haiku-5-5`) and write interaction logs under `logs/propose/` like any run.
