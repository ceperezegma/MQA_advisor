## Why

The theme and keyword proposals for one resource exist (#21, #22), as do the table (#18) and the LLM client (#20), but
nothing runs them over a catalogue. Issue #23 wires them into the pipeline as the opt-in `propose` step, with the cost
controls (`--limit`, `--dry-run`), a worker pool and an Excel report, so a catalogue's proposals can be produced and
reviewed.

## What Changes

- `pipeline.STEPS` gains `propose` (after `db`). The default `--steps` stays `manifest,fetch,db`, so `propose` is
  always opt-in; `--steps propose` and `--steps db,propose` work, and `propose` alone reads the existing database.
- New options `--limit N` (first N resources by id), `--dry-run` (build every prompt, call no model, write no rows and
  no log file, print the number of calls and a rough token and cost estimate computed from text length) and
  `--workers N` (default 4).
- Before any call the step checks that `ANTHROPIC_API_KEY` is set and the catalogue is in the database, else
  `PipelineInputError`.
- Per resource: themes call, then keywords call. Calls run in a thread pool; only the main thread writes to SQLite, in
  resource-id order, replacing earlier proposals.
- Every resource is attempted and every valid proposal is saved. `propose_report.xlsx` (sheets Errors and Summary,
  severity `error`/`warning`) is always written in the catalogue folder, even with zero errors. If any `error` exists the
  step exits 1 after saving and reporting (ADR-0016 pattern); if the report cannot be written the failures are still
  raised.
- After the `db` step the pipeline logs how many proposals the reload removed.
- Logging: INFO per resource (calls, items stored, cost), a run summary with totals and cost, WARNING/ERROR rows as in
  ADR-0019, plus the per-call log of #20.
- Docs: the `pipeline` module docstring, `CLAUDE.md` and the OpenSpec spec of the pipeline are updated.

No breaking change: the default run is unchanged.

## Capabilities

### New Capabilities
- `propose-step`: the `propose` pipeline step: selection and options, input checks, per-resource flow and write order,
  `--limit`/`--dry-run`/`--workers`, the Excel report, exit codes and logging. The repo has no OpenSpec spec of the
  pipeline yet (the earlier steps were never specified there), so the step gets its own capability.

### Modified Capabilities
<!-- none: the steps manifest/fetch/db keep their behavior; the table, client and proposal specs are unchanged -->

## Impact

- Code: `src/mqa_advisor/pipeline.py` (step, options, `main`, logging setup, post-`db` log), a new module for the step's
  orchestration and report (see design), `src/mqa_advisor/catalogue_db.py` only if a helper to list the resources is
  needed.
- Docs: `CLAUDE.md`, the `pipeline` docstring, `.env.example` (already has the key placeholder).
- Dependencies: none new (`openpyxl` is already a runtime dependency).
- Tests: new unit tests with the fake backend; no network.
