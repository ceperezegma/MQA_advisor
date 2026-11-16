## Why

The theme/keyword proposals feature (ADR-0017, `docs/design/theme-keyword-proposals.md`) needs
somewhere to keep what the LLM proposes. Proposals must live in the database but never outlive the
metadata they were made from, so the table has to follow the same mirror-the-folder rule as the
rest of `catalogue-database`. This is ticket 2/8 (#18); it has no dependency on the other tickets.

## What Changes

- Add the table `proposed_property_value` (design section 5) and its index
  `proposed_property_value_resource (resource_pk, property_uri)` to the schema created by `connect()`.
  Existing tables and `connect()` behavior are unchanged.
- Add a function that replaces all proposals of one resource in one transaction, and a function that
  reads them back. The replace function first checks the whole input (a `uri` row has no `lang`, a
  `literal` row has a `lang`) and raises `ValueError` before writing anything.
- `load_catalogue` removes the proposals of every resource it reloads or prunes (through the
  `ON DELETE CASCADE`) and always logs how many it removed (also zero); `LoadSummary` reports the count.
- The `catalogue_db.py` module docstring mentions the new table.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `catalogue-database`: adds the proposal table, its storage and read functions, and the rule that a
  reload removes the proposals of the resources it replaces.

## Impact

- `src/mqa_advisor/catalogue_db.py` only (schema, two functions, `LoadSummary`, `load_catalogue`
  logging, docstring) and tests in `tests/`.
- No new dependency. No change for existing databases: the table is created on the next `connect()`.
- Nothing writes proposals yet; the `propose` step arrives in a later ticket (7/8).
