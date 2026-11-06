# manifest-generation Specification

## Purpose
Produces a catalogue's manifest (the list of Datasets and Dataservices to fetch) from the selected environment's SPARQL endpoint, so no manual Excel step is needed and a missing or stale manifest never blocks a Test Run.

## Requirements

### Requirement: Manifest generated from the environment's SPARQL endpoint
Given a catalogue name, the system SHALL retrieve every Dataset and Dataservice that the catalogue `http://data.europa.eu/88u/catalogue/{catalogue}` lists, by querying the SPARQL endpoint of the selected environment (`PPE` or `PROD`). `PROD` SHALL be queried at `https://data.europa.eu/sparql` without logging in; `PPE` SHALL be queried at `https://ppe.data.europa.eu/sparql` through the authenticated PPE session. The query SHALL be a versioned file in the codebase, parameterised by the catalogue name, and SHALL return each resource's URI and its type (`dcat:Dataset` or `dcat:DataService`) in a stable order.

#### Scenario: Generating a manifest on production
- **WHEN** a manifest is generated for catalogue `europol` with the environment `PROD`
- **THEN** the system queries `https://data.europa.eu/sparql` for the resources of `http://data.europa.eu/88u/catalogue/europol`, with no login

#### Scenario: Generating a manifest on PPE
- **WHEN** a manifest is generated for catalogue `europol` with the environment `PPE`
- **THEN** the system queries `https://ppe.data.europa.eu/sparql` through the authenticated PPE session

#### Scenario: The catalogue name is the catalogue id
- **WHEN** a manifest is generated for catalogue `nsip-lu`
- **THEN** the query targets `http://data.europa.eu/88u/catalogue/nsip-lu`

### Requirement: Manifest workbook content
The system SHALL write the manifest to `catalogues_to_fetch/{catalogue}.xlsx` as one sheet whose header row is `resource_uri`, `type_uri`, `type`, `resource_id`, with one row per resource in the order the query returned them. `resource_uri` and `type_uri` SHALL be the query's resource URI and type URI. All four columns SHALL hold plain values (no formulas), so that the manifest can be read without a spreadsheet application recalculating it. An existing manifest for the same catalogue SHALL be overwritten.

#### Scenario: Rows follow the query's order
- **WHEN** the query returns N resources
- **THEN** the manifest has a header row and N data rows, in the query's order, each with the four columns filled

#### Scenario: The manifest is readable by the manifest loader
- **WHEN** a manifest has been generated
- **THEN** the Catalogue Manifest Loading requirement of `ppe-ingestion` reads all N rows from it with no spreadsheet recalculation step

#### Scenario: An existing manifest is overwritten
- **WHEN** a manifest is generated for a catalogue whose `catalogues_to_fetch/{catalogue}.xlsx` already exists
- **THEN** the file's previous content is replaced by the newly generated rows

### Requirement: Manifest type and resource id derivation
The system SHALL derive `type` from `type_uri`: `dataservice` when `type_uri` is `http://www.w3.org/ns/dcat#DataService`, `dataset` when it is `http://www.w3.org/ns/dcat#Dataset`, and an empty value otherwise. The system SHALL derive `resource_id` from `resource_uri` as the text after `88u/service/` if present, otherwise the text after `88u/dataset/` if present, otherwise an empty value; in all cases the id SHALL be cut at the first `~`, discarding it and everything after it.

#### Scenario: A Dataset URI
- **WHEN** `resource_uri` is `http://data.europa.eu/88u/dataset/annual-report-2004` and `type_uri` is `http://www.w3.org/ns/dcat#Dataset`
- **THEN** `type` is `dataset` and `resource_id` is `annual-report-2004`

#### Scenario: A Dataservice URI
- **WHEN** `resource_uri` is `http://data.europa.eu/88u/service/my-service` and `type_uri` is `http://www.w3.org/ns/dcat#DataService`
- **THEN** `type` is `dataservice` and `resource_id` is `my-service`

#### Scenario: A resource id with a tilde suffix
- **WHEN** `resource_uri` is `http://data.europa.eu/88u/dataset/some-id~~abc`
- **THEN** `resource_id` is `some-id`

#### Scenario: An unknown type or URI prefix
- **WHEN** `type_uri` is neither `dcat:Dataset` nor `dcat:DataService`, or `resource_uri` contains neither `88u/service/` nor `88u/dataset/`
- **THEN** the corresponding `type` or `resource_id` is empty

### Requirement: Manifest generation failure is fatal and explained
The system SHALL treat any of these as a manifest generation failure: a network error reaching the SPARQL endpoint, a non-success HTTP status, a response that is not valid SPARQL JSON results, or a query that returns zero resources. On a failure the system SHALL write no manifest and no other file, SHALL leave any existing manifest for that catalogue unchanged, and SHALL raise an error whose message names the catalogue, the environment, the endpoint URL and the cause, with wording specific to each kind of failure. A zero-resource failure SHALL suggest checking the catalogue name. There SHALL be no fallback to a previously generated manifest.

#### Scenario: The endpoint returns an HTTP error
- **WHEN** the SPARQL endpoint answers with a non-success status
- **THEN** the system raises an error naming the catalogue, environment, endpoint and the HTTP status, and writes no file

#### Scenario: The endpoint cannot be reached
- **WHEN** a network error prevents reaching the SPARQL endpoint
- **THEN** the system raises an error naming the catalogue, environment, endpoint and that it could not be reached, and writes no file

#### Scenario: The response is not SPARQL JSON
- **WHEN** the endpoint answers with a body that is not valid SPARQL JSON results
- **THEN** the system raises an error saying the response could not be understood, and writes no file

#### Scenario: Zero resources
- **WHEN** the query returns no resources (for example a misspelled catalogue name)
- **THEN** the system raises an error saying no resources were found for the catalogue and suggesting the name be checked, and writes no file

#### Scenario: A failed generation leaves the existing manifest unchanged
- **WHEN** generation fails for a catalogue that already has a manifest
- **THEN** the existing manifest file is byte-for-byte unchanged and no temporary file is left behind

### Requirement: Manifest generation can run on its own
The repository SHALL include a manual script under `tests/` that generates the manifest for one configured catalogue and environment without starting ingestion, scoring or Reference Result fetching.

#### Scenario: Running only manifest generation
- **WHEN** the manual script is run for a catalogue and environment
- **THEN** it generates `catalogues_to_fetch/{catalogue}.xlsx`, prints the written path, and performs no ingestion, scoring or Reference Result fetch
