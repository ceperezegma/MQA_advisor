# MQA Advisor

Recommends how to improve the metadata quality of a [data.europa.eu](https://data.europa.eu)
catalogue, based on the MQA methodology's Properties.

It starts from the first half of the sibling project *MQA_test* (manifest generation, fetching
and the SQLite database with its views); the recommendation logic is built on top of the
database.

## Setup

    uv sync
    cp .env.example .env   # PPE credentials only needed when MQA_ENV=PPE

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
| `--steps a,b` | Steps to run, from `manifest,fetch,db` (default: all three) |
| `--force` | Re-fetch every resource instead of keeping the files already on disk |

Environment: set `MQA_ENV` to `PROD` (default, public, no login) or `PPE` (needs `PPE_USERNAME` and
`PPE_PASSWORD` in `.env`). It selects the endpoint and the `fetched_catalogues_data/{PPE|PROD}/` folder.

If a fetch fails, the run stops before the `db` step and exits with code 1. The `db` step always loads
the catalogue folder as it is, so resources whose file is gone lose their rows.

## Prompt versions

Prompts are immutable versioned files (ADR-0018): `src/mqa_advisor/prompts/{name}/v{N}.md`, with a TOML
frontmatter (`version`, `model`, `temperature`, `max_tokens`, `max_items`) and a `string.Template` body
(`$placeholder`, `$$` for a literal dollar). The output schema is found by the same `(name, version)` in
`proposal_schemas.py`. `ACTIVE` in `prompts.py` is the version each prompt uses today; callers should use
`load_prompt(name, active_version(name), values)` and never a literal version number.

To move a prompt to v2:

1. Add `src/mqa_advisor/prompts/{name}/v2.md` with `version = 2` in the frontmatter. Do not edit `v1.md`.
2. Register its schema in `_BUILDERS` in `proposal_schemas.py`: `("{name}", 2): ...`. Reuse the v1 builder
   if the output shape is unchanged, otherwise write a `_{name}_v2()` builder.
3. Set `"{name}": 2` in `ACTIVE` in `prompts.py`.
4. Update the tests that pin the old state: `test_active_versions` (`tests/test_prompts.py`),
   `test_schema_keys_are_the_registered_pairs` (`tests/test_proposal_schemas.py`), and the model/temperature
   assertions in `test_committed_prompt_loads_with_a_value_for_every_placeholder` if v2 changes them.
5. Run `uv run pytest` and `uv run ruff check src tests`.

## Test

    uv run pytest                  # unit tests
    uv run pytest -m integration   # needs network (and PPE credentials for PPE)
    uv run ruff check src tests
