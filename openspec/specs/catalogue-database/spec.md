# catalogue-database Specification

## Purpose
Defines the SQLite database that holds the properties of every fetched catalogue record, its connection and its schema, so scoring and other consumers can read records with plain SQL.

## Requirements

### Requirement: Connection creates the schema idempotently
`connect(path)` SHALL return a `sqlite3.Connection` to the database file at `path` (default `fetched_catalogues_data/mqa.sqlite`), creating the `resource` and `property_value` tables and the indexes `property_value_entity (resource_pk, entity_id)`, `property_value_property (resource_pk, property_uri)` and `property_value_type (resource_pk, entity_type)` when they do not exist. Calling it again on the same path SHALL change nothing and raise nothing.

#### Scenario: Fresh path
- **WHEN** `connect()` is called on a path where no file exists
- **THEN** the file is created with both tables and all three indexes

#### Scenario: Repeated call
- **WHEN** `connect()` is called a second time on the same path
- **THEN** no error is raised and existing rows and schema are unchanged

### Requirement: Foreign keys are enforced on every connection
Every connection returned by `connect()` SHALL have `PRAGMA foreign_keys` set to ON.

#### Scenario: Pragma on new connection
- **WHEN** a connection is obtained from `connect()`, including for an already-existing file
- **THEN** `PRAGMA foreign_keys` returns 1

#### Scenario: Orphan property row
- **WHEN** a `property_value` row is inserted with a `resource_pk` that has no `resource` row
- **THEN** the insert fails with an integrity error

### Requirement: Deleting a resource cascades to its property values
`property_value.resource_pk` SHALL reference `resource(resource_pk)` with `ON DELETE CASCADE`.

#### Scenario: Cascade scoped to one resource
- **WHEN** one of two resources is deleted
- **THEN** all its `property_value` rows are deleted and the other resource's rows remain

### Requirement: Resource identity and allowed values
The `resource` table SHALL hold `resource_pk`, `environment`, `catalogue`, `resource_type`, `resource_id`, `source_file` and `loaded_at` (`TIMESTAMP`, exposed as a Python `datetime`), all `NOT NULL`, and SHALL be unique on `(environment, catalogue, resource_type, resource_id)`. `resource_type` SHALL be restricted to `dataset` or `dataservice`. `property_value` SHALL hold `resource_pk`, `entity_id`, `entity_type`, `property_uri`, `value`, `value_kind`, `datatype` and `lang`, with `value_kind` restricted to `literal`, `uri` or `bnode`.

#### Scenario: Invalid resource_type
- **WHEN** a `resource` row with `resource_type` outside `dataset`/`dataservice` is inserted
- **THEN** the insert fails with an integrity error

#### Scenario: Invalid value_kind
- **WHEN** a `property_value` row with `value_kind` outside `literal`/`uri`/`bnode` is inserted
- **THEN** the insert fails with an integrity error

#### Scenario: Duplicate resource
- **WHEN** two `resource` rows share environment, catalogue, resource_type and resource_id
- **THEN** the second insert fails with an integrity error

#### Scenario: Naive loaded_at
- **WHEN** a naive (timezone-less) `datetime` is stored in `loaded_at`
- **THEN** the insert fails with a `ValueError`

#### Scenario: loaded_at round-trip
- **WHEN** a timezone-aware UTC `datetime` is stored in `loaded_at`
- **THEN** reading it back returns an equal timezone-aware `datetime`, not a string

### Requirement: Database file is not tracked by git
The default database file SHALL be covered by `.gitignore` so that creating it leaves `git status` clean.

#### Scenario: File ignored
- **WHEN** `connect()` creates `fetched_catalogues_data/mqa.sqlite`
- **THEN** `git status` does not list it

### Requirement: Dataset files are loaded into resource and property_value
`load_catalogue(env, catalogue, *, resource_ids=None, conn=None, data_dir)` SHALL read the `dataset_*.jsonld` and `dataservice_*.json` files in `{data_dir}/{env}/{catalogue}/` (every file when `resource_ids` is `None`, otherwise only the files whose resource id is listed) and insert one `resource` row per file (`resource_type='dataset'` with `resource_id` = the file name without the `dataset_` prefix and `.jsonld` suffix, or `resource_type='dataservice'` with `resource_id` = the record's `id`; `source_file`; `loaded_at` as a timezone-aware UTC `datetime`) and, for every node of a Dataset's `@graph` or of a Data Service record (as defined by the `dataservice-json-mapping` capability), one `property_value` row per property value. Other files in the directory SHALL be ignored (not parsed, not an error). A listed resource id SHALL select the Dataset file with that id and/or the Data Service file `dataservice_{id with characters unsafe in a file name replaced by -}.json`. When `conn` is `None` it SHALL open the default database with `connect()` and close it before returning. It SHALL return a `LoadSummary`.

#### Scenario: Round trip
- **WHEN** a Dataset file is loaded
- **THEN** the (entity, property, value, kind, datatype, lang) tuples read back from `property_value` equal the tuples derived directly from the source file

#### Scenario: Whole graph stored
- **WHEN** a Dataset file with Distributions and an embedded Data Service is loaded
- **THEN** rows exist for the Dataset, each Distribution, the embedded Data Service and every other node in `@graph`

#### Scenario: Data Service files loaded
- **WHEN** the directory contains `dataset_*.jsonld` and `dataservice_*.json` files
- **THEN** both kinds are loaded in one transaction, each Data Service with `resource_type='dataservice'` and `resource_id` equal to its `id`

#### Scenario: Data Service failure rolls back Datasets
- **WHEN** a `dataservice_*.json` file is malformed in a mixed directory
- **THEN** an error is raised and no Dataset or Data Service rows from this load remain; the previous data is unchanged

#### Scenario: Data Service files ignored
- **WHEN** the directory also contains files matching neither `dataset_*.jsonld` nor `dataservice_*.json`
- **THEN** they are not parsed and raise no error; `dataservice_*.json` files are no longer among the ignored ones

#### Scenario: Differing embedded copies
- **WHEN** two Dataset files embed `https://data.gov.lt/datasets/2916/` with different content
- **THEN** each copy's rows are stored under its own Dataset's `resource_pk`, unmerged, and the standalone Data Service record is stored as its own `dataservice` resource, unlinked

### Requirement: Property URIs and values follow the file's context
Each property key and each datatype SHALL be stored as a full URI, expanded with the file's `@context`. Values SHALL be stored as: plain string -> `literal` with `lang` NULL; `{@value, @language}` -> `literal` with `lang` exactly as delivered; `{@value, @type}` -> `literal` with `datatype` as full URI; `{@id}` -> `uri`, or `bnode` when the id starts with `_:`; native JSON number or boolean -> `literal` with the matching `xsd` datatype. References SHALL be stored as plain values with no check that the target exists. A prefix absent from the `@context` SHALL raise an error naming the file.

#### Scenario: Language kept as delivered
- **WHEN** literals tagged `eng`, `es` and `el-t-en-t0-mtec` are loaded
- **THEN** `lang` holds exactly those tags, and an untagged literal has `lang` NULL

#### Scenario: Typed literal
- **WHEN** a value `{"@value": "...", "@type": "xsd:dateTime"}` is loaded
- **THEN** `datatype` is the full `http://www.w3.org/2001/XMLSchema#dateTime` URI

#### Scenario: Blank node reference
- **WHEN** a value `{"@id": "_:b1"}` is loaded
- **THEN** its `value_kind` is `bnode`

#### Scenario: Native number
- **WHEN** a native JSON number is loaded
- **THEN** it is stored as a `literal` with an `xsd` numeric datatype

#### Scenario: Undefined prefix
- **WHEN** a file uses a prefix not declared in its `@context`
- **THEN** loading raises an error that names the file

#### Scenario: Embedded node with properties
- **WHEN** a value is an object with `@id` and other keys (an embedded node instead of a reference)
- **THEN** loading raises an error that names the file, rather than dropping the extra properties

#### Scenario: Non-string id or type
- **WHEN** a node's `@id`, a `@type` entry, or a value object's `@id` or `@type` is not a string
- **THEN** loading raises an error that names the file

### Requirement: entity_type is the single expanded @type
`entity_type` SHALL be the node's `@type` expanded to a full URI when it has exactly one value. When `@type` is missing or has more than one value, `entity_type` SHALL be NULL, a warning naming the resource and the entity SHALL be logged, and the load SHALL continue. Every `@type` value SHALL also be stored as an `rdf:type` row with `value_kind='uri'`.

#### Scenario: Single type
- **WHEN** a Dataset or Distribution node is loaded
- **THEN** its `entity_type` is the full `dcat:Dataset` / `dcat:Distribution` URI

#### Scenario: Multiple types
- **WHEN** a node typed `["foaf:Agent", "foaf:Organization"]` (in either order) is loaded
- **THEN** its `entity_type` is NULL, a warning is logged, and both types appear as `rdf:type` rows

#### Scenario: No type
- **WHEN** a node without `@type` is loaded
- **THEN** its `entity_type` is NULL and a warning is logged

### Requirement: Re-loading replaces per resource, atomically
`load_catalogue` SHALL, in one transaction, replace each resource it loads: delete the existing `resource` row with the same (env, catalogue, resource_type, resource_id) (cascading to its `property_value` rows) and insert the new rows. Resources not being loaded SHALL be left untouched. When `resource_ids` is `None` (full load), it SHALL also delete the catalogue's `dataset` and `dataservice` resources whose file is no longer in the directory, so the database matches the directory afterwards; when `resource_ids` is given, it SHALL delete nothing else. A listed id with no Dataset file and no Data Service file, or one that is empty or contains a path separator (`/` or `\`), SHALL raise an error before anything is written; any other id is looked up as given for a Dataset (only when it is safe in a file name) and with unsafe characters replaced by `-` for a Data Service, because a Data Service `id` can contain `:`. The connection passed in SHALL have no pending writes, because the load commits or rolls back the whole connection. Any error SHALL roll the transaction back, leaving the previous data unchanged. Loading SHALL NOT touch other catalogues or the other environment.

#### Scenario: Loaded twice
- **WHEN** the same catalogue is loaded twice
- **THEN** row counts in `resource` and `property_value` are identical after both loads and no `property_value` row lacks its `resource`

#### Scenario: Changed record replaced
- **WHEN** a Dataset or Data Service file changes and the catalogue is loaded again
- **THEN** that resource has only its new values and no leftover rows from the old file

#### Scenario: Removed record pruned on full load
- **WHEN** a Dataset or Data Service file is deleted from the directory and the catalogue is fully loaded again
- **THEN** that resource has no `resource` or `property_value` rows and the other resources are intact

#### Scenario: Prune leaves Data Service resources alone
- **WHEN** a full load runs and a Data Service file is still in the directory
- **THEN** its `dataservice` resource is re-loaded (not pruned), while a `dataservice` resource of another catalogue is unchanged

#### Scenario: Single-resource refresh
- **WHEN** `load_catalogue` is called with `resource_ids` naming one Dataset or one Data Service
- **THEN** only that resource's rows are replaced, all other resources keep their rows (even ones whose file is missing), and nothing is pruned

#### Scenario: Unknown resource id
- **WHEN** `resource_ids` names an id with neither a Dataset file nor a Data Service file in the directory
- **THEN** an error is raised and the database is unchanged

#### Scenario: Unsafe resource id
- **WHEN** `resource_ids` contains an empty id, or one with a path separator such as `../../other/dataset_x`
- **THEN** an error is raised, no file outside the catalogue directory is read, and the database is unchanged

#### Scenario: Data Service id with a colon
- **WHEN** `resource_ids` names a Data Service whose `id` contains `:` (such as `https:-data.gov.lt-datasets-2916-`)
- **THEN** its file `dataservice_https--data.gov.lt-datasets-2916-.json` is loaded and no error is raised

#### Scenario: Other unsafe characters
- **WHEN** `resource_ids` contains an id such as `x*` that matches no Dataset or Data Service file
- **THEN** an unknown-id error is raised and the database is unchanged

#### Scenario: Duplicate ids
- **WHEN** `resource_ids` lists the same id twice, or two ids that select the same file
- **THEN** that resource is loaded and counted once

#### Scenario: Two files with one Data Service id
- **WHEN** two `dataservice_*.json` files in a full load hold the same `id`
- **THEN** an error naming both files is raised and the database is unchanged

#### Scenario: Malformed file mid-load
- **WHEN** one file in the directory is malformed
- **THEN** an error is raised and the rows from before the load are unchanged

#### Scenario: Isolation
- **WHEN** catalogue A is loaded
- **THEN** rows of catalogue B and of the other environment are unchanged

### Requirement: Load summary
`LoadSummary` SHALL report the number of resources loaded, the number of resources pruned, the number of `property_value` rows inserted, and the number of entities with a NULL `entity_type`.

#### Scenario: Summary counts
- **WHEN** a catalogue containing the multi-typed nsip-lu agent and contact nodes is loaded
- **THEN** the summary's resource and row counts match the database and its NULL-type count equals the number of such entities

### Requirement: Connection creates the retrieval views
`connect()` SHALL create, when missing, the views `catalogue_resources`, `dataset_distributions` and `dataset_embedded_services`. `catalogue_resources` SHALL return `environment, catalogue, resource_type, resource_id, resource_pk` for every Dataset and standalone Data Service in `resource`; embedded Data Services SHALL NOT be listed. `dataset_distributions` SHALL return `resource_pk, distribution_id` from the values of `dcat:distribution` of each Dataset. `dataset_embedded_services` SHALL return `resource_pk, distribution_id, service_id` through each Distribution's `dcat:accessService` within the same resource. Each view SHALL return no rows, not an error, when there is nothing to return.

#### Scenario: Catalogue resources
- **WHEN** `catalogue_resources` is queried for a loaded catalogue
- **THEN** it returns exactly the catalogue's Datasets plus standalone Data Services

#### Scenario: Dataset with Distributions and an embedded service
- **WHEN** `dataset_distributions` and `dataset_embedded_services` are queried for a Dataset with several Distributions and an embedded Data Service
- **THEN** they return the expected Distribution ids and the (distribution, service) pairs

#### Scenario: Dataset without them
- **WHEN** the views are queried for a Dataset with no Distributions or embedded services
- **THEN** they return no rows for it

#### Scenario: Same embedded service in two Datasets
- **WHEN** two Datasets embed `https://data.gov.lt/datasets/2916/`
- **THEN** `dataset_embedded_services` returns it twice, once for each Dataset's `resource_pk`

#### Scenario: Empty database
- **WHEN** any view is queried before anything is loaded
- **THEN** it returns an empty result

#### Scenario: Repeated connect
- **WHEN** `connect()` is called again on the same file
- **THEN** no error is raised
