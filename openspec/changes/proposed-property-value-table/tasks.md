## 1. Schema

- [x] 1.1 Append `proposed_property_value` and index `proposed_property_value_resource` to `SCHEMA` exactly as in design section 5, and verify with tests: fresh database, database with the other tables and rows, repeated `connect()`, and CHECK/foreign-key rejections (bad `entity_kind`, bad `value_kind`, orphan `resource_pk`)

## 2. Storage functions

- [ ] 2.1 Add the `ProposedValue` dataclass and `replace_proposals(conn, resource_pk, proposals)` (raise `RuntimeError` first if `conn.in_transaction`; check the `uri`/`lang` pairing and non-empty `value`, `prompt_name`, `model`, `prompt_sha256` of the whole list and raise `ValueError` naming the row; then delete + insert + commit, rollback on error), and verify with tests: `uri` row with `lang` rejected, `literal` row without `lang` rejected, violation after valid proposals keeps the previous rows, first write, same input twice, replacement, empty list, other resources untouched, failure mid-list keeps the previous rows, naive timestamp raises
- [x] 2.2 Add `read_proposals(conn, resource_pk)` and verify with tests: round trip including `lang` and the timezone-aware time, stable order, empty list

## 3. Reload behavior

- [ ] 3.1 Count the catalogue's proposals before and just before commit in `load_catalogue`, add `LoadSummary.proposals_removed` (default 0) and a message after commit, always (INFO at zero, WARNING above zero), and a guard that the count is never negative (the count query comments that it counts the whole catalogue on purpose); verify with tests (use `caplog`): full reload removes them and other catalogues keep theirs, partial reload keeps other resources' proposals, pruned resource loses its proposals, zero case logs the INFO line with 0, failed load keeps proposals and logs nothing

## 4. Documentation and checks

- [x] 4.1 Update the `catalogue_db.py` module docstring (new table, cascade, reload deletes proposals) and verify by reading it against the "Module documentation" requirement
- [x] 4.2 Run `uv run ruff check src/ tests/`, `uv run ruff format src/ tests/` and `uv run pytest`; verify all pass and `openspec validate proposed-property-value-table --strict` still passes
