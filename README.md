# MQA Advisor

Recommends how to improve the metadata quality of a [data.europa.eu](https://data.europa.eu)
catalogue, based on the MQA methodology's Properties.

It starts from the first half of the sibling project *MQA_test* (manifest generation, fetching
and the SQLite database with its views); the recommendation logic is built on top of the
database.

What exists so far:

- the pipeline that prepares a catalogue's database (manifest, fetch, db);
- LLM proposals of additional `dcat:theme` and `dcat:keyword` values for each Dataset and standalone Data
  Service, stored next to the catalogue data and reported in an Excel file (the opt-in `propose` step).

The wider recommendation logic is not written yet.

## Setup

    uv sync
    cp .env.example .env   # PPE credentials only needed when MQA_ENV=PPE;
                           # ANTHROPIC_API_KEY only needed for the propose step

## Prepare a catalogue's database

    uv run python -m mqa_advisor.pipeline nsip-lu                    # all steps
    uv run python -m mqa_advisor.pipeline nsip-lu --steps db         # reload the DB from fetched files
    uv run python -m mqa_advisor.pipeline nsip-lu --steps fetch,db --force

Steps: `manifest` (SPARQL -> `catalogues_to_fetch/{catalogue}.xlsx`), `fetch`
(-> `fetched_catalogues_data/{PPE|PROD}/{catalogue}/`), `db`
(-> `fetched_catalogues_data/mqa.sqlite`). A step that is not selected reuses the existing files.

Options:

| Option | Meaning |
|---|---|
| `catalogue` (required) | Catalogue name, e.g. `nsip-lu` |
| `--steps a,b` | Steps to run, from `manifest,fetch,db,propose` (default: `manifest,fetch,db`; `propose` is opt-in) |
| `--force` | Re-fetch every resource instead of keeping the files already on disk |
| `--limit N` | `propose` only: the first N resources by id |
| `--dry-run` | `propose` only: call no model, write nothing, print the number of calls and a rough cost estimate |
| `--workers N` | `propose` only: parallel model calls (default 4) |

Environment: set `MQA_ENV` to `PROD` (default, public, no login) or `PPE` (needs `PPE_USERNAME` and
`PPE_PASSWORD` in `.env`). It selects the endpoint and the `fetched_catalogues_data/{PPE|PROD}/` folder.

If a fetch fails, the run stops before the `db` step and exits with code 1. The `db` step always loads
the catalogue folder as it is, so resources whose file is gone lose their rows.

## Propose themes and keywords

    uv run python -m mqa_advisor.pipeline nsip-lu --steps propose --limit 20 --dry-run   # estimate first
    uv run python -m mqa_advisor.pipeline nsip-lu --steps propose --limit 20
    uv run python -m mqa_advisor.pipeline nsip-lu --steps db,propose --workers 8

For every resource of a catalogue already in the database, the model proposes new `dcat:theme` values (from
the EU `data-theme` vocabulary, validated by code; at most 5 in total) and then new `dcat:keyword` values (at
most 10 in total), using only the resource's title and description (ADR-0017). The proposals go to the
`proposed_property_value` table. Existing values are never changed or removed.

- Needs `ANTHROPIC_API_KEY` in `.env` (not for `--dry-run`). Before any step runs, the key, the vocabulary,
  the prompt files and (without the `db` step) the catalogue in the database are checked.
- Every resource is attempted and every valid proposal saved. A failure of one resource never stops the
  others, and a failed call never deletes the earlier proposals of its property; only a broken prompt file
  stops the run.
- `fetched_catalogues_data/{PPE|PROD}/{catalogue}/propose_report.xlsx` is written on every run (sheet
  *Errors*: one row per warning or error; sheet *Summary*: counts, tokens, estimated cost). If any resource
  had an error the step exits with code 1, after saving and reporting.
- Each run writes a per-call log with the exact prompts, answers, tokens and estimated cost to
  `logs/propose/` (git-ignored). Prices come from a committed table, so the cost is an estimate.
- The `db` step reloads the catalogue and so deletes its proposals (the load logs how many). A catalogue of
  about 2,300 Datasets is up to about 4,600 calls: use `--limit` and `--dry-run` before a full run.

## Prompt versions

Prompts are immutable versioned files (ADR-0018): `src/mqa_advisor/prompts/{name}/v{N}.md`, with a TOML
frontmatter (`version`, `model`, `max_tokens`, `max_items`; no `temperature`, current models reject sampling
parameters) and a `string.Template` body (`$placeholder`, `$$` for a literal dollar) split by one `---USER---`
line into a system part and a user part. The output schema is found by the same `(name, version)` in
`proposal_schemas.py`. `ACTIVE` in `prompts.py` is the version each prompt uses today; callers should use
`load_prompt(name, active_version(name), values)` and never a literal version number.

To move a prompt to v2:

1. Add `src/mqa_advisor/prompts/{name}/v2.md` with `version = 2` in the frontmatter. Do not edit `v1.md`.
2. Register its schema in `_BUILDERS` in `proposal_schemas.py`: `("{name}", 2): ...`. Reuse the v1 builder
   if the output shape is unchanged, otherwise write a `_{name}_v2()` builder.
3. Set `"{name}": 2` in `ACTIVE` in `prompts.py`.
4. Update the tests that pin the old state: `test_active_versions` (`tests/test_prompts.py`),
   `test_schema_keys_are_the_registered_pairs` (`tests/test_proposal_schemas.py`), and the model
   assertions in `test_committed_prompt_loads_with_a_value_for_every_placeholder` if v2 changes them.
5. Run `uv run pytest` and `uv run ruff check src tests`.

## Test

    uv run pytest                  # unit tests
    uv run pytest -m integration   # needs network (and PPE credentials for PPE)
    uv run ruff check src tests
