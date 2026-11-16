## Context

`catalogue_db.py` owns the schema (`SCHEMA`, run by `connect()` with `CREATE ... IF NOT EXISTS`) and
`load_catalogue`, which replaces a resource by deleting its `resource` row and inserting it again;
foreign keys cascade the delete to `property_value`. Its docstring already says any later table must
reference `resource` the same way. The table definition is fixed by design section 5 and ADR-0017;
this change does not revisit it. See proposal.md for motivation.

## Goals / Non-Goals

**Goals:**
- The table, a replace function and a read function, with the mirror-the-folder guarantee.
- A removal count from `load_catalogue` so the cost of a reload is visible.

**Non-Goals:**
- Anything that produces proposals (vocabulary, prompts, LLM client, `propose` step): other tickets.
- Checking that a proposal is a valid theme or keyword (right property, right authority URI, sensible
  text): the producing code owns that. The storage function checks only the `value_kind`/`lang` pairing
  (see the pairing decision below).
- Proposals for `distribution` or embedded Data Services (the CHECK allows `dataset` and
  `dataservice` only; widening it later is a schema change by design).

## Decisions

- **Schema inside `SCHEMA`, nothing else in `connect()`.** The table and index are appended to the
  existing script, so a fresh database and an existing one converge on the next `connect()`, and the
  older-layout check stays as it is. Alternative: a separate migration function: rejected, the project
  has no migrations and `IF NOT EXISTS` is enough for an additive table.
- **A frozen dataclass `ProposedValue` for the 11 non-key fields**, plus
  `replace_proposals(conn, resource_pk, proposals)` and `read_proposals(conn, resource_pk)`. A dataclass
  instead of bare tuples because there are 11 positional fields, mostly strings, easy to swap. The
  resource is identified by `resource_pk`, which the `propose` step already has; looking it up from
  env/catalogue/id is not needed.
- **Transaction handling mirrors `load_catalogue`:** delete, `executemany` insert, `commit`; on any
  exception `rollback` and re-raise; the connection must have no pending writes. The naive-timestamp
  check comes from the existing datetime adapter (raises `ValueError`), so no new validation code.
- **Pairing is validated in `replace_proposals`, not by a CHECK constraint.** Before the first SQL
  statement the function checks every proposal: `value_kind = 'uri'` requires `lang` to be `None`;
  `value_kind = 'literal'` requires a non-empty `lang` (only keywords are literals). A violation raises
  `ValueError` naming the position and the row, so nothing is deleted or inserted and the previous
  proposals stay. Checking the whole list first, instead of relying on rollback, means no write happens
  for input already known to be bad. Why no CHECK: the ticket fixes the table to design section 5,
  which has none; a CHECK failure is a bare `IntegrityError` that cannot name the offending row; the
  function is the only writer, so one place is enough; and `CREATE TABLE IF NOT EXISTS` would not add
  a CHECK to an existing table, so databases would differ by age. Alternative: add
  `CHECK ((value_kind = 'uri' AND lang IS NULL) OR (value_kind = 'literal' AND lang IS NOT NULL))`
  as a second line of defence: rejected to keep the table as designed and avoid two rules to keep in
  step.
- **Read order:** `ORDER BY property_uri, rowid`, i.e. insertion order within a property, so tests and
  the Excel report see a stable order.
- **Removal count by difference.** `load_catalogue` counts the catalogue's proposals (join to
  `resource` on env and catalogue) before its first write and again just before `commit`; the difference
  is the number removed. This covers replaced, pruned and partial loads with one rule and needs no
  per-resource bookkeeping. Alternative: count inside `_replace_resource` and `_prune`: more code in two
  places for the same number.
- **`LoadSummary.proposals_removed: int = 0`**, a new last field with a default, so existing callers and
  tests that build a summary by keyword keep working. The INFO message is logged after `commit`,
  always, also when the count is zero (the user wants every reload tracked), and a rolled-back load
  logs nothing false.

## Risks / Trade-offs

- [Every reload deletes proposals, even a reload that changes nothing] → by design (ADR-0017); the
  INFO message makes the cost visible before the next paid `propose` run.
- [A caller stores a `uri` theme with a `lang`, or a keyword without one] → rejected by
  `replace_proposals` with `ValueError` before any write; see the pairing decision.
- [A log line on every load, even with no proposals] → accepted by the user; one INFO line per load.
- [The count query runs on every load] → two `COUNT(*)` over a join per load, negligible next to
  parsing the files.

## Discrepancies found

- The ticket says `load_catalogue` logs the removed count; design section 7 says the pipeline does.
  This change follows the ticket (logging in `load_catalogue`, count also in `LoadSummary`). The
  pipeline can reuse `proposals_removed` later; the design doc should be aligned in ticket 7/8.
- The ticket's logging section is silent on the zero case; the user decided the INFO line is always
  logged, with the count (including 0).
- The ticket does not say the storage function validates the `uri`/`lang` pairing; the user decided it
  does (see the pairing decision).
