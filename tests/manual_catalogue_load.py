"""Manual catalogue loader — NOT an automated test (no pytest test functions live here).

Calls `catalogue_db.load_catalogue` on the files already fetched under
`fetched_catalogues_data/{ENV}/{catalogue}/` and shows, step by step, what is in the SQLite
database before and after. It only runs the feature; it does not copy, edit or fake any data.

Choose what to load with the settings below, or on the command line (which wins):
  --catalogue NAME       one catalogue            (default: CATALOGUES; `all` = every folder)
  --kind KIND            dataset | dataservice | both
  --no-pause             run straight through

  uv run python tests/manual_catalogue_load.py                          # settings below
  uv run python tests/manual_catalogue_load.py --catalogue all --kind dataservice
  uv run python tests/manual_catalogue_load.py --catalogue data-gov-lt --kind dataset

`both` is a normal full load (it also deletes resources whose file is gone). `dataset` or
`dataservice` loads only that kind by passing the ids of those files as `resource_ids`, so
nothing is pruned and the other kind is left as it is in the database.

The database is `fetched_catalogues_data/mqa.sqlite` (derived data, git-ignored, always
rebuildable from the fetched files), unless you set `DATABASE` below. Fetch the catalogue first
(`tests/manual_ingestion.py`) if its folder does not exist yet.

Things to try by changing the settings or the files, then running the script again:
  - run it twice                              -> same counts (each resource is replaced)
  - edit a `dataset_*.jsonld` file, run again -> only that Dataset's rows change
  - delete a `dataset_*.jsonld`, run again    -> `pruned = 1` (kind `both` only)
  - delete a `dataservice_*.json`, run again  -> `pruned = 1` (kind `both` only)
  - set RESOURCE_IDS = ["<id>"]               -> only that Dataset or Data Service is reloaded,
                                                 nothing pruned (overrides KIND)
  - set RESOURCE_IDS = ["no-such-id"]         -> an error, and the database is left untouched

With `STEP_BY_STEP` on, it stops after each step and waits for Enter; set it to False, or pass
`--no-pause`, to run straight through.
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from mqa_advisor.catalogue_db import DB_PATH, connect, load_catalogue
from mqa_advisor.ingest import FETCHED_DATA_DIR

REPO_ROOT = Path(__file__).resolve().parents[1]

# 🚨 USER: the environment and the catalogues whose fetched files you want to load.
# CATALOGUES = ["nsip-lu"], ["nsip-lu", "data-gov-lt"], or None = every folder under the env.
ENV = "PROD"
CATALOGUES: list[str] | None = ["nsip-lu"]

# "both", "dataset" or "dataservice": which kind of fetched file to load.
KIND = "both"

# None = follow KIND. A list of resource ids (a Dataset's file name without `dataset_` and
# `.jsonld`, or a Data Service's `id`) = reload only those, delete nothing else.
RESOURCE_IDS: list[str] | None = None

# Where to read the fetched files from and which database file to write.
DATA_DIR = FETCHED_DATA_DIR
DATABASE = DB_PATH

# True = wait for Enter after each step; False = run everything without stopping.
STEP_BY_STEP = True

# Print the loader's warnings (one per entity with a missing or multi-valued @type).
SHOW_WARNINGS = True


def pause():
    global STEP_BY_STEP
    if STEP_BY_STEP and sys.stdin.isatty():
        try:
            input("\n   -- press Enter to continue (Ctrl+C to quit) -- ")
        except EOFError:  # no usable stdin (piped/redirected): stop pausing
            STEP_BY_STEP = False


def step(number, title):
    print(f"\n{'=' * 90}\nSTEP {number}: {title}\n{'=' * 90}")


def show(conn, sql, params=()):
    cur = conn.execute(sql, params)
    headers = [d[0] for d in cur.description]
    rows = [["" if v is None else str(v) for v in r] for r in cur.fetchall()]
    if not rows:
        print("   (no rows)")
        return
    widths = [
        min(70, max(len(h), *(len(r[i]) for r in rows))) for i, h in enumerate(headers)
    ]
    print("   " + "  ".join(f"{h:<{w}}" for h, w in zip(headers, widths, strict=True)))
    print("   " + "  ".join("-" * w for w in widths))
    for r in rows:
        cells = [
            v if len(v) <= w else v[: w - 1] + "…"
            for v, w in zip(r, widths, strict=True)
        ]
        print(
            "   " + "  ".join(f"{c:<{w}}" for c, w in zip(cells, widths, strict=True))
        )


def database_state(conn):
    show(
        conn,
        "SELECT r.environment AS env, r.catalogue, r.resource_type AS type,"
        " COUNT(DISTINCT r.resource_pk) AS resources, COUNT(p.value) AS property_rows"
        " FROM resource r LEFT JOIN property_value p USING (resource_pk)"
        " GROUP BY 1, 2, 3 ORDER BY 1, 2, 3",
    )


def resource_ids_for(folder, kind):
    """Ids of the fetched files of one kind (None = no restriction, i.e. a full load)."""
    if kind == "both":
        return None
    if kind == "dataset":
        return [
            p.name.removeprefix("dataset_").removesuffix(".jsonld")
            for p in sorted(folder.glob("dataset_*.jsonld"))
        ]
    return [
        p.name.removeprefix("dataservice_").removesuffix(".json")
        for p in sorted(folder.glob("dataservice_*.json"))
    ]


def load_one(conn, catalogue, kind):
    folder = Path(DATA_DIR) / ENV / catalogue
    step(1, f"What is on disk: {ENV}/{catalogue}")
    print(f"   folder: {folder.resolve()}")
    if folder.is_dir():
        print(
            f"   dataset_*.jsonld files:    {len(list(folder.glob('dataset_*.jsonld')))}"
        )
        print(
            f"   dataservice_*.json files:  {len(list(folder.glob('dataservice_*.json')))}"
        )
    else:
        print(
            "   (the folder does not exist: a full load will raise FileNotFoundError)"
        )
    print(f"   database: {Path(DATABASE).resolve()}")
    ids = RESOURCE_IDS
    if ids is None and folder.is_dir():
        ids = resource_ids_for(folder, kind)
    mode = (
        "full load (prunes)"
        if ids is None
        else f"refresh {len(ids)} resource(s), no prune"
    )
    print(f"   KIND = {kind!r}, RESOURCE_IDS = {RESOURCE_IDS!r}  ->  {mode}")
    pause()

    step(2, "Database before the load")
    database_state(conn)
    pause()

    step(3, "load_catalogue")
    shown = ids if ids is None or len(ids) <= 3 else f"[{len(ids)} ids]"
    print(f"   >>> load_catalogue({ENV!r}, {catalogue!r}, resource_ids={shown!r}, ...)")
    started = time.perf_counter()
    summary = load_catalogue(
        ENV, catalogue, resource_ids=ids, conn=conn, data_dir=DATA_DIR
    )
    print(f"\n   {summary}   ({time.perf_counter() - started:.1f}s)")
    print(
        "   resources = Dataset and Data Service files loaded, pruned = resources deleted because\n"
        "   their file is gone, rows = property_value rows inserted, null_type_entities = entities\n"
        "   with a missing or multi-valued @type (stored with entity_type NULL)."
    )
    pause()

    step(4, "Database after the load")
    database_state(conn)
    pause()

    step(5, "Look inside one loaded resource of each kind")
    for rtype, root_type in (
        ("dataset", "http://www.w3.org/ns/dcat#Dataset"),
        ("dataservice", "http://www.w3.org/ns/dcat#DataService"),
    ):
        sample = conn.execute(
            "SELECT resource_pk, resource_id FROM resource WHERE environment = ?"
            " AND catalogue = ? AND resource_type = ? ORDER BY resource_id LIMIT 1",
            (ENV, catalogue, rtype),
        ).fetchone()
        if sample is None:
            print(f"\n   (no {rtype} loaded for this catalogue)")
            continue
        pk, resource_id = sample
        print(f"\n   [{rtype}] resource_id = {resource_id}")
        print("   Nodes stored for it (entity_type NULL = no single @type):")
        show(
            conn,
            "SELECT entity_type, COUNT(DISTINCT entity_id) AS entities, COUNT(*) AS rows"
            " FROM property_value WHERE resource_pk = ? GROUP BY 1 ORDER BY rows DESC",
            (pk,),
        )
        print("\n   A few of its root-node rows (full property URIs):")
        show(
            conn,
            "SELECT property_uri, value, value_kind, datatype, lang FROM property_value"
            " WHERE resource_pk = ? AND entity_type = ? LIMIT 10",
            (pk, root_type),
        )


if __name__ == "__main__":
    os.chdir(REPO_ROOT)  # the default paths are relative to the repo root
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--catalogue", help="catalogue name, or `all`")
    parser.add_argument("--kind", choices=["dataset", "dataservice", "both"])
    parser.add_argument("--no-pause", action="store_true")
    args = parser.parse_args()
    if args.no_pause:
        STEP_BY_STEP = False
    kind = args.kind or KIND
    if args.catalogue:
        CATALOGUES = None if args.catalogue == "all" else [args.catalogue]
    sys.stdout.reconfigure(
        encoding="utf-8", errors="replace"
    )  # non-ASCII text in the data
    if SHOW_WARNINGS:
        logging.basicConfig(
            level=logging.WARNING,
            format="   [loader warning] %(message)s",
            stream=sys.stdout,
        )

    env_dir = Path(DATA_DIR) / ENV
    catalogues = CATALOGUES
    if catalogues is None:
        catalogues = (
            sorted(p.name for p in env_dir.iterdir() if p.is_dir())
            if env_dir.is_dir()
            else []
        )
        print(f"All catalogues under {env_dir.resolve()}: {catalogues}")

    conn = connect(DATABASE)
    try:
        for catalogue in catalogues:
            load_one(conn, catalogue, kind)
    finally:
        conn.close()
    print("\nDone.")
