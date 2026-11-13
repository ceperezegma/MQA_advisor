"""Tests for loading standalone Data Service JSON files (`dataservice_mapping`, `catalogue_db`).

Fixtures only (`tests/fixtures/ingested/`): no network, no credentials. Every test uses a
temporary database and a temporary `{env}/{catalogue}/` tree built from the fixtures.
"""

import copy
import json
import shutil
from pathlib import Path

import pytest

from mqa_advisor import dataservice_mapping as dsm
from mqa_advisor.catalogue_db import connect, load_catalogue

FIXTURES = Path(__file__).parent / "fixtures" / "ingested"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
DCT = "http://purl.org/dc/terms/"
DCAT = "http://www.w3.org/ns/dcat#"
FOAF = "http://xmlns.com/foaf/0.1/"
VCARD = "http://www.w3.org/2006/vcard/ns#"
DCATAP = "http://data.europa.eu/r5r/"

NSIP_SERVICE = ("nsip-lu", "dataservice_https-letzdata-public-lu-data-services-1.json")
SERVICE_2916 = ("dataservices", "dataservice_https--data.gov.lt-datasets-2916-.json")
SERVICE_3499 = ("dataservices", "dataservice_https-data-gov-lt-datasets-3499-.json")
SERVICE_1254 = ("data-gov-lt", "dataservice_https-data-gov-lt-datasets-1254-.json")
SERVICE_4352 = ("data-gov-lt", "dataservice_https-data-gov-lt-datasets-4352-.json")
ALL_SERVICES = [NSIP_SERVICE, SERVICE_2916, SERVICE_3499, SERVICE_1254, SERVICE_4352]
DATASET_2275 = ("data-gov-lt", "dataset_https-data-gov-lt-datasets-2275-.jsonld")


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


def _copy(directory, *fixtures):
    for folder, name in fixtures:
        shutil.copy(FIXTURES / folder / name, directory / name)


def _load(conn, directory, **kwargs):
    return load_catalogue(
        "PROD", "cat", conn=conn, data_dir=directory.parents[1], **kwargs
    )


def _fixture_record(fixture):
    folder, name = fixture
    return json.loads((FIXTURES / folder / name).read_text(encoding="utf-8"))


def _write_service(directory, record, name=None):
    doc = {"status": "success", "result": record}
    path = directory / (name or f"dataservice_{record['id']}.json")
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _rows(conn, resource_id, prop=None):
    sql = (
        "SELECT p.entity_id, p.entity_kind, p.property_uri, p.value, p.value_kind, p.lang"
        " FROM property_value p JOIN resource r USING (resource_pk)"
        " WHERE r.resource_id = ? AND r.resource_type = 'dataservice'"
    )
    params = [resource_id]
    if prop:
        sql += " AND p.property_uri = ?"
        params.append(prop)
    return sorted(conn.execute(sql, params).fetchall(), key=repr)


def _counts(conn):
    return tuple(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("resource", "property_value")
    )


# --- mapping module ---------------------------------------------------------------------


def test_undefined_prefix_fails():
    with pytest.raises(ValueError, match="nope"):
        dsm.expand("nope:thing")


def test_expand_uses_full_uri():
    assert dsm.expand("dcatap:hvdCategory") == "http://data.europa.eu/r5r/hvdCategory"


def test_every_fixture_key_is_mapped_or_ignored():
    known = dsm.MAPPED_KEYS | dsm.IGNORED_KEYS
    for fixture in ALL_SERVICES:
        assert set(_fixture_record(fixture)["result"]) <= known, fixture


def test_mapped_and_ignored_keys_do_not_overlap():
    assert not dsm.MAPPED_KEYS & dsm.IGNORED_KEYS


def test_fixtures_cover_every_mapping_branch():
    seen = set()
    for fixture in ALL_SERVICES:
        seen |= set(_fixture_record(fixture)["result"])
    assert dsm.MAPPED_KEYS <= seen


# --- loading one record -----------------------------------------------------------------


def test_round_trip_keywords_publisher_contact_and_license(conn, directory):
    _copy(directory, SERVICE_1254, NSIP_SERVICE)
    _load(conn, directory)
    record = _fixture_record(SERVICE_1254)["result"]
    rid = record["id"]
    assert conn.execute(
        "SELECT resource_type, source_file FROM resource WHERE resource_id = ?", (rid,)
    ).fetchone() == ("dataservice", SERVICE_1254[1])

    keywords = _rows(conn, rid, DCAT + "keyword")
    assert [(r[3], r[4], r[5]) for r in keywords] == sorted(
        [(k["label"], "literal", k["language"]) for k in record["keywords"]], key=repr
    )

    publisher = _rows(conn, rid, DCT + "publisher")
    assert len(publisher) == 1
    assert publisher[0][:2] == (rid, "dataservice")
    assert publisher[0][4] == "node"
    assert json.loads(publisher[0][3]) == {
        "@type": [FOAF + "Organization"],
        FOAF + "name": [{"@value": record["publisher"]["name"]}],
        FOAF + "mbox": [{"@id": record["publisher"]["email"]}],
        FOAF + "homepage": [{"@id": record["publisher"]["homepage"]}],
    }

    contact = _rows(conn, rid, DCAT + "contactPoint")
    assert len(contact) == 1
    assert contact[0][4] == "node"
    assert json.loads(contact[0][3]) == {
        "@type": [VCARD + "Kind"],
        VCARD + "hasEmail": [{"@id": record["contact_point"][0]["email"]}],
    }

    nsip = _fixture_record(NSIP_SERVICE)["result"]
    assert [r[3] for r in _rows(conn, nsip["id"], DCT + "license")] == [
        nsip["license"]["resource"]
    ]


def test_root_entity_and_id_is_not_a_property(conn, directory):
    _copy(directory, NSIP_SERVICE)
    _load(conn, directory)
    rid = _fixture_record(NSIP_SERVICE)["result"]["id"]
    root_rows = _rows(conn, rid)
    assert {r[0] for r in root_rows} == {rid}  # no other entity has rows
    assert {r[1] for r in root_rows} == {"dataservice"}
    assert (rid, "dataservice", RDF_TYPE, DCAT + "DataService", "uri", None) in (
        root_rows
    )
    assert not any(r[3] == rid and r[2] != RDF_TYPE for r in root_rows)
    assert not any(r[2].endswith("id") for r in root_rows)


def test_mapped_resource_and_list_keys_are_full_uri_rows(conn, directory):
    _copy(directory, SERVICE_1254, SERVICE_4352, SERVICE_3499, SERVICE_2916)
    _load(conn, directory)
    full = _fixture_record(SERVICE_1254)["result"]
    rid = full["id"]
    assert [r[3] for r in _rows(conn, rid, DCT + "accessRights")] == [
        full["access_right"]["resource"]
    ]
    assert sorted(r[3] for r in _rows(conn, rid, DCAT + "theme")) == sorted(
        t["resource"] for t in full["theme"]
    )
    assert [r[3] for r in _rows(conn, rid, DCATAP + "hvdCategory")] == [
        full["hvd_category"][0]["resource"]
    ]
    assert [r[3] for r in _rows(conn, rid, DCATAP + "applicableLegislation")] == (
        full["applicable_legislation"]
    )
    landing = _fixture_record(SERVICE_4352)["result"]
    assert [r[3] for r in _rows(conn, landing["id"], DCAT + "landingPage")] == [
        landing["landing_page"][0]["resource"]
    ]
    served = _fixture_record(SERVICE_2916)["result"]
    assert sorted(
        r[3] for r in _rows(conn, served["id"], DCAT + "servesDataset")
    ) == sorted(served["serves_dataset"])
    assert [r[3] for r in _rows(conn, served["id"], DCAT + "endpointDescription")] == (
        served["endpoint_description"]
    )
    assert [r[3] for r in _rows(conn, full["id"], DCAT + "endpointURL")] == (
        full["endpoint_url"]
    )


def test_ignored_keys_create_no_rows(conn, directory):
    record = _fixture_record(NSIP_SERVICE)["result"]
    record["count"] = 3
    record["results"] = [{"anything": 1}]
    _write_service(directory, record)
    _load(conn, directory)
    assert {r[2] for r in _rows(conn, record["id"])} <= {
        RDF_TYPE,
        DCT + "title",
        DCT + "accessRights",
        DCT + "publisher",
        DCT + "license",
        DCAT + "endpointURL",
    }


@pytest.mark.parametrize(
    ("fixture", "langs"),
    [(NSIP_SERVICE, {"eng"}), (SERVICE_2916, {"es", "sl", "lt", "it"})],
)
def test_language_is_kept_as_delivered(conn, directory, fixture, langs):
    _copy(directory, fixture)
    _load(conn, directory)
    record = _fixture_record(fixture)["result"]
    stored = {r[5] for r in _rows(conn, record["id"], DCT + "title")}
    assert stored == set(record["title"])
    assert langs <= stored


def test_two_contact_points_become_two_node_values(conn, directory):
    record = _fixture_record(NSIP_SERVICE)["result"]
    record["contact_point"] = [
        {"type": "Kind", "email": "mailto:a@x.org"},
        {"type": "Kind", "email": "mailto:b@x.org"},
    ]
    _write_service(directory, record)
    _load(conn, directory)
    rows = _rows(conn, record["id"], DCAT + "contactPoint")
    assert {r[4] for r in rows} == {"node"}
    emails = {json.loads(r[3])[VCARD + "hasEmail"][0]["@id"] for r in rows}
    assert emails == {"mailto:a@x.org", "mailto:b@x.org"}


def test_agent_publisher_type(conn, directory):
    _copy(directory, NSIP_SERVICE)
    _load(conn, directory)
    rid = _fixture_record(NSIP_SERVICE)["result"]["id"]
    (publisher,) = _rows(conn, rid, DCT + "publisher")
    assert FOAF + "Agent" in json.loads(publisher[3])["@type"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(new_field=1),
        lambda r: r["publisher"].update(type="Robot"),
        lambda r: r.update(title=["not", "a", "map"]),
        lambda r: r.update(endpoint_url="not-a-list"),
        lambda r: r.update(access_right={"label": "no resource key"}),
        lambda r: r["publisher"].update(phone="123"),
        lambda r: r.pop("id"),
    ],
)
def test_bad_record_raises_naming_the_file(conn, directory, mutate):
    record = copy.deepcopy(_fixture_record(NSIP_SERVICE)["result"])
    mutate(record)
    path = _write_service(directory, record, name="dataservice_bad.json")
    with pytest.raises(ValueError, match=path.name):
        _load(conn, directory)
    assert _counts(conn) == (0, 0)


def test_unknown_key_error_names_the_key(conn, directory):
    record = _fixture_record(NSIP_SERVICE)["result"]
    record["new_field"] = 1
    _write_service(directory, record, name="dataservice_bad.json")
    with pytest.raises(ValueError, match="new_field"):
        _load(conn, directory)


@pytest.mark.parametrize("content", ["{ not json", "[]", '{"status": "success"}'])
def test_malformed_file_raises_naming_the_file(conn, directory, content):
    (directory / "dataservice_bad.json").write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="dataservice_bad.json"):
        _load(conn, directory)


# --- mixed directories, replace, prune, resource_ids ------------------------------------


def test_mixed_directory_loads_both_types_in_one_load(conn, directory):
    _copy(directory, DATASET_2275, SERVICE_2916, SERVICE_3499)
    summary = _load(conn, directory)
    types = conn.execute(
        "SELECT resource_type, COUNT(*) FROM resource GROUP BY resource_type"
    ).fetchall()
    assert sorted(types) == [("dataservice", 2), ("dataset", 1)]
    assert summary.resources == 3
    assert (
        summary.rows
        == conn.execute("SELECT COUNT(*) FROM property_value").fetchone()[0]
    )
    ids = {
        r[0]
        for r in conn.execute(
            "SELECT resource_id FROM resource WHERE resource_type='dataservice'"
        )
    }
    assert ids == {
        _fixture_record(SERVICE_2916)["result"]["id"],
        _fixture_record(SERVICE_3499)["result"]["id"],
    }


def test_malformed_service_rolls_back_datasets_too(conn, directory):
    _copy(directory, DATASET_2275, SERVICE_3499)
    _load(conn, directory)
    before = _counts(conn)
    (directory / "dataservice_bad.json").write_text("{ not json", encoding="utf-8")
    shutil.copy(
        FIXTURES / SERVICE_2916[0] / SERVICE_2916[1], directory / SERVICE_2916[1]
    )
    with pytest.raises(ValueError, match="dataservice_bad.json"):
        _load(conn, directory)
    assert _counts(conn) == before


def test_reload_is_idempotent_and_leaves_no_orphans(conn, directory):
    _copy(directory, DATASET_2275, SERVICE_1254, SERVICE_2916)
    _load(conn, directory)
    first = _counts(conn)
    _load(conn, directory)
    assert _counts(conn) == first
    orphans = conn.execute(
        "SELECT COUNT(*) FROM property_value WHERE resource_pk NOT IN"
        " (SELECT resource_pk FROM resource)"
    ).fetchone()[0]
    assert orphans == 0


def test_changed_service_is_replaced(conn, directory):
    record = _fixture_record(NSIP_SERVICE)["result"]
    _write_service(directory, record, name="dataservice_s.json")
    _load(conn, directory)
    record["title"] = {"fr": "Nouveau"}
    _write_service(directory, record, name="dataservice_s.json")
    _load(conn, directory)
    assert [(r[3], r[5]) for r in _rows(conn, record["id"], DCT + "title")] == [
        ("Nouveau", "fr")
    ]


def test_removed_service_is_pruned_on_full_load(conn, directory):
    _copy(directory, DATASET_2275, SERVICE_2916, SERVICE_3499)
    _load(conn, directory)
    (directory / SERVICE_2916[1]).unlink()
    summary = _load(conn, directory)
    assert summary.pruned == 1
    left = {
        r[0]
        for r in conn.execute(
            "SELECT resource_id FROM resource WHERE resource_type = 'dataservice'"
        )
    }
    assert left == {_fixture_record(SERVICE_3499)["result"]["id"]}
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM resource WHERE resource_type = 'dataset'"
        ).fetchone()[0]
        == 1
    )


def test_prune_is_scoped_to_the_catalogue_and_environment(conn, directory, tmp_path):
    _copy(directory, SERVICE_3499)
    other = tmp_path / "data" / "PROD" / "other"
    other.mkdir()
    shutil.copy(FIXTURES / SERVICE_2916[0] / SERVICE_2916[1], other / SERVICE_2916[1])
    load_catalogue("PROD", "other", conn=conn, data_dir=tmp_path / "data")
    _load(conn, directory)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM resource WHERE catalogue = 'other'"
        ).fetchone()[0]
        == 1
    )


def test_resource_ids_may_name_a_data_service(conn, directory):
    _copy(directory, DATASET_2275, SERVICE_2916, SERVICE_3499)
    _load(conn, directory)
    (directory / SERVICE_3499[1]).unlink()
    record = _fixture_record(SERVICE_2916)["result"]
    record["title"] = {"fr": "Rafraichi"}
    _write_service(directory, record, name=SERVICE_2916[1])
    before_pk = conn.execute("SELECT COUNT(*) FROM resource").fetchone()[0]
    summary = _load(conn, directory, resource_ids=[record["id"]])
    assert (summary.resources, summary.pruned) == (1, 0)
    assert conn.execute("SELECT COUNT(*) FROM resource").fetchone()[0] == before_pk
    assert [r[3] for r in _rows(conn, record["id"], DCT + "title")] == ["Rafraichi"]


def test_resource_ids_may_mix_both_types(conn, directory):
    _copy(directory, DATASET_2275, SERVICE_3499)
    summary = _load(
        conn,
        directory,
        resource_ids=[
            DATASET_2275[1].removeprefix("dataset_").removesuffix(".jsonld"),
            _fixture_record(SERVICE_3499)["result"]["id"],
        ],
    )
    assert summary.resources == 2


def test_unknown_resource_id_raises_and_changes_nothing(conn, directory):
    _copy(directory, SERVICE_3499)
    with pytest.raises(FileNotFoundError, match="no-such-id"):
        _load(conn, directory, resource_ids=["no-such-id"])
    assert _counts(conn) == (0, 0)


# --- review fixes: nulls, duplicates, keyword language ----------------------------------


def test_null_values_are_treated_as_absent(conn, directory):
    record = _fixture_record(SERVICE_1254)["result"]
    record["license"] = None
    record["theme"] = None
    record["description"] = None
    record["publisher"]["homepage"] = None
    record["keywords"][0]["language"] = None
    _write_service(directory, record)
    _load(conn, directory)
    rid = record["id"]
    assert _rows(conn, rid, DCT + "license") == []
    assert _rows(conn, rid, DCAT + "theme") == []
    assert _rows(conn, rid, DCT + "description") == []
    (publisher,) = _rows(conn, rid, DCT + "publisher")
    publisher = json.loads(publisher[3])
    assert FOAF + "homepage" not in publisher  # the null field is absent
    assert FOAF + "name" in publisher
    assert record["keywords"][0]["label"] in {
        r[3] for r in _rows(conn, rid, DCAT + "keyword") if r[5] is None
    }


def test_null_publisher_is_absent(conn, directory):
    record = _fixture_record(NSIP_SERVICE)["result"]
    record["publisher"] = None
    _write_service(directory, record)
    _load(conn, directory)
    assert _rows(conn, record["id"], DCT + "publisher") == []


def test_null_id_is_a_record_without_id(conn, directory):
    record = _fixture_record(NSIP_SERVICE)["result"]
    record["id"] = None
    path = _write_service(directory, record, name="dataservice_x.json")
    with pytest.raises(ValueError, match=f"{path.name}.*without id"):
        _load(conn, directory)


def test_unknown_key_with_null_value_still_raises(conn, directory):
    record = _fixture_record(NSIP_SERVICE)["result"]
    record["new_field"] = None
    _write_service(directory, record, name="dataservice_x.json")
    with pytest.raises(ValueError, match="new_field"):
        _load(conn, directory)


def test_duplicate_requested_ids_load_once(conn, directory):
    _copy(directory, SERVICE_3499)
    rid = _fixture_record(SERVICE_3499)["result"]["id"]
    summary = _load(conn, directory, resource_ids=[rid, rid])
    assert summary.resources == 1
    assert (
        summary.rows
        == conn.execute("SELECT COUNT(*) FROM property_value").fetchone()[0]
    )


def test_two_requested_ids_mapping_to_one_file_load_once(conn, directory):
    record = _fixture_record(NSIP_SERVICE)["result"]
    record["id"] = "a:b"
    _write_service(directory, record, name="dataservice_a-b.json")
    summary = _load(conn, directory, resource_ids=["a:b", "a-b"])
    assert summary.resources == 1


def test_two_files_with_the_same_service_id_raise(conn, directory):
    record = _fixture_record(NSIP_SERVICE)["result"]
    _write_service(directory, record, name="dataservice_one.json")
    _write_service(directory, record, name="dataservice_two.json")
    with pytest.raises(ValueError, match="dataservice_two.json.*dataservice_one.json"):
        _load(conn, directory)
    assert _counts(conn) == (0, 0)


@pytest.mark.parametrize("language", [5, {"a": "b"}, ["en"]])
def test_non_string_keyword_language_raises_naming_the_file(conn, directory, language):
    record = _fixture_record(SERVICE_1254)["result"]
    record["keywords"][0]["language"] = language
    path = _write_service(directory, record, name="dataservice_bad.json")
    with pytest.raises(ValueError, match=path.name):
        _load(conn, directory)
    assert _counts(conn) == (0, 0)
