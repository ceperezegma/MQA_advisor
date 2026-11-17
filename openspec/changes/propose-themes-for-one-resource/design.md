## Context

See proposal.md (Why) and `specs/theme-proposal/spec.md` for the behaviour. The pieces this builds on already
exist: `vocabulary.load_vocabulary()` / `format_entries()` / `code_to_uri()`, `prompts.load_prompt()` with the
`ACTIVE` registry, `LLMClient.call(prompt, resource=...)` (one call, schema-validated, logged to the `RunLog`,
raises an `LLMError` subclass on failure), and the `property_value` table.

Facts from the real `nsip-lu` database that shape the reader:

- Titles and descriptions are `literal` rows with `lang`; the data uses `eng`.
- A Dataset theme is a `node` row whose JSON has the authority URI as `@id`; a Data Service theme is a `uri` row.
- 4 of 51 Dataset records hold a second `dataset` entity (a related dataset with a `letzdata.public.lu` URI and
  its own title and themes). The loader does not mark which entity is the root.

The prompt `propose_themes/v1.md` has the placeholders `$vocabulary`, `$title`, `$description`,
`$existing_themes` and `$remaining`, and no placeholder for the description's language.

## Goals / Non-Goals

**Goals:**
- One function that the `propose` step (#23) calls per resource, from the main thread or from a worker thread.
- Keep the database read apart from the decisions (language, cap, validation), so the decisions are tested with
  plain values and no database.

**Non-Goals:**
- Storing proposals, the Excel report, the thread pool, `--dry-run` (#23).
- Keywords (#22). The result only carries what #22 needs (the existing themes and the new ones).
- Changing the prompt file, the schemas or the loader.

## Decisions

**Module and shape.** All of it goes in a new `src/mqa_advisor/propose.py` (design doc section 10), which #22
and #23 extend. Three layers:

1. `read_resource(conn, resource_pk) -> ResourceText` — the only function that touches SQLite. It reads the
   `resource` row, picks the own entity, and returns frozen plain values: resource id and type, the own entity
   id (or the reason there is none), titles and descriptions as `(lang, text)` pairs, theme URIs. Picking the own
   entity is a small pure helper over the entity ids, so the "none / several" cases are tested without SQL.
2. Pure helpers: `choose_language(titles, descriptions) -> ModelText | None` (title, description with the
   optional `(language: xxx) ` prefix, working language), `existing_themes(uris, vocabulary)`,
   `validate_codes(codes, existing, remaining, vocabulary) -> (uris, warnings)`.
3. `propose_themes(resource: ResourceText, client: LLMClient, vocabulary: Vocabulary) -> ThemeResult` — builds
   the prompt values, calls, validates, retries, returns. It takes no connection, so worker threads can run it.

Alternative considered: one function taking `(conn, resource_pk)`. Rejected: SQLite connections must stay on the
main thread in #23, and the tests would need a database for every language and validation case.

**Own entity.** Dataset: the `dataset` entity whose id starts with `http://data.europa.eu/88u/` (agreed with the
maintainer). Data Service: the `dataservice` entity rows of the resource (the mapper writes one entity). Exactly
one must be found, else `skipped` with a warning. Alternative: matching `.../{resource_id}`; the prefix rule was
chosen by the maintainer and gives the same entity on all 51 Datasets.

**Theme value parsing.** A `uri` row's value is the URI; a `node` row's value is parsed as JSON and its `@id`
taken. Both kinds are accepted for both resource types, so a future Data Service stored as JSON-LD still works.
A `node` value that is not JSON or has no string `@id` is a malformed database: it raises `ValueError` (the
loader wrote it, so this is a bug, not a catalogue problem).

**Language.** `en` and `eng` are normalised only for the English comparison; the tag reported and used in the
prefix is the tag as stored. Ordering: English first, then tags alphabetically, untagged last; within one tag
the first value by text (the data has one). The description prefix is put in the `$description` value, because
v1 has no placeholder for it (maintainer's choice; prompt files may still be edited in development, but none is
needed here).

**Prompt values.** `$vocabulary` = `format_entries()` joined by newlines; `$existing_themes` = one
`CODE — label` line per existing in-vocabulary theme, or `none`; `$remaining` = the number as text. The prompt is
loaded once per resource with `active_version("propose_themes")`; every attempt sends the same rendered prompt.

**Validation order.** For each returned code in order: not a vocabulary code → warning, dropped (exact match, no
trimming or case folding: `code_to_uri` never guesses); an existing theme or already kept → dropped silently;
else kept. Then the list is cut to `remaining` with one warning saying how many were cut.

**Retry.** A loop of at most 4 attempts (`MAX_EMPTY_RETRIES = 3`). Warnings of every attempt are kept in the
result (the report shows what each attempt returned). Any `LLMError` ends the loop as `error` with its message:
`LLMCallError` already went through the SDK retries, `LLMResponseError` is a schema mismatch (design section 8
calls it an error), `LLMInputError` cannot get better by retrying. Other exceptions are bugs and propagate.

**Result.** A frozen `ThemeResult` with `status` (`"ok" | "skipped" | "error"`), `themes`, `existing_themes`,
`prompt` provenance (`PromptInfo(name, version, sha256, model)` or `None`), `calls`, and `issues: tuple[Issue,
...]`, where `Issue(severity, resource_id, message)`. Each issue is also logged (WARNING / ERROR) when it is
created, with the resource id.

## Risks / Trade-offs

- [The `88u` prefix may not hold for PPE or other catalogues] → a resource without (or with several) such
  entities is skipped with a warning naming it, never guessed; the report will show it.
- [Retries cost money] → at most 4 calls per resource, each logged with its cost in the run log.
- [Repeated warnings across attempts make the report noisier] → accepted: each attempt is a separate billed call
  and its invalid codes are worth seeing when tuning the prompt.
- [Description prefix wording is read by the model as part of the description] → the label is short and
  explicit; regression examples (#24) can check it if a multilingual catalogue is added.

## Migration Plan

None: new module, no schema or data change.
