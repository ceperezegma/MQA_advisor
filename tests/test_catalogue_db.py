"""Tests for the SQLite schema and connection in `mqa_advisor.catalogue_db`."""

import sqlite3
from datetime import UTC, datetime

import pytest

from mqa_advisor.catalogue_db import connect


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "mqa.sqlite")
    yield connection
    connection.close()


def _add_resource(conn, resource_id="r1", **overrides):
    row = {
        "environment": "PROD",
        "catalogue": "nsip-lu",
        "resource_type": "dataset",
        "resource_id": resource_id,
        "source_file": f"dataset_{resource_id}.jsonld",
        "loaded_at": datetime(2026, 10, 27, 12, 0, tzinfo=UTC),
    }
    row.update(overrides)
    cur = conn.execute(
        "INSERT INTO resource (environment, catalogue, resource_type, resource_id,"
        " source_file, loaded_at) VALUES (:environment, :catalogue, :resource_type,"
        " :resource_id, :source_file, :loaded_at)",
        row,
    )
    return cur.lastrowid


def _add_value(conn, resource_pk, value_kind="literal"):
    conn.execute(
        "INSERT INTO property_value (resource_pk, entity_id, property_uri, value, value_kind)"
        " VALUES (?, 'e1', 'http://purl.org/dc/terms/title', 'x', ?)",
        (resource_pk, value_kind),
    )


def _names(conn, kind):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))
    return {r[0] for r in rows}


def test_creates_tables_and_indexes_on_fresh_path(tmp_path):
    path = tmp_path / "sub" / "mqa.sqlite"
    conn = connect(path)
    assert path.exists()
    assert {"resource", "property_value"} <= _names(conn, "table")
    assert {
        "property_value_entity",
        "property_value_property",
        "property_value_type",
    } <= _names(conn, "index")
    conn.close()


def test_connect_twice_changes_nothing(tmp_path):
    path = tmp_path / "mqa.sqlite"
    first = connect(path)
    pk = _add_resource(first)
    first.commit()
    first.close()

    second = connect(path)
    assert second.execute("SELECT resource_pk FROM resource").fetchall() == [(pk,)]
    second.close()


def test_foreign_keys_on_for_every_connection(tmp_path):
    path = tmp_path / "mqa.sqlite"
    for _ in range(2):
        conn = connect(path)
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
        conn.close()


def test_property_value_for_missing_resource_fails(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _add_value(conn, resource_pk=999)


def test_delete_resource_cascades_only_to_its_rows(conn):
    first = _add_resource(conn, "r1")
    second = _add_resource(conn, "r2")
    _add_value(conn, first)
    _add_value(conn, first, "uri")
    _add_value(conn, second)

    conn.execute("DELETE FROM resource WHERE resource_pk = ?", (first,))

    remaining = conn.execute(
        "SELECT DISTINCT resource_pk FROM property_value"
    ).fetchall()
    assert remaining == [(second,)]


def test_resource_type_outside_allowed_set_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _add_resource(conn, resource_type="distribution")


def test_value_kind_outside_allowed_set_rejected(conn):
    pk = _add_resource(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _add_value(conn, pk, "number")


def test_duplicate_resource_rejected(conn):
    _add_resource(conn, "r1")
    with pytest.raises(sqlite3.IntegrityError):
        _add_resource(conn, "r1")


def test_loaded_at_naive_datetime_rejected(conn):
    with pytest.raises(ValueError, match="timezone-aware"):
        _add_resource(conn, loaded_at=datetime(2026, 10, 27, 12, 0))


def test_connect_closes_connection_when_schema_fails(tmp_path):
    path = tmp_path / "mqa.sqlite"
    path.write_bytes(b"this is not a sqlite database" * 100)
    with pytest.raises(sqlite3.DatabaseError):
        connect(path)
    path.unlink()  # fails on Windows if the handle was leaked


def test_loaded_at_round_trips_as_aware_datetime(conn):
    stamp = datetime(2026, 10, 27, 12, 30, 15, tzinfo=UTC)
    _add_resource(conn, loaded_at=stamp)
    (stored,) = conn.execute("SELECT loaded_at FROM resource").fetchone()
    assert isinstance(stored, datetime)
    assert stored == stamp
    assert stored.utcoffset() == stamp.utcoffset()
