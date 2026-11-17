## 1. Step core (`propose_step.py`)

- [x] 1.1 Add the resource listing (ordered by id, `--limit`) and the per-resource task (themes then keywords, unexpected exceptions become `error` rows); verify with unit tests on a small database using `FakeBackend`: both calls made, a failing resource does not stop the others
- [x] 1.2 Add the in-order write of each result via `replace_proposals` (rule D3); verify: proposals stored with the right kind/lang/provenance, a re-run replaces, an all-failed resource keeps its earlier rows, results identical for 1 and 4 workers
- [x] 1.3 Add the counting client wrapper, per-resource INFO line and run summary (D5, D9); verify the log lines with `caplog` and that totals match the `RunLog`

## 2. Dry run

- [x] 2.1 Add `DryRunClient` and the rough estimate (D6); verify: no rows written, no log file or folder created, no report, the printed call count equals a clean run's, and the estimate is labelled rough

## 3. Report and exit codes

- [x] 3.1 Add `write_report` (Errors and Summary sheets, D8) and `ProposeIncompleteError`; verify by reading the workbook back: Errors rows and Summary counts, tokens and cost, written with zero errors too
- [x] 3.2 Verify the error path: one failing resource gives saved proposals for the others, an `error` row and `ProposeIncompleteError`; a no-title resource gives a `warning` row and no exception; a report that cannot be written still raises the failures and names the report problem

## 4. Pipeline wiring

- [x] 4.1 Add `propose` to `STEPS` (default `--steps` unchanged), `--limit` / `--dry-run` / `--workers` (default 4) with the usage errors of D11, the `proposer` injection in `run()`, the input checks of D7 and logging setup in `main`; verify: default steps exclude `propose`, `--steps propose` and `db,propose` work, unknown catalogue and missing key give `PipelineInputError`, options without the step are a usage error
- [x] 4.2 Print the propose outcome and map `ProposeIncompleteError` to one stderr line and exit 1 in `main`; verify with `main()` tests (exit code 1 on error, 0 on warnings only)
- [x] 4.3 Pin the post-`db` proposals-removed log with an end-to-end test: propose, then `db`, and the removal count is logged

## 5. Docs and quality gates

- [x] 5.1 Update the `pipeline` docstring (usage with the new step and options), `CLAUDE.md` (propose step, options, `propose_step.py`) and the table's cascade mention; verify by reading them against the code
- [x] 5.2 Run `uv run ruff check src/ tests/`, `uv run ruff format src/ tests/` and `uv run pytest`; verify all pass
- [x] 5.3 Do a `--dry-run` on a catalogue in the local database (no key needed) and confirm nothing is written; verify the printed estimate and the unchanged table

## 6. Review fixes (code review, python-reviewer, silent-failure-hunter, test analysis)

- [x] 6.1 Per-resource isolation: read and store failures become `error` rows (D4, D12); verify tests for an unreadable resource and a failing `replace_proposals`, report still written, others stored
- [x] 6.2 Keep the earlier proposals of a failed call's property (D3); verify with a themes-only and a keywords-only failure over existing proposals, and with a re-run that replaces
- [x] 6.3 Report: any exception is a report problem, atomic save, illegal characters removed, no formulas, synthesized error rows, raise whenever any resource failed (D8); verify tests for each
- [x] 6.4 A successful result that cannot be stored is an `error`; a `PromptError` stops the run (D3, D4); verify tests
- [x] 6.5 Dry run: failures raise without a report, unpriced model counts zero with a warning, the "up to N" wording is pinned against the real prompt (D6); verify tests including a pre-existing report left untouched
- [x] 6.6 Pre-flight checks (vocabulary, prompt files, read-only database check), `main` catches `RuntimeError` and `VocabularyError`, input checks run before `db` in `db,propose` (D7); verify tests
- [x] 6.7 Logging scope (D9), summary printed on failure, `ProposeOptions`, single validation point, shared failure-line helper, run-log error in the exception message (D8, D11, D12); verify tests and a read-through
- [x] 6.8 Close the test gaps: write order with a slow first resource, `--limit` order with out-of-order ids and two resource types, a standalone Data Service and a resource without own entity, all error-row and Summary columns, backend and log failures, `main` exit codes, default steps through `run()`, a catalogue under another environment, the post-`db` check
- [x] 6.9 Run ruff, the whole suite and a real `--dry-run`; verify all pass
