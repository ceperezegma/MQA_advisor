## ADDED Requirements

### Requirement: Connection creates the proposal table
`connect(path)` SHALL also create, when they do not exist, the table `proposed_property_value` and the index `proposed_property_value_resource (resource_pk, property_uri)`. The table SHALL have the columns `resource_pk` (not null, referencing `resource(resource_pk)` with `ON DELETE CASCADE`), `entity_id`, `entity_kind` (only `dataset` or `dataservice`), `property_uri`, `value`, `value_kind` (only `literal` or `uri`), `lang` (nullable), `prompt_name`, `prompt_version` (integer), `prompt_sha256`, `model` and `proposed_at` (a UTC timestamp); every column except `lang` SHALL be not null. Creating it SHALL NOT change the existing tables, views or indexes, and SHALL NOT change what `connect()` does with a database created with the older layout.

#### Scenario: Fresh path
- **WHEN** `connect()` is called on a path where no file exists
- **THEN** the file has `proposed_property_value` and the index `proposed_property_value_resource` in addition to the existing tables

#### Scenario: Database that already has the other tables
- **WHEN** `connect()` is called on a database that holds `resource` and `property_value` rows but no `proposed_property_value` table
- **THEN** the proposal table is created and the existing rows are unchanged

#### Scenario: Repeated call
- **WHEN** `connect()` is called again on the same path after proposals were stored
- **THEN** no error is raised and the proposals are unchanged

#### Scenario: Invalid entity_kind
- **WHEN** a proposal row is inserted with an `entity_kind` other than `dataset` or `dataservice` (for example `distribution`)
- **THEN** the insert fails with an integrity error

#### Scenario: Invalid value_kind
- **WHEN** a proposal row is inserted with a `value_kind` other than `literal` or `uri` (for example `node`)
- **THEN** the insert fails with an integrity error

#### Scenario: Orphan proposal
- **WHEN** a proposal row is inserted with a `resource_pk` that has no `resource` row
- **THEN** the insert fails with an integrity error

### Requirement: Proposals of one resource are replaced atomically
The module SHALL provide a function that takes a connection, a `resource_pk` and a list of proposals (entity id, entity kind, property URI, value, value kind, language or none, prompt name, prompt version, prompt SHA-256, model, and a timezone-aware UTC time), and in one transaction deletes every `proposed_property_value` row of that resource and inserts the given ones. It SHALL commit on success and, on any error, roll back so that the previous proposals of the resource remain. Before writing anything it SHALL check every proposal in the list: a proposal whose `value_kind` is `uri` SHALL have no `lang`, and one whose `value_kind` is `literal` SHALL have a non-empty `lang`; on a violation it SHALL raise `ValueError` that names the offending proposal and leave the resource's previous proposals untouched. It SHALL touch no other resource's proposals and no other table. Calling it twice with the same input SHALL leave the same rows, and calling it with an empty list SHALL leave the resource with no proposals. As for `load_catalogue`, the connection passed in SHALL have no pending writes. Theme proposals are stored by the caller as `uri` rows holding the authority URI and no language, and keyword proposals as `literal` rows with a `lang`; apart from the pairing check above, the function stores what it is given and does not interpret it. The table itself SHALL NOT have a CHECK constraint on the pairing.

#### Scenario: First write
- **WHEN** the function is called for a resource with no proposals and three proposals
- **THEN** the table holds exactly those three rows for that resource, with every given field stored as given

#### Scenario: Same input twice
- **WHEN** the function is called twice for one resource with the same proposals
- **THEN** the resource's rows after the second call equal the rows after the first

#### Scenario: Replacement
- **WHEN** the function is called a second time for one resource with different proposals
- **THEN** only the second set remains for that resource

#### Scenario: Empty list
- **WHEN** the function is called with an empty list for a resource that has proposals
- **THEN** the resource has no proposals afterwards

#### Scenario: Other resources untouched
- **WHEN** the function replaces the proposals of one resource
- **THEN** the proposals of every other resource are unchanged

#### Scenario: Failure keeps the previous proposals
- **WHEN** the function is called with a list in which a later proposal violates a constraint (for example a bad `entity_kind`)
- **THEN** an error is raised, none of the new proposals is stored and the resource's previous proposals remain

#### Scenario: uri row with a language
- **WHEN** the function is called with a list in which one proposal has `value_kind` `uri` and a `lang`
- **THEN** a `ValueError` naming that proposal is raised, nothing is written and the resource's previous proposals remain

#### Scenario: Keyword literal without a language
- **WHEN** the function is called with a list in which one proposal has `value_kind` `literal` and no `lang` (or an empty one)
- **THEN** a `ValueError` naming that proposal is raised, nothing is written and the resource's previous proposals remain

#### Scenario: Violation after valid proposals
- **WHEN** the offending proposal is not the first of the list
- **THEN** none of the proposals before it is stored either, and the previous proposals remain

#### Scenario: Naive timestamp
- **WHEN** a proposal's time is a naive `datetime`
- **THEN** a `ValueError` is raised and the previous proposals remain

### Requirement: Proposals can be read back
The module SHALL provide a function that returns the stored proposals of one resource, with all the fields given when they were written, in a stable order. The time SHALL come back as an equal timezone-aware `datetime`. A resource with no proposals SHALL give an empty list.

#### Scenario: Round trip
- **WHEN** proposals are stored for a resource and then read
- **THEN** the returned proposals equal the stored ones, including `lang` (none for a theme, the tag for a keyword) and the timezone-aware time

#### Scenario: No proposals
- **WHEN** the proposals of a resource that has none are read
- **THEN** an empty list is returned

### Requirement: Reloading a resource removes its proposals
Whenever `load_catalogue` replaces a resource, or deletes it because its file is gone on a full load, the proposals of that resource SHALL be removed with it. Proposals of resources it does not replace or delete, of other catalogues and of the other environment SHALL be unchanged. After the load commits, it SHALL log at INFO one message with the number of proposals removed, always, also when that number is zero, and `LoadSummary` SHALL carry the number (zero when none). When the load fails and rolls back, the proposals SHALL remain and nothing SHALL be logged about removal.

#### Scenario: Full reload removes the catalogue's proposals
- **WHEN** a catalogue whose resources have proposals is loaded again in full
- **THEN** no proposal remains for those resources, the summary and the INFO message give the number removed, and the proposals of another catalogue are unchanged

#### Scenario: Partial reload
- **WHEN** only some resources of a catalogue are reloaded through `resource_ids`
- **THEN** the proposals of those resources are removed and those of the other resources of the catalogue remain

#### Scenario: Resource whose file is gone
- **WHEN** a resource's file is deleted from the directory and the catalogue is fully loaded
- **THEN** the resource and its proposals are gone

#### Scenario: Zero removed is logged
- **WHEN** a catalogue with no proposals is loaded
- **THEN** the summary reports zero removed and the INFO message states that zero proposals were removed

#### Scenario: Failed load
- **WHEN** a load raises an error after it deleted a resource that had proposals
- **THEN** the transaction is rolled back, the proposals are still there and no removal message is logged

### Requirement: Module documentation covers the proposal table
The module docstring of `catalogue_db.py` SHALL mention `proposed_property_value`, state that it references `resource` with `ON DELETE CASCADE`, and say that a reload deletes the proposals of the resources it replaces.

#### Scenario: Docstring
- **WHEN** the module docstring is read
- **THEN** it names `proposed_property_value` and says reloading removes proposals
