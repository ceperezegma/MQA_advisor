"""Tests for loading JSON-LD Dataset files into the database (`catalogue_db.load_catalogue`).

Fixtures only (`tests/fixtures/ingested/{catalogue}/`): no network, no credentials. Every test
uses a temporary database and a temporary `{env}/{catalogue}/` tree built from the fixtures,
never the default `data_dir` or `mqa.sqlite`.
"""

import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mqa_advisor import catalogue_db
from mqa_advisor.catalogue_db import LoadSummary, connect, load_catalogue

FIXTURES = Path(__file__).parent / "fixtures" / "ingested"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
DCT = "http://purl.org/dc/terms/"
DCAT = "http://www.w3.org/ns/dcat#"
XSD = "http://www.w3.org/2001/XMLSchema#"
CORE_KINDS = {
    DCAT + "Dataset": "dataset",
    DCAT + "Distribution": "distribution",
    DCAT + "DataService": "dataservice",
}

DATASET_52N = "dataset_3912ef60-1ee4-45f8-9235-c936989285df.jsonld"
DATASET_MALAGA = (
    "dataset_http-edp-int-com-open-data-context-dataset-fiware-parking-malaga.jsonld"
)
DATASET_2275 = "dataset_https-data-gov-lt-datasets-2275-.jsonld"
DATASET_2283 = "dataset_https-data-gov-lt-datasets-2283-.jsonld"
SERVICE_2916 = "https://data.gov.lt/datasets/2916/"


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "mqa.sqlite")
    yield connection
    connection.close()


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "data"


def _copy_fixtures(data_dir, env, catalogue, names=None):
    """Copy fixture files of `catalogue` into `{data_dir}/{env}/{catalogue}/`."""
    target = data_dir / env / catalogue
    target.mkdir(parents=True, exist_ok=True)
    source = FIXTURES / catalogue
    for path in sorted(source.glob("dataset_*.jsonld")):
        if names is None or path.name in names:
            shutil.copy(path, target / path.name)
    return target


def _write_dataset(directory, resource_id, graph, context=None):
    directory.mkdir(parents=True, exist_ok=True)
    context = context or {"dct": DCT, "dcat": DCAT}
    path = directory / f"dataset_{resource_id}.jsonld"
    path.write_text(
        json.dumps({"@graph": graph, "@context": context}), encoding="utf-8"
    )
    return path


def _tuples(conn, resource_id=None):
    sql = (
        "SELECT p.entity_id, p.property_uri, p.value, p.value_kind, p.datatype, p.lang"
        " FROM property_value p JOIN resource r USING (resource_pk)"
    )
    params = ()
    if resource_id is not None:
        sql += " WHERE r.resource_id = ?"
        params = (resource_id,)
    return sorted(conn.execute(sql, params).fetchall(), key=repr)


def _counts(conn):
    return (
        conn.execute("SELECT COUNT(*) FROM resource").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM property_value").fetchone()[0],
    )


def _expected_tuples(path):
    """The (entity, property, value, kind, datatype, lang) tuples found in a source file.

    Written independently of the loader: only core entities have rows, and a reference to an
    auxiliary node gives a `node` row whose value is the node's expanded-form JSON.
    """
    doc = json.loads(path.read_text(encoding="utf-8"))
    context = doc["@context"]
    nodes = {n["@id"]: n for n in doc["@graph"]}

    def full(term):
        if term.startswith(("http://", "https://", "urn:")):
            return term
        prefix, _, rest = term.partition(":")
        return context[prefix] + rest

    def types(node):
        raw = node.get("@type", [])
        return [full(t) for t in ([raw] if isinstance(raw, str) else raw)]

    core = {i: n for i, n in nodes.items() if set(types(n)) & set(CORE_KINDS)}

    def expand_node(node_id):
        node = nodes[node_id]
        out = {} if node_id.startswith("_:") else {"@id": node_id}
        if types(node):
            out["@type"] = sorted(types(node))
        for key, val in node.items():
            if key in ("@id", "@type"):
                continue
            out[full(key)] = [
                value_json(item) for item in (val if isinstance(val, list) else [val])
            ]
        return out

    def is_aux(ref):
        return ref in nodes and ref not in core

    def value_json(item):
        if isinstance(item, str):
            return {"@value": item}
        if "@id" in item:
            ref = item["@id"]
            if is_aux(ref):
                return expand_node(ref)
            return {} if ref.startswith("_:") else {"@id": ref}
        out = {"@value": str(item["@value"])}
        if "@type" in item:
            out["@type"] = full(item["@type"])
        if "@language" in item:
            out["@language"] = item["@language"]
        return out

    out = []
    for entity, node in core.items():
        for t in types(node):
            out.append((entity, RDF_TYPE, t, "uri", None, None))
        for key, val in node.items():
            if key in ("@id", "@type"):
                continue
            for item in val if isinstance(val, list) else [val]:
                if isinstance(item, str):
                    out.append((entity, full(key), item, "literal", None, None))
                elif "@id" in item:
                    ref = item["@id"]
                    if is_aux(ref):
                        dumped = json.dumps(
                            expand_node(ref), sort_keys=True, ensure_ascii=False
                        )
                        out.append((entity, full(key), dumped, "node", None, None))
                    else:
                        ref_kind = "bnode" if ref.startswith("_:") else "uri"
                        out.append((entity, full(key), ref, ref_kind, None, None))
                else:
                    datatype = full(item["@type"]) if "@type" in item else None
                    out.append(
                        (entity, full(key), str(item["@value"]), "literal", datatype,
                         item.get("@language"))
                    )  # fmt: skip
    return sorted(out, key=repr)


# --- round trip -------------------------------------------------------------------------

ROUND_TRIP_CASES = [
    ("52north", DATASET_52N),
    ("52north", DATASET_MALAGA),
    ("data-gov-lt", DATASET_2275),
    ("data-gov-lt", DATASET_2283),
    ("data-gov-lt", "dataset_https-data-gov-lt-datasets-1-.jsonld"),
    ("nsip-lu", "dataset_16c923dc-7cf7-11f0-9925-46bf4a153a63.jsonld"),
    ("nsip-lu", "dataset_1cd509d2-caac-11f0-a122-8aadbbc611c1.jsonld"),
    ("nsip-lu", "dataset_2deb97de-9787-11f0-ab9c-bee5960d250d.jsonld"),
]


@pytest.mark.parametrize(("catalogue", "name"), ROUND_TRIP_CASES)
def test_round_trip_matches_source(conn, data_dir, catalogue, name):
    _copy_fixtures(data_dir, "PROD", catalogue, {name})
    load_catalogue("PROD", catalogue, conn=conn, data_dir=data_dir)
    resource_id = name.removeprefix("dataset_").removesuffix(".jsonld")
    assert _tuples(conn, resource_id) == _expected_tuples(FIXTURES / catalogue / name)


def test_round_trip_stores_core_entities_only(conn, data_dir):
    _copy_fixtures(data_dir, "PROD", "data-gov-lt", {DATASET_2275})
    load_catalogue("PROD", "data-gov-lt", conn=conn, data_dir=data_dir)
    doc = json.loads(
        (FIXTURES / "data-gov-lt" / DATASET_2275).read_text(encoding="utf-8")
    )
    core_types = {"dcat:Dataset", "dcat:Distribution", "dcat:DataService"}

    def is_core(node):
        types = node.get("@type", [])
        return bool(set([types] if isinstance(types, str) else types) & core_types)

    stored = {
        r[0] for r in conn.execute("SELECT DISTINCT entity_id FROM property_value")
    }
    assert stored == {n["@id"] for n in doc["@graph"] if is_core(n)}
    assert SERVICE_2916 in stored
    assert stored < {n["@id"] for n in doc["@graph"]}  # auxiliary nodes have no rows


def test_resource_row(conn, data_dir):
    _copy_fixtures(data_dir, "PROD", "52north", {DATASET_52N})
    before = datetime.now(UTC)
    load_catalogue("PROD", "52north", conn=conn, data_dir=data_dir)
    row = conn.execute(
        "SELECT environment, catalogue, resource_type, resource_id, source_file, loaded_at"
        " FROM resource"
    ).fetchone()
    assert row[:5] == ("PROD", "52north", "dataset", "3912ef60-1ee4-45f8-9235-c936989285df",
                       DATASET_52N)  # fmt: skip
    assert row[5].tzinfo is not None
    assert before <= row[5] <= datetime.now(UTC)


def test_embedded_service_copies_stored_separately(conn, data_dir):
    _copy_fixtures(data_dir, "PROD", "data-gov-lt", {DATASET_2275, DATASET_2283})
    load_catalogue("PROD", "data-gov-lt", conn=conn, data_dir=data_dir)
    rows = {}
    for resource_id in (
        "https-data-gov-lt-datasets-2275-",
        "https-data-gov-lt-datasets-2283-",
    ):
        rows[resource_id] = [
            t for t in _tuples(conn, resource_id) if t[0] == SERVICE_2916
        ]
        assert rows[resource_id], resource_id
    pks = conn.execute(
        "SELECT COUNT(DISTINCT resource_pk) FROM property_value WHERE entity_id = ?",
        (SERVICE_2916,),
    ).fetchone()[0]
    assert pks == 2


# --- literal shapes -----------------------------------------------------------------------


def test_value_shapes(conn, data_dir):
    node = {
        "@id": "http://example.org/d1",
        "@type": "dcat:Dataset",
        "dct:title": [
            "plain",
            {"@language": "eng", "@value": "english"},
            {"@language": "es", "@value": "español"},
            {"@language": "el-t-en-t0-mtec", "@value": "ελληνικά"},
        ],
        "dct:issued": {"@type": "xsd:dateTime", "@value": "2020-01-17T10:39:46Z"},
        "dcat:bbox": {
            "@type": "http://www.opengis.net/ont/geosparql#wktLiteral",
            "@value": "POINT(1 2)",
        },
        "dct:publisher": {"@id": "_:b1"},
        "dcat:landingPage": {"@id": "http://example.org/page"},
        "dcat:byteSize": 42,
        "dcat:score": 1.5,
        "dct:valid": True,
    }
    context = {"dct": DCT, "dcat": DCAT, "xsd": XSD}
    _write_dataset(data_dir / "PROD" / "x", "d1", [node], context)
    load_catalogue("PROD", "x", conn=conn, data_dir=data_dir)
    got = set(_tuples(conn))
    e = "http://example.org/d1"
    assert (e, DCT + "title", "plain", "literal", None, None) in got
    assert (e, DCT + "title", "english", "literal", None, "eng") in got
    assert (e, DCT + "title", "español", "literal", None, "es") in got
    assert (e, DCT + "title", "ελληνικά", "literal", None, "el-t-en-t0-mtec") in got
    assert (
        e,
        DCT + "issued",
        "2020-01-17T10:39:46Z",
        "literal",
        XSD + "dateTime",
        None,
    ) in got
    assert (e, DCAT + "bbox", "POINT(1 2)", "literal",
            "http://www.opengis.net/ont/geosparql#wktLiteral", None) in got  # fmt: skip
    assert (e, DCT + "publisher", "_:b1", "bnode", None, None) in got
    assert (
        e,
        DCAT + "landingPage",
        "http://example.org/page",
        "uri",
        None,
        None,
    ) in got
    assert (e, DCAT + "byteSize", "42", "literal", XSD + "integer", None) in got
    assert (e, DCAT + "score", "1.5", "literal", XSD + "double", None) in got
    assert (e, DCT + "valid", "true", "literal", XSD + "boolean", None) in got


def test_undefined_prefix_names_file(conn, data_dir):
    node = {"@id": "http://example.org/d1", "@type": "dcat:Dataset", "nope:title": "x"}
    path = _write_dataset(data_dir / "PROD" / "x", "d1", [node])
    with pytest.raises(ValueError, match=path.name):
        load_catalogue("PROD", "x", conn=conn, data_dir=data_dir)
    assert _counts(conn) == (0, 0)


def test_embedded_node_with_properties_is_rejected(conn, data_dir):
    # A flat @graph is expected: an object with properties besides @id would lose them.
    node = {
        "@id": "http://example.org/d1",
        "@type": "dcat:Dataset",
        "dcat:distribution": {"@id": "_:b0", "dct:format": "csv"},
    }
    path = _write_dataset(data_dir / "PROD" / "x", "d1", [node])
    with pytest.raises(ValueError, match=path.name):
        load_catalogue("PROD", "x", conn=conn, data_dir=data_dir)
    assert _counts(conn) == (0, 0)


@pytest.mark.parametrize(
    "node",
    [
        {"@id": 5, "@type": "dcat:Dataset"},
        {"@id": "http://example.org/d1", "@type": [None]},
        {"@id": "http://example.org/d1", "@type": {"@id": "dcat:Dataset"}},
        {
            "@id": "http://example.org/d1",
            "@type": "dcat:Dataset",
            "dct:title": {"@value": "x", "@type": 7},
        },
        {
            "@id": "http://example.org/d1",
            "@type": "dcat:Dataset",
            "dct:title": {"@id": 7},
        },
    ],
)
def test_non_string_id_or_type_names_file(conn, data_dir, node):
    path = _write_dataset(data_dir / "PROD" / "x", "d1", [node])
    with pytest.raises(ValueError, match=path.name):
        load_catalogue("PROD", "x", conn=conn, data_dir=data_dir)
    assert _counts(conn) == (0, 0)


# --- entity_kind and auxiliary nodes -------------------------------------------------------

DATASET = {"@id": "http://example.org/d1", "@type": "dcat:Dataset"}


def _kinds(conn):
    rows = conn.execute("SELECT DISTINCT entity_id, entity_kind FROM property_value")
    return dict(rows.fetchall())


def _node_values(conn, prop):
    return [
        json.loads(v)
        for (v,) in conn.execute(
            "SELECT value FROM property_value WHERE property_uri = ?"
            " AND value_kind = 'node'",
            (prop,),
        )
    ]


def _load_graph(conn, data_dir, graph, resource_id="d1"):
    path = _write_dataset(data_dir / "PROD" / "x", resource_id, graph)
    return path, lambda: load_catalogue("PROD", "x", conn=conn, data_dir=data_dir)


def test_entity_kind_comes_from_the_core_type(conn, data_dir):
    graph = [
        {**DATASET, "dct:title": "t"},
        {"@id": "_:dist", "@type": "dcat:Distribution", "dct:title": "d"},
        {"@id": "http://example.org/s", "@type": "dcat:DataService"},
    ]
    _, load = _load_graph(conn, data_dir, graph)
    summary = load()
    assert _kinds(conn) == {
        "http://example.org/d1": "dataset",
        "_:dist": "distribution",
        "http://example.org/s": "dataservice",
    }
    assert summary.orphan_nodes == 0


def test_core_node_keeps_all_its_types_as_rdf_type_rows(conn, data_dir):
    node = {**DATASET, "@type": ["dcat:Dataset", "dct:Collection"]}
    _, load = _load_graph(conn, data_dir, [node])
    load()
    assert _kinds(conn) == {"http://example.org/d1": "dataset"}
    stored = {r[0] for r in conn.execute("SELECT value FROM property_value")}
    assert stored == {DCAT + "Dataset", DCT + "Collection"}


def test_node_with_two_core_types_is_rejected(conn, data_dir):
    node = {**DATASET, "@type": ["dcat:Dataset", "dcat:Distribution"]}
    path, load = _load_graph(conn, data_dir, [node])
    with pytest.raises(ValueError, match=path.name):
        load()
    assert _counts(conn) == (0, 0)


def test_file_without_a_dataset_node_is_rejected(conn, data_dir):
    graph = [{"@id": "http://example.org/n", "@type": "dct:Location"}]
    path, load = _load_graph(conn, data_dir, graph)
    with pytest.raises(ValueError, match=f"{path.name}: no dcat:Dataset"):
        load()
    assert _counts(conn) == (0, 0)


def test_duplicate_node_id_is_rejected(conn, data_dir):
    path, load = _load_graph(conn, data_dir, [DATASET, DATASET])
    with pytest.raises(ValueError, match=path.name):
        load()


def test_auxiliary_node_is_folded_into_the_property_that_reaches_it(conn, data_dir):
    graph = [
        {
            **DATASET,
            "dct:title": "real title",
            "dct:temporal": {"@id": "_:period"},
            "dct:conformsTo": {"@id": "http://example.org/law"},
        },
        {"@id": "_:period", "@type": "dct:PeriodOfTime", "dct:title": "period title"},
        {
            "@id": "http://example.org/law",
            "@type": "dct:Standard",
            "dct:description": {"@language": "eng", "@value": "law text"},
        },
    ]
    _, load = _load_graph(conn, data_dir, graph)
    load()
    titles = conn.execute(
        "SELECT entity_id, value FROM property_value WHERE property_uri = ?",
        (DCT + "title",),
    ).fetchall()
    assert titles == [("http://example.org/d1", "real title")]
    descriptions = conn.execute(
        "SELECT COUNT(*) FROM property_value WHERE property_uri = ?",
        (DCT + "description",),
    ).fetchone()[0]
    assert descriptions == 0
    # a bnode has no @id; a URI node keeps it; every value is a list
    assert _node_values(conn, DCT + "temporal") == [
        {"@type": [DCT + "PeriodOfTime"], DCT + "title": [{"@value": "period title"}]}
    ]
    assert _node_values(conn, DCT + "conformsTo") == [
        {
            "@id": "http://example.org/law",
            "@type": [DCT + "Standard"],
            DCT + "description": [{"@language": "eng", "@value": "law text"}],
        }
    ]


def test_nested_auxiliary_nodes_are_folded_recursively(conn, data_dir):
    graph = [
        {**DATASET, "dcat:qualifiedRelation": {"@id": "_:rel"}},
        {
            "@id": "_:rel",
            "@type": "dcat:Relationship",
            "dcat:hadRole": {"@id": "http://example.org/role"},
        },
        {
            "@id": "http://example.org/role",
            "@type": "dcat:Role",
            "dct:title": "related",
        },
    ]
    _, load = _load_graph(conn, data_dir, graph)
    summary = load()
    (rel,) = _node_values(conn, DCAT + "qualifiedRelation")
    assert rel[DCAT + "hadRole"] == [
        {
            "@id": "http://example.org/role",
            "@type": [DCAT + "Role"],
            DCT + "title": [{"@value": "related"}],
        }
    ]
    assert summary.orphan_nodes == 0


def test_shared_auxiliary_node_is_copied_into_each_owner(conn, data_dir):
    licence = {"@id": "http://example.org/l"}
    graph = [
        {**DATASET, "dct:license": licence},
        {"@id": "_:dist", "@type": "dcat:Distribution", "dct:license": licence},
        {**licence, "@type": "dct:LicenseDocument", "dct:title": "L"},
    ]
    _, load = _load_graph(conn, data_dir, graph)
    load()
    rows = conn.execute(
        "SELECT entity_id, value FROM property_value WHERE value_kind = 'node'"
    ).fetchall()
    assert {r[0] for r in rows} == {"http://example.org/d1", "_:dist"}
    assert len({r[1] for r in rows}) == 1


def test_reference_to_a_core_entity_is_not_folded(conn, data_dir):
    graph = [
        {**DATASET, "dcat:distribution": {"@id": "_:dist"}},
        {
            "@id": "_:dist",
            "@type": "dcat:Distribution",
            "dcat:accessService": {"@id": "http://example.org/s"},
        },
        {"@id": "http://example.org/s", "@type": "dcat:DataService"},
    ]
    _, load = _load_graph(conn, data_dir, graph)
    load()
    refs = set(
        conn.execute(
            "SELECT entity_id, property_uri, value, value_kind FROM property_value"
            " WHERE property_uri IN (?, ?)",
            (DCAT + "distribution", DCAT + "accessService"),
        )
    )
    assert refs == {
        ("http://example.org/d1", DCAT + "distribution", "_:dist", "bnode"),
        ("_:dist", DCAT + "accessService", "http://example.org/s", "uri"),
    }


def test_reference_inside_a_node_to_a_core_entity_or_unknown_id_is_a_bare_id(
    conn, data_dir
):
    graph = [
        {**DATASET, "dct:relation": {"@id": "_:n"}},
        {
            "@id": "_:n",
            "dct:source": [
                {"@id": "http://example.org/d1"},
                {"@id": "http://example.org/elsewhere"},
                {"@id": "_:ghost"},
            ],
        },
    ]
    _, load = _load_graph(conn, data_dir, graph)
    load()
    (node,) = _node_values(conn, DCT + "relation")
    assert node == {
        DCT + "source": [
            {"@id": "http://example.org/d1"},
            {"@id": "http://example.org/elsewhere"},
            {},
        ]
    }


def test_auxiliary_nodes_that_refer_back_are_rejected(conn, data_dir):
    graph = [
        {**DATASET, "dct:relation": {"@id": "_:a"}},
        {"@id": "_:a", "dct:relation": {"@id": "_:b"}},
        {"@id": "_:b", "dct:relation": {"@id": "_:a"}},
    ]
    path, load = _load_graph(conn, data_dir, graph)
    with pytest.raises(ValueError, match=path.name):
        load()
    assert _counts(conn) == (0, 0)


def test_unreached_nodes_are_not_stored_and_warned_once(conn, data_dir, caplog):
    orphans = [{"@id": f"_:o{i}", "dct:title": "t"} for i in range(3)]
    _write_dataset(data_dir / "PROD" / "x", "d1", [DATASET, *orphans])
    _write_dataset(data_dir / "PROD" / "x", "d2", [DATASET, orphans[0]])
    with caplog.at_level(logging.DEBUG, logger="mqa_advisor.catalogue_db"):
        summary = load_catalogue("PROD", "x", conn=conn, data_dir=data_dir)
    assert summary.orphan_nodes == 4
    stored_titles = conn.execute(
        "SELECT COUNT(*) FROM property_value WHERE property_uri = ?", (DCT + "title",)
    ).fetchone()[0]
    assert stored_titles == 0
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "4 node(s) in 2 resource(s)" in warnings[0].getMessage()
    assert "_:o0" in caplog.text  # listed at DEBUG


def test_no_log_when_every_node_is_reached(conn, data_dir, caplog):
    _, load = _load_graph(conn, data_dir, [DATASET])
    with caplog.at_level(logging.DEBUG, logger="mqa_advisor.catalogue_db"):
        load()
    assert caplog.records == []


def test_node_json_does_not_depend_on_key_or_type_order(conn, data_dir):
    def graph(reverse):
        keys = [("dct:title", "t"), ("dct:description", "d")]
        types = ["dct:Standard", "dct:Location"]
        node = dict(reversed(keys) if reverse else keys)
        return [
            {**DATASET, "dct:relation": {"@id": "_:a"}},
            {"@id": "_:a", "@type": types[::-1] if reverse else types, **node},
        ]

    values = []
    for reverse in (False, True):
        _write_dataset(data_dir / "PROD" / "x", "d1", graph(reverse))
        load_catalogue("PROD", "x", conn=conn, data_dir=data_dir)
        values.append(
            conn.execute(
                "SELECT value FROM property_value WHERE value_kind = 'node'"
            ).fetchone()[0]
        )
    assert values[0] == values[1]


def test_nsip_lu_dataset_has_one_title_and_one_description(conn, data_dir):
    """The reported bug: the title of the PeriodOfTime node and the description of the legal
    resource were stored as if they were the Dataset's."""
    _copy_fixtures(
        data_dir,
        "PROD",
        "nsip-lu",
        {"dataset_16c923dc-7cf7-11f0-9925-46bf4a153a63.jsonld"},
    )
    load_catalogue("PROD", "nsip-lu", conn=conn, data_dir=data_dir)
    dataset = "http://data.europa.eu/88u/dataset/16c923dc-7cf7-11f0-9925-46bf4a153a63"
    for prop, start in (
        ("title", "Registre national des personnes physiques"),
        ("description", "Signalétique : Nom"),
    ):
        rows = conn.execute(
            "SELECT entity_id, value FROM property_value WHERE property_uri = ?",
            (DCT + prop,),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == dataset
        assert rows[0][1].startswith(start)
    (legislation,) = _node_values(
        conn, "http://data.europa.eu/r5r/applicableLegislation"
    )
    assert legislation[DCT + "description"][0]["@value"].startswith(
        "Loi du 19 juin 2013"
    )
    (temporal,) = _node_values(conn, DCT + "temporal")
    assert temporal[DCT + "title"] == [
        {"@language": "eng", "@value": "Dataset Coverage Period"}
    ]


def test_nsip_lu_stores_only_core_entities(conn, data_dir):
    _copy_fixtures(data_dir, "PROD", "nsip-lu")
    summary = load_catalogue("PROD", "nsip-lu", conn=conn, data_dir=data_dir)
    kinds = {
        r[0] for r in conn.execute("SELECT DISTINCT entity_kind FROM property_value")
    }
    assert kinds == {"dataset", "distribution"}
    not_core = conn.execute(
        "SELECT COUNT(DISTINCT p.resource_pk || p.entity_id) FROM property_value p"
        " WHERE NOT EXISTS (SELECT 1 FROM property_value q"
        " WHERE q.resource_pk = p.resource_pk AND q.entity_id = p.entity_id"
        " AND q.property_uri = ? AND q.value IN (?, ?, ?))",
        (RDF_TYPE, *CORE_KINDS),
    ).fetchone()[0]
    assert not_core == 0
    assert summary.orphan_nodes == 0


# --- summary ---------------------------------------------------------------------------------


def test_summary_counts_match_database(conn, data_dir):
    _copy_fixtures(data_dir, "PROD", "nsip-lu")
    summary = load_catalogue("PROD", "nsip-lu", conn=conn, data_dir=data_dir)
    resources, rows = _counts(conn)
    assert isinstance(summary, LoadSummary)
    assert (summary.resources, summary.rows, summary.pruned) == (resources, rows, 0)


# --- re-load, replace, prune ------------------------------------------------------------------


def test_load_twice_is_idempotent(conn, data_dir):
    _copy_fixtures(data_dir, "PROD", "nsip-lu")
    load_catalogue("PROD", "nsip-lu", conn=conn, data_dir=data_dir)
    first = _counts(conn), _tuples(conn)
    load_catalogue("PROD", "nsip-lu", conn=conn, data_dir=data_dir)
    assert (_counts(conn), _tuples(conn)) == first
    orphans = conn.execute(
        "SELECT COUNT(*) FROM property_value WHERE resource_pk NOT IN"
        " (SELECT resource_pk FROM resource)"
    ).fetchone()[0]
    assert orphans == 0


def test_isolation_between_catalogues_and_environments(conn, data_dir):
    _copy_fixtures(
        data_dir,
        "PROD",
        "nsip-lu",
        {"dataset_16c923dc-7cf7-11f0-9925-46bf4a153a63.jsonld"},
    )
    _copy_fixtures(
        data_dir,
        "PPE",
        "nsip-lu",
        {"dataset_1cd509d2-caac-11f0-a122-8aadbbc611c1.jsonld"},
    )
    _copy_fixtures(data_dir, "PROD", "data-gov-lt", {DATASET_2275})
    for env, cat in (("PROD", "nsip-lu"), ("PPE", "nsip-lu"), ("PROD", "data-gov-lt")):
        load_catalogue(env, cat, conn=conn, data_dir=data_dir)

    def snapshot():
        return conn.execute(
            "SELECT r.environment, r.catalogue, COUNT(*) FROM resource r JOIN property_value p"
            " USING (resource_pk) GROUP BY 1, 2 ORDER BY 1, 2"
        ).fetchall()

    before = snapshot()
    load_catalogue("PROD", "nsip-lu", conn=conn, data_dir=data_dir)
    assert snapshot() == before


def test_changed_file_replaces_only_its_rows(conn, data_dir):
    directory = data_dir / "PROD" / "x"
    node = {"@id": "http://example.org/a", "@type": "dcat:Dataset", "dct:title": "old"}
    other = {
        "@id": "http://example.org/b",
        "@type": "dcat:Dataset",
        "dct:title": "keep",
    }
    _write_dataset(directory, "a", [node])
    _write_dataset(directory, "b", [other])
    load_catalogue("PROD", "x", conn=conn, data_dir=data_dir)
    b_before = _tuples(conn, "b")
    node["dct:title"] = "new"
    _write_dataset(directory, "a", [node])
    load_catalogue("PROD", "x", conn=conn, data_dir=data_dir)
    titles = [t[2] for t in _tuples(conn, "a") if t[1] == DCT + "title"]
    assert titles == ["new"]
    assert _tuples(conn, "b") == b_before


def test_full_load_prunes_removed_files(conn, data_dir):
    directory = data_dir / "PROD" / "x"
    for rid in ("a", "b", "c"):
        _write_dataset(
            directory,
            rid,
            [{"@id": f"http://example.org/{rid}", "@type": "dcat:Dataset"}],
        )
    load_catalogue("PROD", "x", conn=conn, data_dir=data_dir)
    (directory / "dataset_b.jsonld").unlink()
    summary = load_catalogue("PROD", "x", conn=conn, data_dir=data_dir)
    ids = {r[0] for r in conn.execute("SELECT resource_id FROM resource")}
    assert ids == {"a", "c"}
    assert summary.pruned == 1
    assert _tuples(conn, "b") == []


def test_resource_ids_refreshes_only_those_and_prunes_nothing(conn, data_dir):
    directory = data_dir / "PROD" / "x"
    for rid in ("a", "b", "c"):
        _write_dataset(
            directory,
            rid,
            [
                {
                    "@id": f"http://example.org/{rid}",
                    "@type": "dcat:Dataset",
                    "dct:title": "v1",
                }
            ],
        )
    load_catalogue("PROD", "x", conn=conn, data_dir=data_dir)
    before = {rid: _tuples(conn, rid) for rid in "abc"}
    _write_dataset(
        directory,
        "a",
        [{"@id": "http://example.org/a", "@type": "dcat:Dataset", "dct:title": "v2"}],
    )
    _write_dataset(
        directory,
        "b",
        [{"@id": "http://example.org/b", "@type": "dcat:Dataset", "dct:title": "v2"}],
    )
    (directory / "dataset_c.jsonld").unlink()  # missing file must not be pruned
    summary = load_catalogue(
        "PROD", "x", resource_ids=["a"], conn=conn, data_dir=data_dir
    )
    assert summary.resources == 1
    assert summary.pruned == 0
    assert [t[2] for t in _tuples(conn, "a") if t[1] == DCT + "title"] == ["v2"]
    assert _tuples(conn, "b") == before["b"]  # changed on disk but not requested
    assert _tuples(conn, "c") == before["c"]  # file gone but not pruned


@pytest.mark.parametrize("bad_id", ["", "../../other/dataset_x", "sub/dir", "sub\\dir"])
def test_invalid_resource_id_is_rejected_before_any_write(conn, data_dir, bad_id):
    directory = data_dir / "PROD" / "x"
    _write_dataset(
        directory, "a", [{"@id": "http://example.org/a", "@type": "dcat:Dataset"}]
    )
    # A file the traversal would reach if the id were joined into a path unchecked.
    _write_dataset(
        data_dir / "other",
        "x",
        [{"@id": "http://example.org/x", "@type": "dcat:Dataset"}],
    )
    with pytest.raises(ValueError, match="invalid resource id"):
        load_catalogue(
            "PROD", "x", resource_ids=["a", bad_id], conn=conn, data_dir=data_dir
        )
    assert _counts(conn) == (0, 0)


def test_unknown_resource_id_raises_and_changes_nothing(conn, data_dir):
    directory = data_dir / "PROD" / "x"
    _write_dataset(
        directory, "a", [{"@id": "http://example.org/a", "@type": "dcat:Dataset"}]
    )
    load_catalogue("PROD", "x", conn=conn, data_dir=data_dir)
    before = (_counts(conn), _tuples(conn))
    with pytest.raises(FileNotFoundError, match="missing"):
        load_catalogue(
            "PROD", "x", resource_ids=["a", "missing"], conn=conn, data_dir=data_dir
        )
    assert (_counts(conn), _tuples(conn)) == before


@pytest.mark.parametrize("unsafe_id", ["a:b", "x*"])
def test_id_with_other_unsafe_characters_is_not_found(conn, data_dir, unsafe_id):
    _write_dataset(
        data_dir / "PROD" / "x",
        "a",
        [{"@id": "http://example.org/a", "@type": "dcat:Dataset"}],
    )
    with pytest.raises(FileNotFoundError, match="no Dataset or Data Service file"):
        load_catalogue(
            "PROD", "x", resource_ids=["a", unsafe_id], conn=conn, data_dir=data_dir
        )
    assert _counts(conn) == (0, 0)


def test_missing_directory_on_full_load_raises_and_keeps_data(conn, data_dir):
    _write_dataset(
        data_dir / "PROD" / "x",
        "a",
        [{"@id": "http://example.org/a", "@type": "dcat:Dataset"}],
    )
    load_catalogue("PROD", "x", conn=conn, data_dir=data_dir)
    shutil.rmtree(data_dir / "PROD" / "x")
    before = (_counts(conn), _tuples(conn))
    with pytest.raises(FileNotFoundError):
        load_catalogue("PROD", "x", conn=conn, data_dir=data_dir)
    assert (_counts(conn), _tuples(conn)) == before


# --- rollback ----------------------------------------------------------------------------------


def test_malformed_file_mid_load_leaves_previous_data(conn, data_dir):
    directory = data_dir / "PROD" / "x"
    for rid in ("a", "c"):
        _write_dataset(
            directory,
            rid,
            [
                {
                    "@id": f"http://example.org/{rid}",
                    "@type": "dcat:Dataset",
                    "dct:title": "v1",
                }
            ],
        )
    load_catalogue("PROD", "x", conn=conn, data_dir=data_dir)
    before = (_counts(conn), _tuples(conn))
    _write_dataset(
        directory,
        "a",
        [{"@id": "http://example.org/a", "@type": "dcat:Dataset", "dct:title": "v2"}],
    )
    (directory / "dataset_b.jsonld").write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_catalogue("PROD", "x", conn=conn, data_dir=data_dir)
    assert (_counts(conn), _tuples(conn)) == before


def test_failure_during_write_rolls_back(conn, data_dir, monkeypatch):
    directory = data_dir / "PROD" / "x"
    for rid in ("a", "b"):
        _write_dataset(
            directory,
            rid,
            [
                {
                    "@id": f"http://example.org/{rid}",
                    "@type": "dcat:Dataset",
                    "dct:title": "v1",
                }
            ],
        )
    load_catalogue("PROD", "x", conn=conn, data_dir=data_dir)
    before = (_counts(conn), _tuples(conn))
    for rid in ("a", "b"):
        _write_dataset(
            directory,
            rid,
            [
                {
                    "@id": f"http://example.org/{rid}",
                    "@type": "dcat:Dataset",
                    "dct:title": "v2",
                }
            ],
        )

    real = catalogue_db._replace_resource
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("disk full")
        return real(*args, **kwargs)

    monkeypatch.setattr(catalogue_db, "_replace_resource", flaky)
    with pytest.raises(RuntimeError, match="disk full"):
        load_catalogue("PROD", "x", conn=conn, data_dir=data_dir)
    assert len(calls) == 2
    assert (_counts(conn), _tuples(conn)) == before


# --- Data Service files and connection ownership ---------------------------------------------


def test_files_matching_neither_pattern_are_ignored(conn, data_dir):
    directory = data_dir / "PROD" / "x"
    _write_dataset(
        directory, "a", [{"@id": "http://example.org/a", "@type": "dcat:Dataset"}]
    )
    (directory / "notes.txt").write_text("{ not json", encoding="utf-8")
    (directory / "dataservice_s1.txt").write_text("{ not json", encoding="utf-8")
    summary = load_catalogue("PROD", "x", conn=conn, data_dir=data_dir)
    assert (summary.resources, summary.pruned) == (1, 0)


def test_own_connection_is_closed_and_passed_connection_stays_open(
    tmp_path, data_dir, monkeypatch
):
    _write_dataset(
        data_dir / "PROD" / "x",
        "a",
        [{"@id": "http://example.org/a", "@type": "dcat:Dataset"}],
    )
    db = tmp_path / "own.sqlite"
    opened = []

    def fake_connect():
        opened.append(connect(db))
        return opened[-1]

    monkeypatch.setattr(catalogue_db, "connect", fake_connect)
    load_catalogue("PROD", "x", data_dir=data_dir)
    assert len(opened) == 1
    with pytest.raises(Exception, match="closed"):
        opened[0].execute("SELECT 1")
    check = connect(db)
    assert _counts(check)[0] == 1
    load_catalogue("PROD", "x", conn=check, data_dir=data_dir)
    check.execute("SELECT 1")  # still open
    check.close()
