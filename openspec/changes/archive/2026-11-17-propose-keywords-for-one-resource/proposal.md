## Why

Issue #21 added the theme proposal for one resource. Issue #22 adds the same for `dcat:keyword`: for one Dataset or
standalone Data Service, return the valid new keywords, using the effective themes as context. The `propose` step
(#23) will call both and store the results.

## What Changes

- `src/mqa_advisor/propose.py` gains:
  - `ResourceText.keywords`: the own entity's existing `dcat:keyword` values (any language), read by `read_resource`;
  - `propose_keywords(resource, client, theme_result, vocabulary)`: skips when 10 keywords already exist, otherwise
    calls the active `propose_keywords` prompt through `LLMClient` with the title, the description (same language
    rule as #21), the labels of the effective themes (existing in-vocabulary + just proposed; none when the theme
    call failed), the existing keywords and the number asked for (10 minus existing); validates the answer (trim,
    drop empty, drop case-insensitive duplicates of existing or already proposed, keep the case, cut at the cap),
    retries an empty result up to 3 times, and returns a `KeywordResult`;
  - the language the keywords are stored with: the title's own tag, or, for an untagged title, the language the
    model returned (logged as inferred).
- No change to the prompt file, schemas, LLM client or database schema. Nothing is written to the database
  (storage is #23).

## Capabilities

### New Capabilities
- `keyword-proposal`: proposing additional `dcat:keyword` values for one resource: existing keywords, theme
  context, call, validation, retry, storage language and the result handed to the caller.

### Modified Capabilities
<!-- None: theme-proposal requirements are unchanged; ResourceText only gains a field. -->

## Impact

- Code: `src/mqa_advisor/propose.py`; new `tests/test_propose_keywords.py`.
- No new dependency, no network in tests (`FakeBackend`).
- Docs: the `propose.py` line of `CLAUDE.md` mentions keywords.
