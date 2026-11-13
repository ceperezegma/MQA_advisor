"""Manual catalogue-database tool — NOT an automated test (no pytest test functions live here).

Loads the fixtures under `tests/fixtures/ingested/` into a temporary SQLite database with
`catalogue_db.load_catalogue` and prints what the three views return:

  - resource and row counts per catalogue (`catalogue_resources`, `LoadSummary`);
  - the stored entities per `entity_kind` (only Datasets, Distributions and Data Services have
    rows; every other node of a file is folded into the `node` value of the property that
    reaches it);
  - the Distributions and embedded Data Services of a sample Dataset
    (`dataset_distributions`, `dataset_embedded_services`).

No network, no credentials. It never reads or writes `fetched_catalogues_data/`: the database
is a temporary file that is deleted when the script ends.

Usage: `uv run python tests/manual_catalogue_db.py` from anywhere. Change `CATALOGUES` or
`SAMPLE_DATASET` below to look at other fixtures.
"""

import shutil
import tempfile
from pathlib import Path

from mqa_advisor.catalogue_db import connect, load_catalogue

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "ingested"

ENV = "PROD"
# Fixture folders to load. `dataservices` only holds extra standalone Data Services; it is
# loaded into the `data-gov-lt` catalogue below so that embedded service 2916 has its
# standalone counterpart next to the two Datasets that embed it.
CATALOGUES = ["nsip-lu", "data-gov-lt"]
EXTRA_FOLDERS = {"data-gov-lt": ["dataservices"]}
# A Dataset with Distributions and an embedded Data Service (resource_id, file-name form).
SAMPLE_DATASET = ("data-gov-lt", "https-data-gov-lt-datasets-2275-")


def _copy_catalogue(data_dir: Path, catalogue: str) -> None:
    """Copy a fixture catalogue into `{data_dir}/{ENV}/{catalogue}/` (the layout the loader reads)."""
    target = data_dir / ENV / catalogue
    target.mkdir(parents=True)
    for folder in [catalogue, *EXTRA_FOLDERS.get(catalogue, [])]:
        for path in (FIXTURES / folder).iterdir():
            if path.is_file():
                shutil.copy(path, target / path.name)


def main() -> None:
    work_dir = Path(tempfile.mkdtemp(prefix="mqa_manual_catalogue_db_"))
    conn = connect(work_dir / "mqa.sqlite")
    try:
        data_dir = work_dir / "data"
        for catalogue in CATALOGUES:
            _copy_catalogue(data_dir, catalogue)
            summary = load_catalogue(ENV, catalogue, conn=conn, data_dir=data_dir)
            print(f"== {catalogue}: {summary}")
            for resource_type, count in conn.execute(
                "SELECT resource_type, COUNT(*) FROM catalogue_resources"
                " WHERE environment = ? AND catalogue = ? GROUP BY resource_type",
                (ENV, catalogue),
            ):
                print(f"   catalogue_resources: {count} {resource_type}(s)")

        print("\n== Stored entities per entity_kind")
        for kind, entities, rows in conn.execute(
            "SELECT entity_kind, COUNT(DISTINCT resource_pk || entity_id), COUNT(*)"
            " FROM property_value GROUP BY entity_kind ORDER BY entity_kind"
        ):
            print(f"   {kind}: {entities} entities, {rows} rows")
        (folded,) = conn.execute(
            "SELECT COUNT(*) FROM property_value WHERE value_kind = 'node'"
        ).fetchone()
        print(f"   auxiliary nodes folded into a property value: {folded}")

        catalogue, resource_id = SAMPLE_DATASET
        row = conn.execute(
            "SELECT resource_pk FROM catalogue_resources"
            " WHERE catalogue = ? AND resource_id = ?",
            (catalogue, resource_id),
        ).fetchone()
        print(f"\n== Sample Dataset {catalogue} / {resource_id}")
        if row is None:
            print("   not found in the loaded fixtures")
            return
        (pk,) = row
        print("   dataset_distributions:")
        for (distribution_id,) in conn.execute(
            "SELECT distribution_id FROM dataset_distributions WHERE resource_pk = ?",
            (pk,),
        ):
            print(f"     {distribution_id}")
        print("   dataset_embedded_services:")
        for distribution_id, service_id in conn.execute(
            "SELECT distribution_id, service_id FROM dataset_embedded_services"
            " WHERE resource_pk = ?",
            (pk,),
        ):
            print(f"     {distribution_id} -> {service_id}")
    finally:
        conn.close()
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
