---
status: accepted
---

# Every LLM call is logged with its cost, and the step reports its errors in an Excel file

The `propose` step spends money and its output is not deterministic, so each call must be
reconstructible after the fact, and a failing resource must be visible without reading logs. The
fetch step already sets the pattern (ADR-0016: attempt everything, always write `report.xlsx`,
then fail if anything failed).

## Decision

- **Interaction log:** one plain-text file per run, `logs/propose/{catalogue}-{UTC timestamp}.log`
  (git-ignored), written through the standard `logging` module. One delimited block per call:
  run id, resource, prompt name/version/hash, model, the exact system and user text sent, the raw
  response, the validation outcome, input/output/cached token counts, estimated cost in USD,
  latency and attempt number. An INFO line per call carries the cost, and a run total is logged at
  the end. The API key is never logged. Prompts and responses contain only public catalogue
  metadata. Every run creates a new file; nothing deletes old ones, so they stay until the
  maintainer removes them (`logs/` is git-ignored). A `--dry-run` calls no model, so it writes
  no log file.
- **Cost:** computed from a committed price table (USD per million tokens, with an `as_of` date)
  and labelled "estimated". It is not stored in the database: a reload cascades away the
  proposals, which would take the cost history with it.
- **Report:** `propose_report.xlsx` in the catalogue folder, written on every run, even with zero
  errors. Sheet *Errors*: one row per problem (resource type and id, which call, category, message,
  prompt name/version, model, UTC time, severity). Sheet *Summary*: counts per category, tokens and
  total cost. If the report cannot be written, the failures are still raised, as in ADR-0016.
- **Severity:** `error` makes the run exit 1 after everything has been processed and saved (API
  failure after the SDK's retries, response that does not match the schema, no valid value after
  the 3 retries of an empty result). `warning` does not fail the run (an invalid theme code dropped,
  a list cut at the cap, a language tag without a known meaning, a resource skipped for having no
  title). Every warning is also logged, so nothing is dropped silently.
- **Cost controls:** `--limit N` and `--dry-run` (prints the number of calls and a rough token and
  cost estimate, writes nothing). A `--max-cost` stop was considered and rejected.

## Consequences

- `logs/` is a new git-ignored folder, and this is the first file logging in the project;
  `pipeline.main` gains a logging setup.
- The prices go stale: the table is a manual update, and the cost is an estimate, not an invoice.
