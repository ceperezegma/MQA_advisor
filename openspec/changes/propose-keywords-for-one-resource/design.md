## Context

See proposal.md and `specs/keyword-proposal/spec.md`. Builds on #21 (`ResourceText`, `read_resource`,
`choose_language`, `Issue`, `PromptInfo`, `ThemeResult`, the retry constant) and on `propose_keywords/v1.md`, whose
placeholders are `$title`, `$description`, `$language`, `$themes`, `$existing_keywords`, `$remaining`, with
`max_items = 10`. Its schema returns `keywords` (min 1) and a required `language` (`^[a-z]{2,3}$`).

## Goals / Non-Goals

**Goals:** one function the `propose` step calls per resource, thread-safe like `propose_themes`; decisions testable
without a database.

**Non-Goals:** storage, report, thread pool (#23); changing the prompt or schema.

## Decisions

**Same module, same layers.** `read_resource` also reads `dcat:keyword` `literal` values of the own entity into
`ResourceText.keywords` (`(lang, text)` pairs; the new field defaults to `()` so theme code and tests are
unaffected). Pure helper: `validate_keywords(keywords, existing, remaining)`. `propose_keywords` takes no connection.

**Signature.** `propose_keywords(resource, client, theme_result: ThemeResult | None, vocabulary)`. The effective
themes are `theme_result.existing_themes + theme_result.themes` when its status is `ok` or `skipped`; they are empty
when the status is `error` or `theme_result` is `None`. Their English labels are sent joined by `, `; with no theme
context `$themes` is `none`.

**Existing keywords.** Count = number of non-blank existing keyword values, any language. `remaining = 10 - count`;
`<= 0` means skipped without a warning and without a call. The existing values are listed one per line in
`$existing_keywords` (`none` when empty). No own entity or no title: skipped with a warning, as for themes.

**Language.** `choose_language` gives the working language (the title's tag). `$language` is that tag, or `unknown`
for an untagged title. Keywords are stored with the title's own tag; for an untagged title the model's `language`
value is used and an INFO is logged ("language inferred"). The description's tag is never used.

**Validation.** In order: strip; drop empty; drop a value whose `casefold()` equals an existing or already kept
keyword; keep the original case; then cut to `remaining` with one WARNING stating how many were dropped.

**Retry and errors.** Same as themes: up to 3 retries when nothing valid is left (4 calls), then an `error` result
and an ERROR log; any `LLMError` ends at once as `error`. A prompt whose `max_items` is not 10 raises `PromptError`.

**Result.** `KeywordResult(resource_id, status, keywords, lang, lang_inferred, existing_count, prompt, calls,
issues)`; `keywords` is empty unless `ok`.

## Risks / Trade-offs

- [Model language code vs data] The issue says the inferred code is 3 letters, but prompt v1 and its schema ask for
  ISO 639-1 (2 letters; 3 only when none exists), while the data uses `eng`. This change stores the code as returned
  and does not convert 2- to 3-letter codes. Flagged for the maintainer at review.
- [Duplicate warning] A resource with no title is warned about by both the theme and the keyword call; #23 can
  deduplicate in the report.

## Migration Plan

None.
