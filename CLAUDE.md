# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

MQA Advisor recommends how to improve the metadata quality of a data.europa.eu catalogue. It was
split from the sibling project *MQA_test* (which tests the MQA App's scoring): the manifest ->
fetch -> database front half was copied here, unchanged apart from the package name. The
recommendation logic is built on top of the database and is not written yet.

Package manager is `uv`. Source lives under `src/mqa_advisor/` (src layout, built with hatchling),
tests under `tests/`.

**Install:** `uv sync` (runtime deps `requests`, `python-dotenv`, `openpyxl`, plus the `dev` group:
`pytest`, `ruff`, `pre-commit`).

**Prepare a catalogue's database:** `uv run python -m mqa_advisor.pipeline {catalogue}`; pick the
steps with `--steps manifest,fetch,db` (a step not selected reuses the files on disk) and add
`--force` to re-fetch.

**Test:** `uv run pytest` runs the unit suite. Integration tests need network (and PPE credentials
when `MQA_ENV=PPE`); they are marked `integration`, excluded by default, and run with
`uv run pytest -m integration` after copying `.env.example` to `.env`.

**Lint/format:** `uv run ruff check src/ tests/` and `uv run ruff format src/ tests/`.

**Data folders** (all relative to the working directory, git-ignored): manifests in
`catalogues_to_fetch/{catalogue}.xlsx`, fetched files in `fetched_catalogues_data/{PPE|PROD}/{catalogue}/`,
the SQLite database in `fetched_catalogues_data/mqa.sqlite`. Test fixtures live under `tests/fixtures/`.

### Architecture notes

- `auth.py` — PPE login (`Config`, `PPEAuth`) and `PublicSession` for production (no login); see ADR-0007.
- `environment.py` — `MQA_ENV` (`PPE`/`PROD`, default `PROD`) drives every step and folder (ADR-0013).
- `manifest.py` — `generate_manifest()` writes the manifest from the environment's SPARQL endpoint (ADR-0014).
- `ingest.py` — `PPEIngestor` fetches the manifest's Datasets and Data Services (ADR-0003, ADR-0010).
- `catalogue_db.py` — SQLite schema, the views `catalogue_resources`, `dataset_distributions`,
  `dataset_embedded_services`, and `load_catalogue()`; `dataservice_mapping.py` maps standalone Data Services.
- `properties.py` — the Property catalog (`MQA_methodology/MQA-properties-for-app.xlsx`): URI, Importance, Weight.
- `pipeline.py` — the manifest -> fetch -> db entry point with the step selector. Unlike MQA_test's Test Run,
  any failure of a selected step is fatal.

## Mandatory workflow

- Always follow the workflow in `.claude/skills/sw_development_methodology.md` for every change — don't skip or reorder its steps except when the user explicitly says which steps to skip or where to start.

## Agent skills

### Issue tracker

Issues live in this repo's GitHub Issues (uses the `gh` CLI). See `docs/agents/issue-tracker.md`.

### Triage labels

Default five canonical labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
