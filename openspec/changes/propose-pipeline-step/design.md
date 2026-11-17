## Context

The pieces exist: `propose.read_resource` (the only db read), `propose_themes` / `propose_keywords` (pure with respect
to the database, safe in worker threads, return `ThemeResult` / `KeywordResult` with `Issue`s), `LLMClient` +
`RunLog` (per-call log, cost, run total), `catalogue_db.replace_proposals` (one transaction per resource) and
`LoadSummary.proposals_removed`. `pipeline.py` has the step selector but no logging setup and no spec. The design is
`docs/design/theme-keyword-proposals.md` sections 7 and 8; ADR-0016 (finish, report, then fail) and ADR-0019 (report
and severities) already fix the behavior. See `proposal.md` for the scope.

## Goals / Non-Goals

**Goals:**
- One new module that runs the step; `pipeline.py` only selects it, parses options and prints.
- Deterministic stored result for any worker count; one SQLite writer.

**Non-Goals:**
- Changing `propose_themes` / `propose_keywords`, the prompts, the table or `LLMClient`.
- A `--max-cost` stop (rejected in ADR-0019), resuming a run, writing the report on a dry run.

## Decisions

**D1. New module `propose_step.py`.** `run_propose(...)` holds the orchestration, `write_report(...)` the Excel file.
`pipeline.run` calls it through an injectable `proposer` parameter, as it does for the other collaborators.
*Alternative:* put it all in `pipeline.py` (239 lines, would double); or in `propose.py` (592 lines, per-resource logic).

**D2. Flow and ordering.** The main thread reads every `ResourceText` (`read_resource`, ordered by `resource_id`,
cut by `--limit`), then submits one task per resource to a `ThreadPoolExecutor(workers)`. A task runs
`propose_themes` then `propose_keywords(resource, client, theme_result, vocabulary)`. The main thread takes the
futures in submission order and, for each, writes with `replace_proposals`, so writes happen in resource-id order
whatever the worker timing. *Alternative:* `as_completed` (writes in completion order, so a crash leaves a
non-deterministic subset).

**D3. What is written.** A resource's rows are its theme URIs (`uri`, no lang) and its keywords (`literal` with the
result's `lang`), `entity_id` = the own entity, `entity_kind` = the resource type, provenance from `result.prompt`, one
UTC `proposed_at` per resource. Rule: write (replace) unless the resource has no valid proposal and some call failed;
then the earlier proposals stay. A resource with both calls skipped is written empty (clears stale rows, as the
step "replaces earlier proposals"). If only one call failed, the other's proposals still replace the old rows.
*Alternative:* per-property replacement: needs a new storage function for a rare case.

**D4. Unexpected exceptions.** A worker exception that is not an `LLMError` (already turned into an `error` issue by
the propose functions) becomes an `error` row for that resource (`unexpected: {Type}: {message}`) and the others go
on (ADR-0016: collect everything). This includes `PromptError`; it makes no call, so a broken prompt costs nothing and
shows up as an error row per resource. *Alternative:* abort on `PromptError`: rejected for the sake of one rule.

**D5. Cost and tokens.** Each task wraps the client in a small counting wrapper that sums the `LLMResult` tokens and
cost of the answered calls, for the per-resource INFO line. The run total (including failed-but-billed calls) is
`RunLog.total_cost_usd`, which is what the summary and the report show. So the per-resource lines can add up to less
than the total; the summary line says which one is authoritative.

**D6. Dry run.** The same flow with a `DryRunClient` in place of `LLMClient`: its `call` records the prompt sizes and
returns a canned answer that validates (all vocabulary codes for themes, so the validator cuts to the remaining count;
one keyword and language `eng` for keywords), so exactly the calls of a clean run are counted (no empty-answer
retries) and the keyword prompt gets effective-theme context. No model, no `RunLog` file (`enabled=False`), no
`replace_proposals`, no report. Estimate: input tokens = UTF-8 bytes / 4 (rough), output tokens = a fixed 100 per
call, priced with `estimate_cost`; the output is labelled "rough". A model without a price is reported as
"not priced" instead of failing.

**D7. Input checks.** `pipeline._check_inputs` gains: when `propose` is selected and not `--dry-run`, the key must load
(`llm.load_api_key`, `LLMInputError` re-raised as `PipelineInputError`); when `propose` is selected without `db`, the
database must have resources for `(env, catalogue)`. `run_propose` repeats the catalogue check (after a `db` step it
is the first moment it can be true) so it never runs on nothing. All before any call.

**D8. Report and exit.** `write_report` always writes `propose_report.xlsx` in `{fetched_data_dir}/{env}/{catalogue}/`
with openpyxl (like `ingest`): *Errors* (resource type, resource id, call `themes`/`keywords`, severity, message,
prompt name, version, model, UTC time) sorted by resource id, severity `error` first; *Summary* with env, catalogue,
run id, log path, prices date, resources per outcome (stored / skipped / error), themes and keywords stored, `error`
and `warning` counts, calls, tokens, estimated cost. Issues come from `ThemeResult.issues` (call `themes`) and
`KeywordResult.issues` (call `keywords`); an identical severity+message repeated by the keyword call (the "no title"
skip) is dropped. After saving and reporting, any `error` raises `ProposeIncompleteError` (report path or
`report_error`, the failures, as `FetchIncompleteError`); `pipeline.main` prints one stderr line and exits 1. A report
write failure with zero errors raises its `OSError` (already caught by `main`).

**D9. Logging.** `pipeline.main` calls `logging.basicConfig(level=INFO)` (the console handler the `RunLog` lines
reach, per ADR-0019). `run_propose` logs INFO per resource and a run summary; issues are logged by the propose
functions already (WARNING/ERROR). The log file is closed in a `finally`; a failure to write it surfaces as the
`LLMError` of `RunLog.close` after the report is written.

**D10. After `db`.** `load_catalogue` already logs `proposals_removed` (WARNING when non-zero, INFO otherwise) and
returns it in `LoadSummary`. The pipeline's printed `Database:` line already shows it through the summary; no new
logging is needed, only a test that pins it.

**D11. Option scope.** `--limit`, `--dry-run`, `--workers` are rejected by `main` (`parser.error`) unless `propose` is
selected; `run()` also raises `ValueError` for `workers < 1` or `limit < 1`.

## Risks / Trade-offs

- [Per-resource cost lines can omit failed calls] → totals come from the run log; stated in the summary line.
- [Dry-run estimate is crude: bytes/4 and fixed output size] → labelled rough; real cost is in the log after a run.
- [All futures are submitted at once; results wait in memory until written] → one small object per resource, fine
  for ~2,300 resources (≈4,600 calls).
- [A worker blocked on the in-order write of an earlier slow resource delays writes, not calls] → calls continue in
  the pool; only the writes queue.
- [Replace-on-write drops an earlier theme proposal when the theme call fails but keywords succeed] → accepted
  (D3); the error row names the failed call, and a re-run restores it.

## Open Questions

None that change the specs or the tasks.
