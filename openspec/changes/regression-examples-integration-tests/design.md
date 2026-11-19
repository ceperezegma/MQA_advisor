## Context

`propose_themes()` and `propose_keywords()` take a `ResourceText`, an `LLMClient` and the vocabulary, and write
nothing to the database. So an example can be a plain `ResourceText` built from a fixture, with no database. See
proposal.md for why.

## Goals / Non-Goals

**Goals:** a new prompt version can be checked with one command; failures are readable.

**Non-Goals:** scoring quality beyond the loose checks; testing the `propose` step or the database (covered offline).

## Decisions

- **One JSON file** `tests/fixtures/prompts/regression_examples.json`: a list of 8 objects. Simple to review and to
  edit when a candidate is replaced. Alternative: one file per example; rejected as more files for no gain.
- **Checks are pure functions** in `tests/prompt_regression.py` returning a list of failure messages (each naming the
  example and property), so they are unit tested offline with a `FakeBackend` and also used by the live test.
- **The live test uses the real functions** (`propose_themes`, `propose_keywords`) with `AnthropicBackend` and a
  `RunLog` in the default log folder, so the run is logged and priced like any other. The calls are made once per
  example (a module-scoped fixture), checks run per example so each failure names its example.
- **Language check:** tagged title: stored language equals the tag's primary subtag; untagged: the model's code must
  not be `en` when the title is Spanish (the only untagged example). Exact language detection is not attempted.
- **Accepted themes** are my best-judgment draft; the maintainer can edit the JSON later. Flagged in the PR.
- **Resources:** data-gov-lt 2268, 2267, 3, 1306, 1460, 2264; nsip-lu 11; 52north 2 (the draft in the issue
  discussion). Descriptions longer than ~600 characters are kept whole (the prompt sees the real text).

## Risks / Trade-offs

- A live model is not deterministic → checks are loose; a flaky failure is re-run once before concluding.
- Fixtures contain public catalogue text → acceptable, it is open data.
