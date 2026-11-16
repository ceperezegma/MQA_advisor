---
status: accepted
---

# Prompts are immutable versioned files, and every proposal records which one made it

Prompts decide the output, so they are code-like artefacts that change over time and must be
comparable and reversible. Options considered: prompts as files in git, immutable version files,
metadata in the files, a hosted prompt-management service, recording the version with every output,
and regression evaluation. We take the first three, the recording, and the evaluation; a hosted
service is overkill for one developer and a CLI pipeline.

## Decision

- **Layout:** `src/mqa_advisor/prompts/{prompt_name}/v{N}.md`. A committed version is never edited:
  a change is a new `v{N+1}`. Two prompts exist in v1: `propose_themes` and `propose_keywords`.
- **Format:** Markdown whose first block is TOML frontmatter, parsed with the stdlib `tomllib`
  (no PyYAML). Frontmatter fields: `version` (must equal the `N` in the file name), `model`,
  `temperature`, `max_tokens`, and the output limits. The body is a `string.Template` in two parts
  split by a line `---USER---`: the system part (role, rules, fixed lists such as the theme
  vocabulary) and the user part (the resource's title and description and the counts). The two are
  sent separately, as the model's system and user messages; untrusted catalogue text only ever goes
  in the user part. The model is part of the prompt version: changing it is a new version.
- **One-time exception:** `v1` of both prompts was rewritten in place to add the system part
  (ticket #20) because no proposal had ever been produced or stored with it. From the first real
  run on, `v1` is frozen like any other version.
- **Active version:** a registry in code (`ACTIVE = {"propose_themes": 1, ...}`). Rollback is a
  one-line change that shows up in a PR.
- **Output schema: one per prompt version, with the same version number.** Prompt `v{N}` of
  `{prompt_name}` always uses output schema `v{N}` of the same name, and no other: prompt `v1`
  cannot be used with schema `v2`, nor `v2` with schema `v1`. The schemas are defined in code, in
  a registry keyed by `(prompt_name, version)`, for example `("propose_themes", 1)`. Nothing in the
  frontmatter names a schema, so a mismatch cannot be written. A committed schema is never edited,
  like its prompt; a different output shape is a new prompt version together with a new schema
  version. A new prompt version whose output shape does not change still gets its own schema
  entry, which may reuse the previous definition explicitly in code. Unit tests check that:
  - every committed prompt file has a schema with the same name and version, and every schema has
    a prompt file;
  - the frontmatter `version` equals the number in the file name;
  - the call for a prompt version is built with that version's schema (never the active one's).
- **No item limit in the schema:** the schema has `minItems` 1 and no `maxItems`. Structured outputs do
  not support `maxItems` (the docs allow `minItems` only as 0 or 1), so the cap, the frontmatter's
  `max_items`, is enforced in code after the call, never silently (a warning with the count).
- **Provenance:** every row of `proposed_property_value` stores `prompt_name`, `prompt_version`,
  `prompt_sha256` (hash of the file content, to detect an edit of a supposedly immutable version)
  and `model`; the interaction log repeats them.
- **Regression examples:** about 8 hand-picked resources (title, description, language and loose
  checks, committed under `tests/fixtures/prompts/`) run as `integration` tests against the live
  model. The checks are deterministic (codes in the vocabulary, count within 1..cap, keyword
  language, at least one theme from an accepted set chosen by the maintainer), never an exact
  match. A new version must pass them before it becomes the active one. Offline unit tests with a
  fake client cover loading, rendering, validation, caps and storage.

## Consequences

- The `prompts/` folder grows with every version; prompts are small, so this is accepted.
- A stored `prompt_version` is enough to know the output shape that produced a row: the schema
  has the same name and number. The price is a small duplicated schema entry when a new prompt
  version changes only the wording or the model.
- Comparing two versions (for instance Haiku against Sonnet, or adding existing-theme context)
  means running the same examples with both and reading the results; no tooling is built for it.
