"""Tests for the SQL views created by `catalogue_db.connect()`.

Fixtures only (`tests/fixtures/ingested/`): no network, no credentials. Every test uses a
temporary database and a temporary `{env}/{catalogue}/` tree built from the fixtures. Expected
ids are read straight from the fixture files, not from the database.
"""

import json
import shutil
from pathlib import Path

import pytest

from mqa_advisor.catalogue_db import connect, load_catalogue

FIXTURES = Path(__file__).parent / "fixtures" / "ingested"
SERVICE_2916 = "https://data.gov.lt/datasets/2916/"

DATASET_1 = ("data-gov-lt", "dataset_https-data-gov-lt-datasets-1-.jsonld")
DATASET_2275 = ("data-gov-lt", "dataset_https-data-gov-lt-datasets-2275-.jsonld")
DATASET_2283 = ("data-gov-lt", "dataset_https-data-gov-lt-datasets-2283-.jsonld")
NO_DISTRIBUTIONS = ("nsip-lu", "dataset_16c923dc-7cf7-11f0-9925-46bf4a153a63.jsonld")
SERVICE_FILE = ("dataservices", "dataservice_https--data.gov.lt-datasets-2916-.json")


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "mqa.sqlite")
    yield connection
    connection.close()


@pytest.fixture
def directory(tmp_path):
    path = tmp_path / "data" / "PROD" / "cat"
    path.mkdir(parents=True)
    return path


def _load(conn, directory, *fixtures):
    for folder, name in fixtures:
        shutil.copy(FIXTURES / folder / name, directory / name)
    return load_catalogue("PROD", "cat", conn=conn, data_dir=directory.parents[1])


def _pk(conn, fixture):
    resource_id = fixture[1].removeprefix("dataset_").removesuffix(".jsonld")
    return conn.execute(
        "SELECT resource_pk FROM catalogue_resources WHERE resource_id = ?",
        (resource_id,),
    ).fetchone()[0]


def _as_list(value):
    return value if isinstance(value, list) else [value]


def _expected(fixture):
    """(distribution ids, (distribution id, service id) pairs) read from the JSON-LD file."""
    doc = json.loads((FIXTURES / fixture[0] / fixture[1]).read_text(encoding="utf-8"))
    distributions, services = [], []
    for node in doc["@graph"]:
        for ref in _as_list(node.get("dcat:distribution", [])):
            distributions.append(ref["@id"])
        for ref in _as_list(node.get("dcat:accessService", [])):
            services.append((node["@id"], ref["@id"]))
    return sorted(distributions), sorted(services)


def test_views_are_empty_before_anything_is_loaded(conn):
    for view in (
        "catalogue_resources",
        "dataset_distributions",
        "dataset_embedded_services",
    ):
        assert conn.execute(f"SELECT * FROM {view}").fetchall() == []


def test_catalogue_resources_lists_datasets_and_standalone_services_only(
    conn, directory
):
    _load(conn, directory, DATASET_2275, DATASET_1, SERVICE_FILE)

    rows = conn.execute(
        "SELECT environment, catalogue, resource_type, resource_id FROM catalogue_resources"
    ).fetchall()

    assert sorted(rows) == sorted(
        [
            ("PROD", "cat", "dataset", "https-data-gov-lt-datasets-2275-"),
            ("PROD", "cat", "dataset", "https-data-gov-lt-datasets-1-"),
            ("PROD", "cat", "dataservice", "https:-data.gov.lt-datasets-2916-"),
        ]
    )  # the embedded copy of service 2916 inside Dataset 2275 is not a resource


def test_catalogue_resources_includes_resource_pk(conn, directory):
    _load(conn, directory, DATASET_1)

    (pk,) = conn.execute("SELECT resource_pk FROM catalogue_resources").fetchone()

    assert pk == conn.execute("SELECT resource_pk FROM resource").fetchone()[0]


@pytest.mark.parametrize("fixture", [DATASET_2275, DATASET_1, DATASET_2283])
def test_dataset_distributions_match_the_source_file(conn, directory, fixture):
    _load(conn, directory, fixture)

    rows = conn.execute(
        "SELECT distribution_id FROM dataset_distributions WHERE resource_pk = ?",
        (_pk(conn, fixture),),
    ).fetchall()

    assert sorted(r[0] for r in rows) == _expected(fixture)[0]
    assert len(rows) >= 1


@pytest.mark.parametrize("fixture", [DATASET_2275, DATASET_2283])
def test_dataset_embedded_services_match_the_source_file(conn, directory, fixture):
    _load(conn, directory, fixture)

    rows = conn.execute(
        "SELECT distribution_id, service_id FROM dataset_embedded_services"
        " WHERE resource_pk = ?",
        (_pk(conn, fixture),),
    ).fetchall()

    assert sorted(rows) == _expected(fixture)[1]
    assert len(rows) >= 1


def test_dataset_without_distributions_has_no_rows_in_either_view(conn, directory):
    _load(conn, directory, NO_DISTRIBUTIONS, DATASET_2275)
    pk = _pk(conn, NO_DISTRIBUTIONS)

    for view in ("dataset_distributions", "dataset_embedded_services"):
        rows = conn.execute(
            f"SELECT * FROM {view} WHERE resource_pk = ?", (pk,)
        ).fetchall()
        assert rows == []


def test_dataset_with_distributions_but_no_service_has_no_embedded_rows(
    conn, directory
):
    _load(conn, directory, DATASET_1)

    assert conn.execute("SELECT * FROM dataset_embedded_services").fetchall() == []


def test_embedded_service_2916_comes_back_separately_for_each_dataset(conn, directory):
    _load(conn, directory, DATASET_2275, DATASET_2283)

    for fixture in (DATASET_2275, DATASET_2283):
        rows = conn.execute(
            "SELECT distribution_id FROM dataset_embedded_services"
            " WHERE resource_pk = ? AND service_id = ?",
            (_pk(conn, fixture), SERVICE_2916),
        ).fetchall()
        # one row per Distribution that embeds it, taken from that Dataset's own file
        expected = [d for d, s in _expected(fixture)[1] if s == SERVICE_2916]
        assert sorted(r[0] for r in rows) == sorted(expected)
        assert expected  # the fixture really embeds it

    owners = {
        pk
        for (pk,) in conn.execute(
            "SELECT resource_pk FROM dataset_embedded_services WHERE service_id = ?",
            (SERVICE_2916,),
        )
    }
    assert owners == {_pk(conn, DATASET_2275), _pk(conn, DATASET_2283)}


def test_views_do_not_list_a_standalone_service_as_embedded(conn, directory):
    _load(conn, directory, SERVICE_FILE)

    assert conn.execute("SELECT * FROM dataset_distributions").fetchall() == []
    assert conn.execute("SELECT * FROM dataset_embedded_services").fetchall() == []


def test_views_follow_a_reload(conn, directory):
    _load(conn, directory, DATASET_2275)
    pk = _pk(conn, DATASET_2275)
    (directory / DATASET_2275[1]).unlink()
    load_catalogue("PROD", "cat", conn=conn, data_dir=directory.parents[1])

    assert conn.execute("SELECT * FROM catalogue_resources").fetchall() == []
    assert (
        conn.execute(
            "SELECT * FROM dataset_distributions WHERE resource_pk = ?", (pk,)
        ).fetchall()
        == []
    )


def test_connect_twice_keeps_the_views(tmp_path):
    path = tmp_path / "mqa.sqlite"
    connect(path).close()
    conn = connect(path)
    try:
        views = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")
        }
    finally:
        conn.close()

    assert views == {
        "catalogue_resources",
        "dataset_distributions",
        "dataset_embedded_services",
    }


def test_literal_values_are_not_followed_as_references(conn, directory):
    """A `dcat:distribution` / `dcat:accessService` value that is a literal is not a node id,
    even when its text equals an entity id in the same resource."""
    _load(conn, directory, DATASET_2275)
    pk = _pk(conn, DATASET_2275)
    distribution = _expected(DATASET_2275)[0][0]
    before = (
        conn.execute("SELECT COUNT(*) FROM dataset_distributions").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM dataset_embedded_services").fetchone()[0],
    )
    dcat = "http://www.w3.org/ns/dcat#"
    for entity, kind, prop, value in (
        ("dataset-with-literal", "dataset", f"{dcat}distribution", distribution),
        (distribution, "distribution", f"{dcat}accessService", "not-a-reference"),
    ):
        conn.execute(
            "INSERT INTO property_value (resource_pk, entity_id, entity_kind,"
            " property_uri, value, value_kind) VALUES (?, ?, ?, ?, ?, 'literal')",
            (pk, entity, kind, prop, value),
        )

    after = (
        conn.execute("SELECT COUNT(*) FROM dataset_distributions").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM dataset_embedded_services").fetchone()[0],
    )
    assert after == before
