"""Manual pipeline + database tool — NOT an automated test (no pytest test functions live here).

Tries `pipeline.run` on a real catalogue, with a switch per step, and then reads the database
back through the three views and prints:

  - the `LoadSummary` of the load step;
  - `catalogue_resources`: how many Datasets / Data Services the database holds for the
    catalogue, next to how many `dataset_*.jsonld` / `dataservice_*.json` files are on disk;
  - a sample Dataset's Distributions and embedded Data Services (`dataset_distributions`,
    `dataset_embedded_services`).

Switch each step ON (True) / OFF (False) below. A step that is OFF reuses the files already on
disk: e.g. with only STEP_DB (and SHOW_DATABASE) ON, the fetched files are loaded and read back
with no network at all.

Requires network access (and a real `.env` when MQA_ENV=PPE). Start with a small catalogue
(`nsip-lu`, 40 Datasets).

Usage: `uv run python tests/manual_pipeline_db.py` from anywhere (it changes to the repo root,
because the pipeline's default folders are relative to the working directory).
"""

import os
import sqlite3
from pathlib import Path

from mqa_advisor.catalogue_db import DB_PATH, connect
from mqa_advisor.pipeline import run

REPO_ROOT = Path(__file__).resolve().parents[1]

# 🚨 USER: the catalogue to run.
CATALOGUE = "nsip-lu"

# 🚨 USER: the pipeline steps (see the module docstring).
STEP_MANIFEST = False  # regenerate catalogues_to_fetch/{CATALOGUE}.xlsx from SPARQL
STEP_FETCH = (
    False  # download the resources into fetched_catalogues_data/{ENV}/{CATALOGUE}
)
STEP_DB = (
    True  # load the fetched files into the database (creates the tables and views)
)
SHOW_DATABASE = True  # print what the database/views hold

# 🚨 USER: True to re-fetch every resource instead of keeping files already on disk.
FORCE = False

# Dataset whose Distributions are shown (resource_id as in the file name); None = the first one.
SAMPLE_DATASET_ID: str | None = None


def _show_database(env: str) -> None:
    if not DB_PATH.exists():
        print(f"  database {DB_PATH}: does not exist")
        return
    folder = REPO_ROOT / "fetched_catalogues_data" / env / CATALOGUE
    conn = connect(DB_PATH)
    try:
        counts = dict(
            conn.execute(
                "SELECT resource_type, COUNT(*) FROM catalogue_resources"
                " WHERE environment = ? AND catalogue = ? GROUP BY resource_type",
                (env, CATALOGUE),
            )
        )
        files = {
            "dataset": len(list(folder.glob("dataset_*.jsonld"))),
            "dataservice": len(list(folder.glob("dataservice_*.json"))),
        }
        print(f"  database {DB_PATH}")
        for kind in ("dataset", "dataservice"):
            print(
                f"    {kind:<12} in database: {counts.get(kind, 0):>5}"
                f"   files on disk: {files[kind]:>5}"
            )
        _show_sample_dataset(conn, env)
    finally:
        conn.close()


def _show_sample_dataset(conn: sqlite3.Connection, env: str) -> None:
    query = (
        "SELECT resource_pk, resource_id FROM catalogue_resources"
        " WHERE environment = ? AND catalogue = ? AND resource_type = 'dataset'"
    )
    params: tuple = (env, CATALOGUE)
    if SAMPLE_DATASET_ID is not None:
        query += " AND resource_id = ?"
        params += (SAMPLE_DATASET_ID,)
    row = conn.execute(query + " ORDER BY resource_id LIMIT 1", params).fetchone()
    if row is None:
        print("    sample Dataset: none found")
        return
    pk, resource_id = row
    distributions = [
        r[0]
        for r in conn.execute(
            "SELECT distribution_id FROM dataset_distributions WHERE resource_pk = ?",
            (pk,),
        )
    ]
    services = conn.execute(
        "SELECT distribution_id, service_id FROM dataset_embedded_services"
        " WHERE resource_pk = ?",
        (pk,),
    ).fetchall()
    print(f"    sample Dataset {resource_id}: {len(distributions)} Distribution(s)")
    for distribution_id in distributions[:5]:
        print(f"      {distribution_id}")
    print(f"    embedded Data Services: {len(services)}")
    for distribution_id, service_id in services[:5]:
        print(f"      {distribution_id} -> {service_id}")


def main() -> None:
    os.chdir(REPO_ROOT)
    steps = {
        step
        for step, on in (
            ("manifest", STEP_MANIFEST),
            ("fetch", STEP_FETCH),
            ("db", STEP_DB),
        )
        if on
    }
    print(f"Catalogue {CATALOGUE!r}, steps {sorted(steps) or 'none'}\n")
    outcome = run(CATALOGUE, steps=steps, force=FORCE)
    env = outcome.env
    print(f"== Pipeline ({env})")
    print(f"  manifest:     {outcome.manifest_path}")
    print(f"  fetch report: {outcome.fetch_report_path}")
    print(f"  load_summary: {outcome.load_summary}")
    if SHOW_DATABASE:
        print("\n== Database (read through the views)")
        _show_database(env)


if __name__ == "__main__":
    main()
