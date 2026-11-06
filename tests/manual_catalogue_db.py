"""Manual catalogue-database tool — NOT an automated test (no pytest test functions live here).

Loads the fixtures under `tests/fixtures/ingested/` into a temporary SQLite database with
`catalogue_db.load_catalogue` and prints what the three views return:

  - resource and row counts per catalogue (`catalogue_resources`, `LoadSummary`);
  - the entities stored with a `NULL` `entity_type` (the multi-typed nsip-lu agent/contact
    nodes); no Dataset, Distribution or Data Service should be among them;
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
RDF_TYPE_DCAT = "http://www.w3.org/ns/dcat#"


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

        print("\n== Entities with a NULL entity_type")
        null_rows = conn.execute(
            "SELECT r.catalogue, r.resource_id, pv.entity_id,"
            " group_concat(DISTINCT t.value)"
            " FROM property_value pv"
            " JOIN resource r ON r.resource_pk = pv.resource_pk"
            " LEFT JOIN property_value t ON t.resource_pk = pv.resource_pk"
            "   AND t.entity_id = pv.entity_id"
            "   AND t.property_uri = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#type'"
            " WHERE pv.entity_type IS NULL"
            " GROUP BY r.catalogue, r.resource_id, pv.entity_id"
        ).fetchall()
        for catalogue, resource_id, entity_id, types in null_rows[:15]:
            print(f"   {catalogue} / {resource_id}: {entity_id}  types={types}")
        print(f"   total: {len(null_rows)}")
        wrongly_untyped = [
            row for row in null_rows if row[3] and RDF_TYPE_DCAT in row[3]
        ]
        print(
            "   Dataset/Distribution/Data Service among them: "
            f"{len(wrongly_untyped)} (expected 0)"
        )

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
