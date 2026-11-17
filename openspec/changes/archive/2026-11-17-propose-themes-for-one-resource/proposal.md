## Why

The vocabulary (#17), the proposals table (#18), the prompts (#19) and the LLM client (#20) exist, but nothing yet
turns one resource of the database into a theme proposal. Issue #21 adds that step: for one Dataset or standalone
Data Service, build the model input from its own metadata and return the valid new `dcat:theme` values. The
keyword proposal (#22) and the `propose` pipeline step (#23) build on it.

## What Changes

- New module `src/mqa_advisor/propose.py` with:
  - a reader that takes one `resource` row and returns the resource's own title(s), description(s) and existing
    themes from `property_value`. The own entity is, for a Dataset, the `dataset` entity whose id starts with
    `http://data.europa.eu/88u/` (a record can also hold a related `dcat:Dataset` under another URI, which is
    ignored); for a standalone Data Service, its single `dataservice` entity;
  - the language rule of design section 3.1 (English title if present, else the alphabetically first title tag;
    the description in that language, else in its own language prefixed `(language: xxx) `);
  - `propose_themes(...)`: counts the existing in-vocabulary themes, calls the active `propose_themes` prompt
    through `LLMClient` asking for 1..(5 - existing) new themes, validates the answer (unknown codes dropped,
    duplicates of existing themes dropped, list cut at the cap), retries an empty result up to 3 times, and
    returns a result with the new themes as authority URIs, the existing in-vocabulary themes, the prompt
    provenance, and the warnings and errors for the report.
- No change to the prompt file, the schemas, the LLM client or the database schema. Nothing is written to the
  database: storage is #23.

## Capabilities

### New Capabilities
- `theme-proposal`: proposing additional `dcat:theme` values for one Dataset or standalone Data Service: reading
  the resource's own metadata, choosing the language, calling the model, validating, retrying, and the result
  handed to the caller.

### Modified Capabilities
<!-- None: the prompt, schema, vocabulary, LLM client and database requirements are unchanged. -->

## Impact

- Code: new `src/mqa_advisor/propose.py`; new `tests/test_propose_themes.py`. Uses `vocabulary`, `prompts`,
  `llm` and the `property_value` table read-only.
- No new dependency, no schema change, no network in the tests (`FakeBackend`).
- Docs: `CLAUDE.md` architecture notes get a line for `propose.py`.
- Real data: 4 of the 51 `nsip-lu` Datasets hold a second, related `dcat:Dataset`; the own-entity rule keeps
  their related dataset's title and themes out of the input.
