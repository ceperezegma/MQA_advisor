## 1. Step core (`propose_step.py`)

- [ ] 1.1 Add the resource listing (ordered by id, `--limit`) and the per-resource task (themes then keywords, unexpected exceptions become `error` rows); verify with unit tests on a small database using `FakeBackend`: both calls made, a failing resource does not stop the others
- [ ] 1.2 Add the in-order write of each result via `replace_proposals` (rule D3); verify: proposals stored with the right kind/lang/provenance, a re-run replaces, an all-failed resource keeps its earlier rows, results identical for 1 and 4 workers
- [ ] 1.3 Add the counting client wrapper, per-resource INFO line and run summary (D5, D9); verify the log lines with `caplog` and that totals match the `RunLog`

## 2. Dry run

- [ ] 2.1 Add `DryRunClient` and the rough estimate (D6); verify: no rows written, no log file or folder created, no report, the printed call count equals a clean run's, and the estimate is labelled rough

## 3. Report and exit codes

- [ ] 3.1 Add `write_report` (Errors and Summary sheets, D8) and `ProposeIncompleteError`; verify by reading the workbook back: Errors rows and Summary counts, tokens and cost, written with zero errors too
- [ ] 3.2 Verify the error path: one failing resource gives saved proposals for the others, an `error` row and `ProposeIncompleteError`; a no-title resource gives a `warning` row and no exception; a report that cannot be written still raises the failures and names the report problem

## 4. Pipeline wiring

- [ ] 4.1 Add `propose` to `STEPS` (default `--steps` unchanged), `--limit` / `--dry-run` / `--workers` (default 4) with the usage errors of D11, the `proposer` injection in `run()`, the input checks of D7 and logging setup in `main`; verify: default steps exclude `propose`, `--steps propose` and `db,propose` work, unknown catalogue and missing key give `PipelineInputError`, options without the step are a usage error
- [ ] 4.2 Print the propose outcome and map `ProposeIncompleteError` to one stderr line and exit 1 in `main`; verify with `main()` tests (exit code 1 on error, 0 on warnings only)
- [ ] 4.3 Pin the post-`db` proposals-removed log with an end-to-end test: propose, then `db`, and the removal count is logged

## 5. Docs and quality gates

- [ ] 5.1 Update the `pipeline` docstring (usage with the new step and options), `CLAUDE.md` (propose step, options, `propose_step.py`) and the table's cascade mention; verify by reading them against the code
- [ ] 5.2 Run `uv run ruff check src/ tests/`, `uv run ruff format src/ tests/` and `uv run pytest`; verify all pass
- [ ] 5.3 Do a `--dry-run` on a catalogue in the local database (no key needed) and confirm nothing is written; verify the printed estimate and the unchanged table
