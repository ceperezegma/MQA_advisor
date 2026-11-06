# dataservice-json-mapping Specification

## Purpose
Defines how a standalone Data Service JSON record (EDP API envelope, no JSON-LD context) is turned into property rows with full URIs, and guarantees that no key of the record is dropped silently.

## Requirements

### Requirement: Mapped keys become full-URI property rows
A Data Service record SHALL be read from the `result` object of its envelope. Each mapped key SHALL produce `property_value` rows whose `property_uri` is a full URI (prefixes `dct`, `dcat`, `foaf`, `vcard`, `rdf`, `dcatap` = `http://data.europa.eu/r5r/`): `title.<lang>` -> `dct:title` and `description.<lang>` -> `dct:description` (literal, `lang` from the key); `keywords[].label` -> `dcat:keyword` (literal, `lang` from the item's `language`); `endpoint_url[]` -> `dcat:endpointURL`; `endpoint_description[]` -> `dcat:endpointDescription`; `access_right.resource` -> `dct:accessRights`; `format.resource` -> `dct:format`; `license.resource` -> `dct:license`; `theme[].resource` -> `dcat:theme`; `hvd_category[].resource` -> `dcatap:hvdCategory`; `applicable_legislation[]` -> `dcatap:applicableLegislation`; `landing_page[].resource` -> `dcat:landingPage`; `serves_dataset[]` -> `dcat:servesDataset` (all of these URI-valued ones with `value_kind='uri'`). Languages SHALL be stored exactly as delivered.

#### Scenario: Multilingual title
- **WHEN** a record with `title` keys `eng`, or `es` and `sl`, is loaded
- **THEN** one `dct:title` literal row exists per key, with `lang` exactly `eng`, or `es` and `sl`

#### Scenario: Keywords carry their own language
- **WHEN** a keyword `{"label": "land cover", "language": "en"}` is loaded
- **THEN** a `dcat:keyword` literal row has value `land cover` and `lang` `en`

#### Scenario: Resource-valued keys
- **WHEN** a record has `access_right`, `format` and `license` objects with `resource` URIs
- **THEN** each yields one `uri` row under `dct:accessRights`, `dct:format`, `dct:license` holding the `resource` value, and their `id`/`label` are not stored

### Requirement: Root node and id
The record's `id` SHALL be the resource's `resource_id` and SHALL NOT be stored as a property. The root node SHALL be an entity whose `entity_id` is the `id` and whose `entity_type` is the full URI of `dcat:DataService`, with an `rdf:type` row of the same value.

#### Scenario: Root entity
- **WHEN** a Data Service with `id` `X` is loaded
- **THEN** `resource_id` is `X`, its root rows have `entity_type` `http://www.w3.org/ns/dcat#DataService`, and no row has the property `id`

### Requirement: Nested publisher and contact point are separate entities
`publisher` SHALL become a new entity with a synthetic `_:` id, typed `foaf:Organization` or `foaf:Agent` according to the JSON `type`, with `foaf:name`, `foaf:mbox` (from `email`) and `foaf:homepage` rows for the keys present, and the root SHALL reference it with a `dct:publisher` row of `value_kind='bnode'`. Each `contact_point[]` item SHALL become a new `_:` entity typed `vcard:Kind` with a `vcard:hasEmail` row, referenced from the root by a `dcat:contactPoint` row of `value_kind='bnode'`. A nested `type` outside `Organization`, `Agent` (publisher) or `Kind` (contact point) SHALL raise an error naming the file.

#### Scenario: Publisher reachable from root
- **WHEN** a record with a publisher `{"type": "Organization", "name": "N", "email": "mailto:a@b", "homepage": "https://h"}` is loaded
- **THEN** the root has a `dct:publisher` row whose value is the id of an entity of type `foaf:Organization` that has `foaf:name` `N`, `foaf:mbox` `mailto:a@b` and `foaf:homepage` `https://h`

#### Scenario: Two contact points
- **WHEN** a record has two `contact_point` items
- **THEN** two distinct `vcard:Kind` entities exist, each referenced by its own `dcat:contactPoint` row

### Requirement: No key is dropped silently
Every key of `result` SHALL be either mapped or in an explicit ignore list (`catalog`, `translation_meta`, `count`, `results`); the envelope key `status` is not a record key. A record holding any other key SHALL raise an error naming the file and the key. A file that is not valid JSON, has no `result` object, or has a mapped key of an unexpected shape SHALL raise an error naming the file. A key (or nested field) whose value is `null` SHALL be treated as absent, except that an unmapped key raises even when null.

#### Scenario: Fixture completeness
- **WHEN** the keys of every Data Service fixture under `tests/fixtures/` are checked
- **THEN** each is mapped or in the ignore list

#### Scenario: Unknown key
- **WHEN** a record holds a key such as `new_field`
- **THEN** loading raises an error naming the file and `new_field`

#### Scenario: Null is absent
- **WHEN** a record has `"license": null`, `"publisher": null` or a nested `"homepage": null`
- **THEN** no row is created for it and no error is raised

#### Scenario: Non-string keyword language
- **WHEN** a keyword's `language` is not a string
- **THEN** loading raises an error naming the file

#### Scenario: Ignored keys
- **WHEN** a record holds `catalog` and `translation_meta`
- **THEN** no row is created for them and no error is raised

### Requirement: Prefixes are validated at import
Every prefixed name in the mapping SHALL use a prefix of the module's prefix table; an undefined prefix SHALL make importing the module fail.

#### Scenario: Undefined prefix
- **WHEN** the mapping builder is given a name with a prefix not in the table
- **THEN** it raises an error naming the prefix
