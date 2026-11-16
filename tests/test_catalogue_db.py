"""Tests for the SQLite schema and connection in `mqa_advisor.catalogue_db`."""

import sqlite3
from datetime import UTC, datetime

import pytest

from mqa_advisor.catalogue_db import (
    ProposedValue,
    connect,
    read_proposals,
    replace_proposals,
)


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


def _add_value(conn, resource_pk, value_kind="literal", entity_kind="dataset"):
    conn.execute(
        "INSERT INTO property_value (resource_pk, entity_id, entity_kind, property_uri,"
        " value, value_kind) VALUES (?, 'e1', ?, 'http://purl.org/dc/terms/title', 'x', ?)",
        (resource_pk, entity_kind, value_kind),
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
        "property_value_kind",
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


@pytest.mark.parametrize("value_kind", ["literal", "uri", "bnode", "node"])
def test_every_value_kind_is_accepted(conn, value_kind):
    _add_value(conn, _add_resource(conn), value_kind)


@pytest.mark.parametrize("entity_kind", ["dataset", "distribution", "dataservice"])
def test_every_entity_kind_is_accepted(conn, entity_kind):
    _add_value(conn, _add_resource(conn), entity_kind=entity_kind)


@pytest.mark.parametrize("entity_kind", ["catalogue", "node", ""])
def test_entity_kind_outside_allowed_set_rejected(conn, entity_kind):
    pk = _add_resource(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _add_value(conn, pk, entity_kind=entity_kind)


def test_database_with_the_older_layout_is_refused_and_closed(tmp_path):
    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE property_value (resource_pk INTEGER, entity_id TEXT, entity_type TEXT)"
    )
    old.commit()
    old.close()
    with pytest.raises(RuntimeError, match="older layout"):
        connect(path)
    path.unlink()  # fails on Windows if the handle was leaked


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


# --- proposed_property_value -------------------------------------------------------------------

TIME = datetime(2026, 11, 15, 9, 30, tzinfo=UTC)


def _theme(**overrides):
    fields = {
        "entity_id": "http://example.org/d1",
        "entity_kind": "dataset",
        "property_uri": "http://www.w3.org/ns/dcat#theme",
        "value": "http://publications.europa.eu/resource/authority/data-theme/ENVI",
        "value_kind": "uri",
        "lang": None,
        "prompt_name": "propose_themes",
        "prompt_version": 1,
        "prompt_sha256": "abc",
        "model": "claude-haiku-5-5",
        "proposed_at": TIME,
    }
    fields.update(overrides)
    return ProposedValue(**fields)


def _keyword(text="river", **overrides):
    return _theme(
        property_uri="http://www.w3.org/ns/dcat#keyword",
        value=text,
        value_kind="literal",
        lang="eng",
        prompt_name="propose_keywords",
        **overrides,
    )


def _insert_raw(conn, resource_pk, **overrides):
    """Insert one proposal row directly, bypassing `replace_proposals`."""
    row = {"resource_pk": resource_pk, **_theme(**overrides).__dict__}
    conn.execute(
        "INSERT INTO proposed_property_value VALUES (:resource_pk, :entity_id,"
        " :entity_kind, :property_uri, :value, :value_kind, :lang, :prompt_name,"
        " :prompt_version, :prompt_sha256, :model, :proposed_at)",
        row,
    )


def test_creates_proposal_table_and_index_on_fresh_path(tmp_path):
    conn = connect(tmp_path / "mqa.sqlite")
    assert "proposed_property_value" in _names(conn, "table")
    assert "proposed_property_value_resource" in _names(conn, "index")
    conn.close()


def test_proposal_table_is_added_to_a_database_that_has_the_other_tables(tmp_path):
    path = tmp_path / "mqa.sqlite"
    conn = connect(path)
    pk = _add_resource(conn)
    _add_value(conn, pk)
    conn.execute("DROP TABLE proposed_property_value")
    conn.commit()
    conn.close()
    conn = connect(path)
    assert "proposed_property_value" in _names(conn, "table")
    assert conn.execute("SELECT COUNT(*) FROM property_value").fetchone()[0] == 1
    conn.close()


def test_connect_again_keeps_the_proposals(tmp_path):
    path = tmp_path / "mqa.sqlite"
    conn = connect(path)
    pk = _add_resource(conn)
    replace_proposals(conn, pk, [_theme()])
    conn.close()
    conn = connect(path)
    assert read_proposals(conn, pk) == [_theme()]
    conn.close()


@pytest.mark.parametrize(
    "overrides", [{"entity_kind": "distribution"}, {"value_kind": "node"}]
)
def test_proposal_check_constraints_reject_bad_kinds(conn, overrides):
    pk = _add_resource(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw(conn, pk, **overrides)


def test_proposal_for_missing_resource_fails(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw(conn, 999)


def test_replace_proposals_stores_the_given_rows(conn):
    pk = _add_resource(conn)
    replace_proposals(conn, pk, [_theme(), _keyword("river"), _keyword("water")])
    # keywords sort before the theme (property URI order)
    assert read_proposals(conn, pk) == [_keyword("river"), _keyword("water"), _theme()]


def test_replace_proposals_twice_with_the_same_input_gives_the_same_rows(conn):
    pk = _add_resource(conn)
    proposals = [_theme(), _keyword()]
    replace_proposals(conn, pk, proposals)
    first = read_proposals(conn, pk)
    replace_proposals(conn, pk, proposals)
    assert read_proposals(conn, pk) == first


def test_replace_proposals_leaves_only_the_second_set(conn):
    pk = _add_resource(conn)
    replace_proposals(conn, pk, [_theme(), _keyword("old")])
    replace_proposals(conn, pk, [_keyword("new")])
    assert read_proposals(conn, pk) == [_keyword("new")]


def test_replace_proposals_with_an_empty_list_clears_the_resource(conn):
    pk = _add_resource(conn)
    replace_proposals(conn, pk, [_theme()])
    replace_proposals(conn, pk, [])
    assert read_proposals(conn, pk) == []


def test_replace_proposals_leaves_other_resources_alone(conn):
    pk1, pk2 = _add_resource(conn, "r1"), _add_resource(conn, "r2")
    replace_proposals(conn, pk2, [_keyword("other")])
    replace_proposals(conn, pk1, [_theme()])
    assert read_proposals(conn, pk2) == [_keyword("other")]


def test_replace_proposals_failure_keeps_the_previous_rows(conn):
    pk = _add_resource(conn)
    replace_proposals(conn, pk, [_theme()])
    with pytest.raises(sqlite3.IntegrityError):
        replace_proposals(
            conn, pk, [_keyword("ok"), _keyword(entity_kind="distribution")]
        )
    assert read_proposals(conn, pk) == [_theme()]


def test_replace_proposals_naive_time_raises_and_keeps_the_previous_rows(conn):
    pk = _add_resource(conn)
    replace_proposals(conn, pk, [_theme()])
    with pytest.raises(ValueError):
        replace_proposals(conn, pk, [_keyword(proposed_at=datetime(2026, 11, 15))])
    assert read_proposals(conn, pk) == [_theme()]


def test_read_proposals_round_trips_lang_and_aware_time(conn):
    pk = _add_resource(conn)
    replace_proposals(conn, pk, [_theme(), _keyword()])
    keyword, theme = read_proposals(conn, pk)
    assert theme.lang is None and keyword.lang == "eng"
    assert keyword.proposed_at == TIME and keyword.proposed_at.tzinfo is not None


def test_read_proposals_is_ordered_by_property_then_insertion(conn):
    pk = _add_resource(conn)
    proposals = [_keyword("b"), _theme(), _keyword("a")]
    replace_proposals(conn, pk, proposals)
    # dcat:keyword sorts before dcat:theme; within a property, insertion order
    assert read_proposals(conn, pk) == [_keyword("b"), _keyword("a"), _theme()]


def test_read_proposals_of_a_resource_without_proposals_is_empty(conn):
    assert read_proposals(conn, _add_resource(conn)) == []
