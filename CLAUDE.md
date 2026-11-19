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
steps with `--steps manifest,fetch,db,propose` (the default is the first three; a step not selected
reuses the files on disk) and add
`--force` to re-fetch. A failed fetch stops the run before the db step (`FetchIncompleteError`,
exit 1). The db step always loads the catalogue folder as it is: the database mirrors the folder, so
resources whose file is gone, or a folder with no resource files, remove their rows (no guard, no flag).

**Propose themes and keywords (opt-in):** `uv run python -m mqa_advisor.pipeline {catalogue} --steps propose`
(or `--steps db,propose`) calls the model for every resource of the catalogue already in the database, and
needs `ANTHROPIC_API_KEY` in `.env`. Options: `--limit N` (first N resources by id), `--dry-run` (no model call,
nothing written, no key needed; prints the number of calls and a rough cost estimate, and fails if it cannot build a prompt) and `--workers N` (default 4); they are
rejected unless `propose` is selected. Every resource is attempted (a failure of one never stops the others; only a broken prompt file does) and every
valid proposal saved; a failed call never deletes the earlier proposals of its property;
`propose_report.xlsx` (sheets Errors and Summary) is always written in the catalogue folder, and the step exits 1
if any resource had an error. A later `db` step deletes the proposals (the load logs how many). Use `--limit` and
`--dry-run` before a full run: a catalogue of about 2,300 Datasets is up to about 4,600 calls.

**Test:** `uv run pytest` runs the unit suite. Integration tests need network (and PPE credentials
when `MQA_ENV=PPE`); they are marked `integration`, excluded by default, and run with
`uv run pytest -m integration` after copying `.env.example` to `.env`. The prompt regression examples
(`tests/fixtures/prompts/`, `tests/test_prompt_regression_integration.py`) call the live model (about USD 0.003 a run, needs
`ANTHROPIC_API_KEY`, skipped without it): run them before a new prompt version becomes the active one.

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
  Only Datasets, Distributions and Data Services have rows; other graph nodes are folded into the
  `node` value of the property that reaches them (ADR-0015).
- `properties.py` — the Property catalog (`MQA_methodology/MQA-properties-for-app.xlsx`): URI, Importance, Weight.
- `prompts.py` + `prompts/` — versioned prompt files (frontmatter, a system part and a user part split by `---USER---`)
  and `proposal_schemas.py`, their output schemas (ADR-0018).
- `llm.py` — `LLMClient`: one forced-tool-call model call from a loaded prompt with schema validation, token usage,
  estimated cost (committed price table) and the per-run log `logs/propose/` (`RunLog`); `AnthropicBackend` (needs
  `ANTHROPIC_API_KEY` in `.env`) and `FakeBackend` for tests. No sampling parameter is ever sent (ADR-0019).
- `propose.py` — the theme proposal for one resource: `read_resource()` (the only db read; a Dataset's own entity is
  the one whose id starts with `http://data.europa.eu/88u/`), the language rule, code validation, and
  `propose_themes()` (up to 3 retries of an empty answer, returns a `ThemeResult`; writes nothing), and
  `propose_keywords()` (existing keywords of any language count toward the cap of 10, the effective themes are the
  context, stored language = the title's tag or the model's code; returns a `KeywordResult`; writes nothing).
- `propose_step.py` — the `propose` step: `run_propose()` reads the resources (main thread), runs the theme and
  keyword proposals in a thread pool, saves each result with `replace_proposals` from the main thread in resource-id
  order, writes `propose_report.xlsx` and raises `ProposeIncompleteError` after that if any `error` exists; `DryRunClient`
  stands in for the model on a dry run (ADR-0016, ADR-0019).
- `pipeline.py` — the manifest -> fetch -> db -> propose entry point with the step selector (`propose` is opt-in). Unlike MQA_test's Test Run,
  any failure of a selected step is fatal.

## Mandatory workflow

- The development workflow is global, not per project: `~/.claude/CLAUDE.md` makes it mandatory for every change and points to `~/.claude/skills/sw_development_methodology.md`. Don't copy it into this repo.

## Agent skills

### Issue tracker

Issues live in this repo's GitHub Issues (uses the `gh` CLI). See `docs/agents/issue-tracker.md`.

### Triage labels

Default five canonical labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
