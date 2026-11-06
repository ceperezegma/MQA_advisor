"""SQLite database holding the properties of every fetched catalogue record.

The database is derived data: the fetched files stay the source of truth and it can always be
rebuilt from them. It is not committed (`fetched_catalogues_data/*` is git-ignored).

A *resource* is a Dataset or a standalone Data Service of a catalogue; `property_value` holds
one row per property value of every node (entity) in the resource's record. Deleting a
resource deletes its property rows (`ON DELETE CASCADE`), so any table added later must
reference `resource` the same way.

`connect()` returns an open connection: the caller commits its writes and closes it. It also
creates three read-only views for the common questions: `catalogue_resources` (the Datasets
and standalone Data Services), `dataset_distributions` (`resource_pk, distribution_id`) and
`dataset_embedded_services` (`resource_pk, distribution_id, service_id`).

`load_catalogue()` fills the database from the files already fetched under
`{data_dir}/{env}/{catalogue}/`. It replaces each resource it loads (delete + insert), so
refreshing one record never reloads the others, and a full load also deletes resources whose
file is gone. The whole call is one transaction: any error leaves the previous data as it was.
Both `dataset_*.jsonld` files and standalone `dataservice_*.json` files are read; the latter
are mapped by `dataservice_mapping`.

Usage:

    conn = connect()
    try:
        load_catalogue("PROD", "nsip-lu", conn=conn)  # commits; or rolls back and raises
    finally:
        conn.close()
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mqa_advisor import dataservice_mapping as dsm
from mqa_advisor.filenames import safe_file_component
from mqa_advisor.ingest import FETCHED_DATA_DIR

logger = logging.getLogger(__name__)

DB_PATH = FETCHED_DATA_DIR / "mqa.sqlite"

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
_XSD = "http://www.w3.org/2001/XMLSchema#"
_DATASET_PREFIX = "dataset_"
_DATASET_SUFFIX = ".jsonld"
_DATASERVICE_PREFIX = "dataservice_"
_DATASERVICE_SUFFIX = ".json"
_ABSOLUTE_URI_PREFIXES = ("http://", "https://", "urn:")

SCHEMA = """
CREATE TABLE IF NOT EXISTS resource (
    resource_pk    INTEGER PRIMARY KEY,
    environment    TEXT NOT NULL,
    catalogue      TEXT NOT NULL,
    resource_type  TEXT NOT NULL CHECK (resource_type IN ('dataset', 'dataservice')),
    resource_id    TEXT NOT NULL,
    source_file    TEXT NOT NULL,
    loaded_at      TIMESTAMP NOT NULL,
    UNIQUE (environment, catalogue, resource_type, resource_id)
);

CREATE TABLE IF NOT EXISTS property_value (
    resource_pk    INTEGER NOT NULL REFERENCES resource(resource_pk) ON DELETE CASCADE,
    entity_id      TEXT NOT NULL,
    entity_type    TEXT,
    property_uri   TEXT NOT NULL,
    value          TEXT NOT NULL,
    value_kind     TEXT NOT NULL CHECK (value_kind IN ('literal', 'uri', 'bnode')),
    datatype       TEXT,
    lang           TEXT
);

CREATE INDEX IF NOT EXISTS property_value_entity   ON property_value (resource_pk, entity_id);
CREATE INDEX IF NOT EXISTS property_value_property ON property_value (resource_pk, property_uri);
CREATE INDEX IF NOT EXISTS property_value_type     ON property_value (resource_pk, entity_type);

-- Plain views (saved queries, no stored data): they always reflect the current rows.
-- All Datasets and standalone Data Services; embedded Data Services are not resources.
CREATE VIEW IF NOT EXISTS catalogue_resources AS
SELECT environment, catalogue, resource_type, resource_id, resource_pk
FROM resource;

-- The Distributions of each Dataset: the references in its `dcat:distribution` properties
-- (literal values are not node ids).
CREATE VIEW IF NOT EXISTS dataset_distributions AS
SELECT pv.resource_pk AS resource_pk, pv.value AS distribution_id
FROM property_value AS pv
JOIN resource AS r ON r.resource_pk = pv.resource_pk
WHERE r.resource_type = 'dataset'
  AND pv.property_uri = 'http://www.w3.org/ns/dcat#distribution'
  AND pv.value_kind IN ('uri', 'bnode');

-- The Data Services embedded in a Dataset, through each Distribution's `dcat:accessService`.
-- The join is on (resource_pk, entity_id), so a service shared by two Datasets is returned
-- once for each of them.
CREATE VIEW IF NOT EXISTS dataset_embedded_services AS
SELECT dd.resource_pk AS resource_pk,
       dd.distribution_id AS distribution_id,
       pv.value AS service_id
FROM dataset_distributions AS dd
JOIN property_value AS pv
  ON pv.resource_pk = dd.resource_pk AND pv.entity_id = dd.distribution_id
WHERE pv.property_uri = 'http://www.w3.org/ns/dcat#accessService'
  AND pv.value_kind IN ('uri', 'bnode');
"""


# SQLite has no timestamp type: `loaded_at` is stored as ISO 8601 text and converted to and
# from a `datetime` here, because the adapters/converters built into `sqlite3` are deprecated.
def _adapt_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("loaded_at must be timezone-aware (UTC); got a naive datetime")
    return value.isoformat()


sqlite3.register_adapter(datetime, _adapt_datetime)
sqlite3.register_converter(
    "TIMESTAMP", lambda raw: datetime.fromisoformat(raw.decode())
)


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    """Open the database at `path`, creating the file, tables and indexes if missing.

    Foreign keys are switched on for the returned connection (SQLite has them off by
    default, and the cascade depends on them). Calling this again on the same path changes
    nothing.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
    except BaseException:
        conn.close()
        raise
    return conn


@dataclass(frozen=True)
class LoadSummary:
    """What a `load_catalogue` call did.

    `null_type_entities` counts the entities stored with a NULL `entity_type` (their `@type`
    was missing or had several values); the load logs one warning with this count, and each
    entity at DEBUG level.
    """

    resources: int
    pruned: int
    rows: int
    null_type_entities: int


# One `property_value` row without its `resource_pk`:
# (entity_id, entity_type, property_uri, value, value_kind, datatype, lang)
_Row = tuple[str, str | None, str, str, str, str | None, str | None]


def _expand(term: str, context: dict[str, str], source: str) -> str:
    """Expand `prefix:local` to a full URI through the file's `@context`.

    Absolute URIs are returned as they are. Anything else (an undefined prefix, a bare word)
    means the file is malformed, so it raises instead of silently dropping the term.
    """
    prefix, sep, rest = term.partition(":")
    if sep and prefix in context:
        return context[prefix] + rest
    if term.startswith(_ABSOLUTE_URI_PREFIXES):
        return term
    raise ValueError(
        f"{source}: cannot expand {term!r}, prefix not defined in its @context"
    )


def _native_literal(value: bool | float) -> tuple[str, str]:
    """Value text and `xsd` datatype of a native JSON boolean or number."""
    if isinstance(value, bool):  # before int: bool is a subclass of int
        return ("true" if value else "false"), _XSD + "boolean"
    if isinstance(value, int):
        return str(value), _XSD + "integer"
    return str(value), _XSD + "double"


def _require_str(value: object, what: str, source: str) -> None:
    """Raise a `ValueError` naming the file when a JSON-LD id or type is not a string."""
    if not isinstance(value, str):
        raise ValueError(f"{source}: {what} must be a string, got {value!r}")  # noqa: TRY004 - malformed file, one exception type


def _value_columns(
    item: object, context: dict[str, str], source: str
) -> tuple[str, str, str | None, str | None]:
    """(value, value_kind, datatype, lang) for one value of a property."""
    if isinstance(item, str):
        return item, "literal", None, None
    if isinstance(item, bool | int | float):
        value, datatype = _native_literal(item)
        return value, "literal", datatype, None
    if isinstance(item, dict) and "@id" in item:
        # A reference only: properties of the target are separate nodes in `@graph`. An object
        # with more keys would lose them silently, so it is rejected like any malformed file.
        extra = set(item) - {"@id"}
        if extra:
            raise ValueError(
                f"{source}: embedded node {item['@id']!r} has properties {sorted(extra)}"
                " besides @id; expected a flat @graph"
            )
        _require_str(item["@id"], "@id", source)
        kind = "bnode" if item["@id"].startswith("_:") else "uri"
        return item["@id"], kind, None, None
    if isinstance(item, dict) and "@value" in item:
        raw = item["@value"]
        if isinstance(raw, bool | int | float):
            value, datatype = _native_literal(raw)
        else:
            value, datatype = str(raw), None
        if "@type" in item:
            _require_str(item["@type"], "@type", source)
            datatype = _expand(item["@type"], context, source)
        return value, "literal", datatype, item.get("@language")
    raise ValueError(f"{source}: unsupported value {item!r}")


def _node_rows(
    node: dict, context: dict[str, str], source: str, resource_id: str
) -> tuple[list[_Row], bool]:
    """The rows of one `@graph` node, and whether its `entity_type` is NULL.

    A node whose `@type` is missing or has several values gets a NULL `entity_type` and a
    DEBUG message (`load_catalogue` logs one warning for the whole load); its types are still
    stored as `rdf:type` rows.
    """
    if "@id" not in node:
        raise ValueError(f"{source}: node without @id: {node!r}")
    entity = node["@id"]
    _require_str(entity, "@id", source)
    types = node.get("@type", [])
    types = [types] if isinstance(types, str) else types
    if not isinstance(types, list):
        raise ValueError(f"{source}: @type of {entity} must be a string or a list")  # noqa: TRY004 - malformed file, one exception type
    for t in types:
        _require_str(t, "@type", source)
    expanded = [_expand(t, context, source) for t in types]
    entity_type = expanded[0] if len(expanded) == 1 else None
    if entity_type is None:
        logger.debug(
            "resource %s: entity %s has %d @type value(s), entity_type stored as NULL",
            resource_id,
            entity,
            len(expanded),
        )

    rows: list[_Row] = [
        (entity, entity_type, RDF_TYPE, t, "uri", None, None) for t in expanded
    ]
    for key, raw in node.items():
        if key in ("@id", "@type"):
            continue
        if key.startswith("@"):
            raise ValueError(f"{source}: unsupported keyword {key!r} in node {entity}")
        property_uri = _expand(key, context, source)
        for item in raw if isinstance(raw, list) else [raw]:
            rows.append(
                (
                    entity,
                    entity_type,
                    property_uri,
                    *_value_columns(item, context, source),
                )
            )
    return rows, entity_type is None


def _parse_dataset_file(path: Path) -> tuple[list[_Row], int]:
    """Rows of one Dataset file and the number of its entities stored with a NULL type."""
    source = path.name
    resource_id = source.removeprefix(_DATASET_PREFIX).removesuffix(_DATASET_SUFFIX)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source}: not valid JSON ({exc})") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("@graph"), list):
        raise ValueError(f"{source}: expected an object with a @graph list")  # noqa: TRY004 - malformed file, one exception type
    context = doc.get("@context")
    if not isinstance(context, dict):
        raise ValueError(f"{source}: @context must be a prefix -> URI mapping")  # noqa: TRY004 - same

    rows: list[_Row] = []
    null_type_entities = 0
    for node in doc["@graph"]:
        node_rows, is_null = _node_rows(node, context, source, resource_id)
        rows.extend(node_rows)
        null_type_entities += is_null
    return rows, null_type_entities


def _object(value: object, what: str, source: str) -> dict:
    """`value` as a dict, or a `ValueError` naming the file."""
    if not isinstance(value, dict):
        raise ValueError(f"{source}: {what} must be an object, got {value!r}")  # noqa: TRY004 - malformed file, one exception type
    return value


def _list(value: object, what: str, source: str) -> list:
    """`value` as a list, or a `ValueError` naming the file."""
    if not isinstance(value, list):
        raise ValueError(f"{source}: {what} must be a list, got {value!r}")  # noqa: TRY004 - same
    return value


def _text(value: object, what: str, source: str) -> str:
    """`value` as a string, or a `ValueError` naming the file."""
    if not isinstance(value, str):
        raise ValueError(f"{source}: {what} must be a string, got {value!r}")  # noqa: TRY004 - same
    return value


def _without_nulls(obj: dict) -> dict:
    """`obj` without its null values: the API sends an explicit null for an absent field."""
    return {k: v for k, v in obj.items() if v is not None}


def _nested_rows(
    key: str, node: dsm.NestedNode, raw: object, root: str, source: str
) -> list[_Row]:
    """Rows of the nested `publisher` / `contact_point` entities and of the root's references."""
    items = _list(raw, key, source) if node.many else [raw]
    rows: list[_Row] = []
    for index, item in enumerate(items):
        item = _object(item, key, source)
        entity = f"{node.id_prefix}_{index}" if node.many else node.id_prefix
        json_type = _text(item.get("type"), f"{key}.type", source)
        if json_type not in node.types:
            raise ValueError(f"{source}: unsupported {key} type {json_type!r}")
        entity_type = node.types[json_type]
        extra = set(item) - {"type"} - node.fields.keys()
        if extra:
            raise ValueError(f"{source}: unmapped key(s) {sorted(extra)} in {key}")
        item = _without_nulls(item)
        rows.append(
            (root, dsm.DATA_SERVICE_TYPE, node.reference, entity, "bnode", None, None)
        )
        rows.append((entity, entity_type, dsm.RDF_TYPE, entity_type, "uri", None, None))
        for field, (prop, kind) in node.fields.items():
            if field in item:
                value = _text(item[field], f"{key}.{field}", source)
                rows.append((entity, entity_type, prop, value, kind, None, None))
    return rows


def _parse_dataservice_file(path: Path) -> tuple[str, list[_Row]]:
    """(resource_id, rows) of one standalone Data Service file.

    The record is the `result` object of the EDP API envelope. Its `id` is the resource id
    and every other key must be mapped or ignored in `dataservice_mapping`.
    """
    source = path.name
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source}: not valid JSON ({exc})") from exc
    record = _object(_object(doc, "the file", source).get("result"), "result", source)
    unknown = record.keys() - dsm.MAPPED_KEYS - dsm.IGNORED_KEYS
    if unknown:
        raise ValueError(
            f"{source}: unmapped key(s) {sorted(unknown)}; map or ignore them"
        )
    record = _without_nulls(record)
    if dsm.ID_KEY not in record:
        raise ValueError(f"{source}: record without id")
    root = _text(record[dsm.ID_KEY], "id", source)
    root_type = dsm.DATA_SERVICE_TYPE

    rows: list[_Row] = [(root, root_type, dsm.RDF_TYPE, root_type, "uri", None, None)]

    def add(prop: str, value: str, kind: str, lang: str | None = None) -> None:
        rows.append((root, root_type, prop, value, kind, None, lang))

    for key, prop in dsm.LANGUAGE_MAP_KEYS.items():
        for lang, text in _object(record.get(key, {}), key, source).items():
            add(prop, _text(text, f"{key}.{lang}", source), "literal", lang)
    for item in _list(record.get(dsm.KEYWORDS_KEY, []), dsm.KEYWORDS_KEY, source):
        item = _without_nulls(_object(item, dsm.KEYWORDS_KEY, source))
        label = _text(item.get("label"), "keyword label", source)
        language = item.get("language")
        if language is not None:
            language = _text(language, "keyword language", source)
        add(dsm.KEYWORDS_PROPERTY, label, "literal", language)
    for key, prop in dsm.URI_LIST_KEYS.items():
        for uri in _list(record.get(key, []), key, source):
            add(prop, _text(uri, key, source), "uri")
    for key, prop in dsm.RESOURCE_KEYS.items():
        if key in record:
            resource = _object(record[key], key, source).get("resource")
            add(prop, _text(resource, f"{key}.resource", source), "uri")
    for key, prop in dsm.RESOURCE_LIST_KEYS.items():
        for item in _list(record.get(key, []), key, source):
            resource = _object(item, key, source).get("resource")
            add(prop, _text(resource, f"{key}.resource", source), "uri")
    for key, node in dsm.NESTED_KEYS.items():
        if key in record:
            rows.extend(_nested_rows(key, node, record[key], root, source))
    return root, rows


def _check_resource_id(resource_id: str) -> None:
    """Reject an empty id or one with a path separator, which must not reach a path.

    Other characters unsafe in a file name are fine here: a Data Service `id` can contain `:`
    and is looked up through `safe_file_component`; a Dataset id with such a character cannot
    match a file, so it is simply not found.
    """
    if not resource_id or "/" in resource_id or "\\" in resource_id:
        raise ValueError(f"invalid resource id: {resource_id!r}")


def _dataset_files(directory: Path, resource_ids: list[str] | None) -> dict[str, Path]:
    """Dataset files to load by resource id: all of them, or those of the requested ids."""
    if resource_ids is None:
        paths = sorted(directory.glob(f"{_DATASET_PREFIX}*{_DATASET_SUFFIX}"))
        return {
            p.name.removeprefix(_DATASET_PREFIX).removesuffix(_DATASET_SUFFIX): p
            for p in paths
        }
    # A Dataset id is a file name, so one with unsafe characters cannot name a Dataset file.
    paths = {
        rid: directory / f"{_DATASET_PREFIX}{rid}{_DATASET_SUFFIX}"
        for rid in resource_ids
        if safe_file_component(rid) == rid
    }
    return {rid: p for rid, p in paths.items() if p.is_file()}


def _dataservice_files(
    directory: Path, resource_ids: list[str] | None
) -> dict[str, Path]:
    """Data Service files to load: all of them, or those of the requested ids.

    A Data Service file is named after its id with unsafe characters replaced, so a requested
    id selects `dataservice_{safe_file_component(id)}.json` if it exists. The result is keyed by
    requested id, or by file name when every file is wanted.
    """
    if resource_ids is None:
        paths = sorted(directory.glob(f"{_DATASERVICE_PREFIX}*{_DATASERVICE_SUFFIX}"))
        return {p.name: p for p in paths}
    paths = {
        rid: directory
        / f"{_DATASERVICE_PREFIX}{safe_file_component(rid)}{_DATASERVICE_SUFFIX}"
        for rid in resource_ids
    }
    return {rid: p for rid, p in paths.items() if p.is_file()}


def _replace_resource(
    conn: sqlite3.Connection,
    env: str,
    catalogue: str,
    resource_type: str,
    resource_id: str,
    source_file: str,
    rows: list[_Row],
    loaded_at: datetime,
) -> None:
    """Delete the resource's `resource` row (cascading to its values) and insert it again."""
    conn.execute(
        "DELETE FROM resource WHERE environment = ? AND catalogue = ?"
        " AND resource_type = ? AND resource_id = ?",
        (env, catalogue, resource_type, resource_id),
    )
    cur = conn.execute(
        "INSERT INTO resource (environment, catalogue, resource_type, resource_id, source_file,"
        " loaded_at) VALUES (?, ?, ?, ?, ?, ?)",
        (env, catalogue, resource_type, resource_id, source_file, loaded_at),
    )
    conn.executemany(
        "INSERT INTO property_value (resource_pk, entity_id, entity_type, property_uri, value,"
        " value_kind, datatype, lang) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(cur.lastrowid, *row) for row in rows],
    )


def _prune(
    conn: sqlite3.Connection,
    env: str,
    catalogue: str,
    resource_type: str,
    keep: set[str],
) -> int:
    """Delete the catalogue's resources of `resource_type` whose id is not in `keep`."""
    stored = conn.execute(
        "SELECT resource_id FROM resource WHERE environment = ? AND catalogue = ?"
        " AND resource_type = ?",
        (env, catalogue, resource_type),
    ).fetchall()
    gone = [
        (env, catalogue, resource_type, rid) for (rid,) in stored if rid not in keep
    ]
    conn.executemany(
        "DELETE FROM resource WHERE environment = ? AND catalogue = ?"
        " AND resource_type = ? AND resource_id = ?",
        gone,
    )
    return len(gone)


def load_catalogue(
    env: str,
    catalogue: str,
    *,
    resource_ids: Iterable[str] | None = None,
    conn: sqlite3.Connection | None = None,
    data_dir: Path = FETCHED_DATA_DIR,
) -> LoadSummary:
    """Load the fetched Dataset and Data Service files of `catalogue` into the database.

    Reads `dataset_*.jsonld` and `dataservice_*.json` in `{data_dir}/{env}/{catalogue}/`: all
    of them, or only those selected by `resource_ids` (a Dataset's file name without
    `dataset_` and `.jsonld`, or a Data Service's `id`). Each loaded file replaces its
    resource's previous rows. A full load (`resource_ids=None`) also deletes the catalogue's
    resources whose file is gone; a load with `resource_ids` deletes nothing else. An id
    without a file, an invalid id, a missing directory on a full load, or a malformed file
    raises, and everything done so far is rolled back.

    `conn` must come from `connect()` (the cascade needs foreign keys on) and must have no
    pending writes: the load commits on success and rolls back on error, which would commit or
    discard the caller's own uncommitted work too. It is left open. When `conn` is `None`, the
    default database is opened and closed here.
    """
    directory = Path(data_dir) / env / catalogue
    own_conn = conn is None
    if conn is None:
        conn = connect()
    resources = rows_count = null_type_entities = null_type_resources = 0
    try:
        ids = None if resource_ids is None else list(dict.fromkeys(resource_ids))
        for resource_id in ids or []:
            _check_resource_id(resource_id)
        if ids is None and not directory.is_dir():
            raise FileNotFoundError(f"catalogue directory not found: {directory}")
        dataset_files = _dataset_files(directory, ids)
        dataservice_files = _dataservice_files(directory, ids)
        for rid in ids or []:
            if rid not in dataset_files and rid not in dataservice_files:
                raise FileNotFoundError(
                    f"no Dataset or Data Service file for resource id {rid!r} in {directory}"
                )
        loaded_at = datetime.now(UTC)
        # One file at a time, so memory does not grow with the catalogue; a malformed file
        # raises here and the rollback below undoes the files already written.
        for rid, path in dataset_files.items():
            rows, null_entities = _parse_dataset_file(path)
            _replace_resource(
                conn, env, catalogue, "dataset", rid, path.name, rows, loaded_at
            )
            resources += 1
            rows_count += len(rows)
            null_type_entities += null_entities
            null_type_resources += bool(null_entities)
        service_files: dict[str, str] = {}  # resource id -> file it came from
        # Two requested ids can map to the same file: load each file once.
        for path in dict.fromkeys(dataservice_files.values()):
            rid, rows = _parse_dataservice_file(path)
            if rid in service_files:
                raise ValueError(
                    f"{path.name}: Data Service id {rid!r} is also in {service_files[rid]}"
                )
            service_files[rid] = path.name
            _replace_resource(
                conn, env, catalogue, "dataservice", rid, path.name, rows, loaded_at
            )
            resources += 1
            rows_count += len(rows)
        pruned = 0
        if ids is None:
            pruned = _prune(conn, env, catalogue, "dataset", set(dataset_files))
            pruned += _prune(conn, env, catalogue, "dataservice", set(service_files))
        conn.commit()
        if null_type_entities:
            logger.warning(
                "%s/%s: %d entities in %d resource(s) stored with entity_type NULL (@type missing"
                " or with several values; their types are in the rdf:type rows). Set the"
                " %s logger to DEBUG to list them.",
                env,
                catalogue,
                null_type_entities,
                null_type_resources,
                __name__,
            )
    except BaseException:
        conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()
    return LoadSummary(
        resources=resources,
        pruned=pruned,
        rows=rows_count,
        null_type_entities=null_type_entities,
    )
