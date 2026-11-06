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

## Test

    uv run pytest                  # unit tests
    uv run pytest -m integration   # needs network (and PPE credentials for PPE)
    uv run ruff check src tests
