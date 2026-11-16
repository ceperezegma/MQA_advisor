## Why

The theme/keyword proposal feature (ADR-0017) calls an LLM with prompts that decide the output. ADR-0018
requires those prompts to be immutable, versioned files whose version is bound to an output schema of the
same name and number, so any stored proposal can be traced to the exact prompt and output shape that made it.
Nothing in the code loads prompts or defines these schemas yet; tickets #20-#23 (LLM client, theme and keyword
proposals, pipeline step) all depend on them. This is ticket #19 (Proposals 3/8), and it can be built in
parallel with #17 and #18.

## What Changes

- Add the two v1 prompt files `src/mqa_advisor/prompts/propose_themes/v1.md` and
  `src/mqa_advisor/prompts/propose_keywords/v1.md` (TOML frontmatter + `string.Template` body). The prompt
  wording is drafted in `design.md` and reviewed with this change.
- Add `prompts.py`: a loader that parses the frontmatter, renders the body and returns the SHA-256 of the file
  content; the active-version registry; errors for unknown name/version, version mismatch, malformed frontmatter
  and missing placeholder values.
- Add `proposal_schemas.py`: the output schemas, in code, keyed by `(prompt_name, version)`, with `minItems` 1
  and no `maxItems` (the structured-output schema subset does not support it, so the cap `max_items` of the
  frontmatter is enforced in code by #21/#22); the keyword schema also carries the required `language` field
  (2 or 3 lowercase letters).
- Make sure the wheel build ships the prompt files, with an `integration`-marked test that builds the wheel.
- Update ADR-0018 and design document section 6 to say the cap is enforced in code, not in the schema.
- No change to existing modules, tables, pipeline steps or dependencies (`tomllib`, `string` and `hashlib` are
  stdlib).

## Capabilities

### New Capabilities
- `prompt-versioning`: versioned prompt files, their loader and active-version registry, and the output schemas
  bound to each prompt version.

### Modified Capabilities
<!-- None: no existing requirement changes. -->

## Impact

- New code: `src/mqa_advisor/prompts.py`, `src/mqa_advisor/proposal_schemas.py`, the two `v1.md` files, and
  tests under `tests/`.
- `pyproject.toml`: only if the build check shows the `.md` files are not packaged (expected: no change).
- No new dependencies. No database, pipeline or CLI change. Consumers (#20-#23) call the loader and the schema
  function; they are out of scope here.
