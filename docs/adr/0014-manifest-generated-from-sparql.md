---
status: accepted
---

# The catalogue manifest is generated from the environment's SPARQL endpoint

Each `catalogues_to_fetch/{catalogue}.xlsx` used to be built by hand: run a SPARQL query in
Excel, paste the rows, let two Excel formulas derive `type` and `resource_id`. The files were
once deleted by accident, and a missing manifest blocks every Test Run. Issue #50 makes the app
produce the manifest itself.

## Findings (checked live)

- Production's `https://data.europa.eu/sparql` is public and, for `europol`, returns the same
  resources as the hand-made manifest.
- PPE serves `https://ppe.data.europa.eu/sparql` behind the same Cognito login as every other
  PPE path, so `PPEAuth.get()` reaches it (confirmed with real credentials by the opt-in
  integration test).
- `openpyxl` can store a formula or a value in a cell, not both, so a generated workbook with
  the old formulas would read as `None` under `data_only=True` until Excel re-saved it.

## Decision

- `manifest.generate_manifest(catalogue, env, session)` runs the packaged query
  (`sparql/catalogue_resources.rq`, `{catalogue}` placeholder, name validated before it is
  substituted) against `{session.base_url}sparql` and writes the manifest with plain values:
  `resource_uri`, `type_uri`, `type`, `resource_id`. The two Excel formulas became the pure
  functions `manifest_type` and `manifest_resource_id`.
- `test_run.run_catalogue` generates the manifest first, from the run's environment and with
  the run's session (one PPE login), overwriting any existing file. `--no-generate` /
  `generate_manifest=False` keeps a hand-made manifest as it is.
- A failure is fatal and has no fallback: an unreachable endpoint, an HTTP error, a response
  that is not SPARQL JSON, or zero resources raises `ManifestGenerationError`; `main` prints its
  message (catalogue, environment, endpoint, cause) to stderr and exits 1. The workbook is
  written only after the query succeeded, so a failure never touches an existing manifest and
  leaves no temp file.

## Consequences

- A Test Run needs the environment's SPARQL endpoint before anything is fetched; an outage there
  blocks the run (use `--no-generate` with an existing manifest).
- Hand-made manifests whose name is not a catalogue id (such as a hand-named `nsip-lu_app_validation`) are
  overwritten, or fail with zero resources, unless `--no-generate` is passed.
- The query is not paginated, as the Excel flow was not.
