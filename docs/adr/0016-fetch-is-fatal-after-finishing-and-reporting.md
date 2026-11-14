---
status: accepted
---

# The fetch step finishes and reports, then fails if any resource failed

The ingestor was copied from MQA_test, where a Test Run is best effort: a resource that fails all
its retries is marked `Failed` in `report.xlsx` and the run goes on with the rest. In this app the
pipeline documents the opposite ("any failure of a selected step is fatal": the database is the
product), but `run_catalogue` swallowed every `ResourceFetchError` and returned the report path, so
a catalogue with failed downloads, or with bad credentials or a host that is down, exited 0 and the
`db` step loaded whatever was on disk. Issue #6.

## Decision

- `PPEIngestor.run_catalogue` still attempts **every** fetchable resource and always writes
  `report.xlsx` (the failure-isolation requirement of the `ppe-ingestion` spec is kept).
- It collects every worker exception, not only `ResourceFetchError`, so one unexpected error (a disk
  write failing, say) never aborts the others or leaves queued fetches running.
- After the report is written, if any resource failed it raises `FetchIncompleteError`, carrying
  the report path, the failures (resource type, id, cause) sorted by type and id so the output does
  not depend on thread timing, and the number of fetchable resources. If the report itself cannot be
  written, the error still carries the failures (`report_path` is `None`, `report_error` says why).
- `pipeline.run` lets the error propagate, so the `db` step does not run; the ingestor is still shut
  down. `pipeline.main` prints one stderr line (counts, report path, up to 10 failures) and exits 1.
- The report keeps deriving `Success`/`Failed` from the files on disk (ground truth). A failed
  `--force` re-fetch that leaves an older file in place is therefore still shown as `Success` in the
  report, while the error is raised; changing that is a separate spec change.

## Consequences

- A catalogue with even one permanently missing resource no longer completes `fetch,db` in one
  command. Re-running resumes: existing files are skipped and only the missing ones are retried.
- A full database load prunes the rows of resources whose files are missing (spec
  `catalogue-database`), so `--steps db` after a failed fetch used to delete data silently. Issue
  #9 closed that in the pipeline: the db step refuses a folder with no resource files (never
  overridable) and, unless `--allow-partial` is given, one whose `report.xlsx` has `Failed` rows.
  A folder without a report loads, because its completeness cannot be told. The deliberate route
  for a catalogue with permanently missing resources is `--steps fetch` (fails, writes the report),
  then `--steps db --allow-partial`.
- Callers of `run_catalogue` (`tests/manual_ingestion.py` and the integration tests) now see an
  exception where they used to see a `Failed` row; the integration tests were not run.
