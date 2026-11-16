# Design: LLM proposals for `dcat:theme` and `dcat:keyword`

Status: **approved** (workflow step 4). Decisions are recorded in ADR-0017 (the proposal
design), ADR-0018 (prompt versioning) and ADR-0019 (log, cost and report). Terms (Proposal,
Enrichment, Effective themes, Prompt version) are in `CONTEXT.md`.

## 1. Goal

For every Dataset and standalone Data Service in the database, propose additional `dcat:theme`
and `dcat:keyword` values, using only the resource's title and description. Themes must come from
the EU `data-theme` vocabulary and are validated by code. Prompts are versioned files, and
regression examples run as tests.

Both properties are *Recommended* (Weight 0.5) for Datasets and Data Services in the MQA
methodology; Distributions have neither, so they are out of scope.

## 2. Scope

| In | Out |
|---|---|
| Datasets, standalone Data Services (`resource` rows) | Distributions, embedded Data Services |
| `dcat:theme`, `dcat:keyword` | `dct:temporal` and every other Property |
| Adding values (enrichment) | Changing or removing existing values |

## 3. Flow per resource

1. Read the resource's title, description, existing themes and existing keywords from
   `property_value` (`entity_kind` of the resource's own entity only).
2. **Themes.** Existing themes count when they are in the vocabulary (a Dataset stores a theme as a
   `node` value whose `@id` is the authority URI; a Data Service stores it as a `uri` value).
   If fewer than 5 count, call `propose_themes` asking for between 1 and (5 − existing) new ones.
3. Validate the response: keep codes that exist in the vocabulary, drop duplicates of existing
   themes. The result plus the existing in-vocabulary themes are the **effective themes**.
4. **Keywords.** If the resource has fewer than 10 keywords (of any language), call
   `propose_keywords` with the title, the description and the effective themes' labels, asking for
   between 1 and (10 − existing) new ones. Drop empty values and duplicates (case-insensitive,
   trimmed) of existing or already proposed keywords; do not change the case.
5. If a call has nothing valid left after validation, retry it, up to 3 times. Still empty: an
   `error` (ADR-0019). If the theme call fails, the keyword call still runs, without theme context.
6. Store the valid proposals for the resource, replacing any earlier ones, in one transaction.
7. A resource with no title is skipped with a logged warning. A missing description is fine: the
   title alone is sent. Nothing is guessed.

A resource already at both caps makes no call.

### 3.1 Language

- Tags `en` and `eng` mean English.
- Working language: English if the title has an English version; otherwise the title's language
  (the alphabetically first tag if there are several).
- The description is sent in that language if it exists, otherwise in its own language, labelled
  with that language.
- Keywords are written in the working language and stored with the title's own tag (`eng` in the
  data we have). An untagged title: the model infers the language, returns it as a 3-letter code in
  the keyword call's output, and it is logged as inferred. Only the title's tag counts; the
  description's tag is never used to pick the language.

## 4. Vocabulary

- Source: `vocabularies/data-theme-skos.rdf`, the EU Publications Office *data-theme* table,
  version `20241211-0` (read from `owl:versionInfo`, logged on every run), committed to the repo.
  To refresh it, replace the file; a unit test pins the 14 expected codes, so a change is noticed.
- Parsed with the stdlib `xml.etree` (no `rdflib`). Per concept: URI
  (`http://publications.europa.eu/resource/authority/data-theme/{CODE}`), code, English
  `skos:prefLabel` and English `skos:definition`. Concepts with `at:deprecated="true"` are skipped
  (none today).
- The 14 codes: AGRI, ECON, EDUC, ENER, ENVI, GOVE, HEAL, INTR, JUST, OP_DATPRO, REGI, SOCI, TECH,
  TRAN. 13 have an English definition (`OP_DATPRO`, "Provisional data", has none; its label is
  sent alone).
- The theme prompt receives the entries as lines `CODE — English label: English definition`. The
  model must answer with codes; the code maps them to URIs. Anything not a known code is dropped
  with a warning.

## 5. Storage

New table in `catalogue_db.SCHEMA` (created if missing, no change to existing tables):

```sql
CREATE TABLE IF NOT EXISTS proposed_property_value (
    resource_pk     INTEGER NOT NULL REFERENCES resource(resource_pk) ON DELETE CASCADE,
    entity_id       TEXT NOT NULL,
    entity_kind     TEXT NOT NULL CHECK (entity_kind IN ('dataset', 'dataservice')),
    property_uri    TEXT NOT NULL,       -- dcat:theme or dcat:keyword
    value           TEXT NOT NULL,       -- authority URI (theme) or the keyword text
    value_kind      TEXT NOT NULL CHECK (value_kind IN ('literal', 'uri')),
    lang            TEXT,                -- keywords only
    prompt_name     TEXT NOT NULL,
    prompt_version  INTEGER NOT NULL,
    prompt_sha256   TEXT NOT NULL,
    model           TEXT NOT NULL,
    proposed_at     TIMESTAMP NOT NULL   -- UTC
);
CREATE INDEX IF NOT EXISTS proposed_property_value_resource
    ON proposed_property_value (resource_pk, property_uri);
```

`entity_kind` is keyed like `property_value` so embedded Data Services can be added later without
a schema change. Themes are `uri` rows with the authority URI; keywords are `literal` rows. The
cascade means a reload of the catalogue deletes the proposals: the database always mirrors the
folder (ADR-0017). Re-running `propose` replaces the proposals of each resource it processes.

## 6. Prompts

```
src/mqa_advisor/prompts/
    propose_themes/v1.md
    propose_keywords/v1.md
```

Each file is TOML frontmatter between `+++` lines, then the body (a `string.Template`):

```
+++
version = 1
model = "claude-haiku-5-5"
temperature = 0
max_tokens = 1024
max_items = 5          # the total cap, enforced in code by the caller; the call asks for up to (max_items - existing)
+++
You assign themes from a fixed list ... $title ... $description ... $vocabulary ... $remaining
```

- Structured output through a forced tool call whose input schema is the output schema of the same
  prompt name and **the same version number**: `propose_themes/v1.md` always uses schema
  `("propose_themes", 1)`, never another version (ADR-0018). The frontmatter does not name a
  schema, so a mismatch cannot be written. `minItems` is 1; there is no `maxItems`: structured outputs
  do not support it (the docs allow `minItems` only as 0 or 1), so the cap `max_items` is enforced in
  code after the call (extras dropped with a warning and a count), never by the schema.
  Schemas live in code, in a registry keyed by `(prompt_name, version)`.
- A registry in code gives the active version of each prompt. Committed versions are immutable.
- v1 uses `claude-haiku-5-5` at temperature 0. Whether `claude-sonnet-5-5` is worth the cost is
  decided by running the regression examples against both as separate prompt versions.
- The keyword prompt's theme context (the effective themes) is part of v1, as in section 3.

## 7. The `propose` step

- `pipeline.STEPS` becomes `manifest, fetch, db, propose`; the default `--steps` stays
  `manifest,fetch,db`. Use `--steps propose` or `--steps db,propose`.
- Options for this step: `--limit N` (first N resources by id), `--dry-run` (build every prompt,
  write nothing, print the number of calls and a rough token and cost estimate computed locally
  from text length, labelled as rough), `--workers N` (default 4).
- Before any call: the key `ANTHROPIC_API_KEY` must be set and the catalogue must be in the
  database, else a `PipelineInputError`. `.env.example` gets a placeholder; the real key stays in
  `.env`.
- Calls run in a thread pool; only the main thread writes to SQLite and the log, in resource-id
  order, so a run is deterministic apart from the model's own output.
- API reliability: the SDK's `max_retries=3` with its exponential backoff for 429/5xx, 60-second
  timeout per call. After that the resource gets an `error` row.
- Every resource is attempted and every successful proposal is saved; then, if any `error` row
  exists, the step exits 1 (ADR-0016 pattern).
- After the `db` step the pipeline logs how many proposals the reload removed.

## 8. Reporting and logging

See ADR-0019. Summary: `logs/propose/{catalogue}-{UTC timestamp}.log` (plain text, one block per
call, with prompt, response, tokens, estimated cost, latency, attempt), `propose_report.xlsx` in
the catalogue folder (Errors and Summary sheets, severity `error` or `warning`), and a run cost
total. Prices come from a committed table with an `as_of` date.

Severity: `error` = API failure after retries, response not matching the schema, still nothing
valid after 3 retries. `warning` = invalid theme code dropped, list cut at the cap, unknown
language tag, resource skipped for no title.

## 9. Tests

- **Unit (offline, a fake client):** vocabulary parsing and the 14 pinned codes; prompt loading,
  frontmatter, rendering and the registry; every committed prompt file has a schema with the same name and version (and no schema lacks a prompt), and the frontmatter `version` equals the file's number;
  language selection; cap arithmetic (existing + proposed); duplicate removal; validation;
  retries on empty results; storage, replacement on re-run and cascade on reload; report content;
  cost arithmetic.
- **Integration (`-m integration`, live model, needs the key):** about 8 regression examples under
  `tests/fixtures/prompts/` with loose, deterministic checks (section ADR-0018). Must include a
  standalone Data Service, a resource with an existing theme, and one with no description. The
  maintainer chooses the accepted themes per example; candidates are drafted for review.
- Tests never write to real data folders.

## 10. Proposed code layout

| Module | Responsibility |
|---|---|
| `vocabulary.py` | read the RDF; entries, codes, URIs, labels, definitions, version |
| `prompts.py` + `prompts/` | load a versioned file, registry, frontmatter, sha256, render |
| `proposal_schemas.py` | the output schemas, keyed by `(prompt_name, version)` |
| `llm.py` | the SDK client wrapper: call, retries, token usage, cost (price table), fake for tests |
| `propose.py` | orchestration per resource, validation, storage, report, log |
| `catalogue_db.py` | the new table (schema only) |
| `pipeline.py` | the `propose` step, options, logging setup |

## 11. Suggested ticket order (for step 5)

1. Vocabulary reader (+ pinned-codes test).
2. `proposed_property_value` table and the storage functions.
3. Prompt loader, registry and the schemas.
4. LLM client wrapper: call, retries, usage, cost, log block.
5. Theme proposal for one resource (`propose_themes` v1), with unit tests.
6. Keyword proposal for one resource (`propose_keywords` v1), with unit tests.
7. The `propose` step in the pipeline: cap logic, thread pool, `--limit`, `--dry-run`, `--workers`,
   Excel report and exit codes.
8. Regression examples as integration tests, and the first run to pick the active versions.

## 12. Known costs and risks

- Every `db` step deletes all proposals, so a full re-run is paid again. A catalogue of about
  2,300 Datasets is up to about 4,600 calls. Use `--limit` and `--dry-run` before the first full
  run.
- Prices change; the cost shown is an estimate.
- Keyword quality has no exact test; the regression checks are loose by design.
