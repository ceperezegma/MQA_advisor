## Purpose

Propose additional `dcat:theme` values from the EU data-theme vocabulary for one Dataset or standalone Data
Service, from its own title and description only, and hand the validated result to the caller (ADR-0017).

## ADDED Requirements

### Requirement: Read the resource's own metadata
The system SHALL read a resource's title, description and existing themes from `property_value`, using only the
rows of the resource's own entity. For a Dataset the own entity SHALL be the `dataset` entity whose id starts
with `http://data.europa.eu/88u/`; any other `dataset` entity of the same record (a related dataset) SHALL be
ignored. For a standalone Data Service the own entity SHALL be its single `dataservice` entity. When no such
entity, or more than one, exists, the resource SHALL be skipped with a warning and no model call.

#### Scenario: Dataset with a related dataset in its record
- **WHEN** a Dataset's record holds the entity `http://data.europa.eu/88u/dataset/X` and a related `dcat:Dataset`
  `https://example.org/other.html`, each with its own title and themes
- **THEN** only the title, description and themes of `http://data.europa.eu/88u/dataset/X` are used

#### Scenario: Dataset without an own entity
- **WHEN** a Dataset has no `dataset` entity whose id starts with `http://data.europa.eu/88u/`
- **THEN** the result is `skipped` with a warning naming the resource, and no call is made

#### Scenario: Dataset with two own entities
- **WHEN** a Dataset has two `dataset` entities whose ids start with `http://data.europa.eu/88u/`
- **THEN** the result is `skipped` with a warning naming the resource, and no call is made

### Requirement: Existing themes
An existing theme SHALL be the URI of a `dcat:theme` value of the own entity: the value of a `uri` row, or the
`@id` of a `node` row (a Dataset stores a theme as a `node`, a Data Service as a `uri`). Only existing themes
whose URI is in the vocabulary SHALL count toward the cap of 5 and be returned as existing themes; the same URI
twice SHALL count once. Out-of-vocabulary existing themes SHALL be ignored, never changed.

#### Scenario: Dataset node theme
- **WHEN** a Dataset has a `dcat:theme` `node` value whose `@id` is the `ENVI` authority URI
- **THEN** `ENVI` counts as an existing theme

#### Scenario: Data Service uri theme
- **WHEN** a Data Service has a `dcat:theme` `uri` value equal to the `TECH` authority URI
- **THEN** `TECH` counts as an existing theme

#### Scenario: Out-of-vocabulary existing themes only
- **WHEN** every existing theme of the resource is outside the vocabulary
- **THEN** the existing count is 0 and a call asking for up to 5 themes is made

### Requirement: Working language and model input
Tags `en` and `eng` SHALL both mean English. The working language SHALL be English when the title has an English
version, otherwise the title's tag that sorts first alphabetically (an untagged title is used only when no
tagged title exists). The title sent SHALL be the title in the working language. The description sent SHALL be
the description in the working language when one exists; otherwise the description in its own language (the
alphabetically first tag), prefixed with `(language: {tag}) `. An untagged description is sent without a prefix.
A resource with no title SHALL be skipped with a warning and no call; a resource with no description SHALL be
sent with an empty description.

#### Scenario: English title present
- **WHEN** the title exists in `fra` and `eng`, and the description in `fra` and `eng`
- **THEN** the English title and the English description are sent, without a prefix

#### Scenario: Non-English title
- **WHEN** the title exists only in `fra` and the description only in `fra`
- **THEN** the French title and the French description are sent, without a prefix

#### Scenario: Description in another language
- **WHEN** the title exists only in `eng` and the description only in `fra`
- **THEN** the description is sent as `(language: fra) ` followed by the French text

#### Scenario: Two title tags, none English
- **WHEN** the title exists in `fra` and `deu`
- **THEN** the working language is `deu` and the German title is sent

#### Scenario: No title
- **WHEN** the resource has no title
- **THEN** the result is `skipped` with a warning naming the resource, and no call is made

#### Scenario: No description
- **WHEN** the resource has a title and no description
- **THEN** the call is made with the title and an empty description

### Requirement: Theme call
When fewer than 5 existing themes count, the system SHALL call the active version of the `propose_themes` prompt
with the vocabulary entries, the title, the description, the existing in-vocabulary themes and the number of new
themes asked for (5 minus the existing count). When 5 or more count, no call SHALL be made and the result SHALL be
`skipped` without a warning.

#### Scenario: Cap already reached
- **WHEN** the resource has 5 in-vocabulary themes
- **THEN** no call is made and the result is `skipped` with no warning

#### Scenario: Remaining count in the prompt
- **WHEN** the resource has 2 in-vocabulary themes
- **THEN** the call asks for up to 3 additional themes and lists the 2 existing ones

### Requirement: Validation of the answer
Each returned code SHALL be kept only when it is exactly a vocabulary code; any other value SHALL be dropped with
a warning naming the resource and the value. Codes that are existing themes, or repeat a code already kept, SHALL
be dropped. When more codes remain than were asked for, the list SHALL be cut to that number, in the model's
order, with a warning. The kept codes SHALL be returned as authority URIs.

#### Scenario: Valid answer
- **WHEN** the model answers `["ENVI", "AGRI"]` for a resource with no theme
- **THEN** the result is `ok` with the `ENVI` and `AGRI` authority URIs and no warning

#### Scenario: Mixed valid and invalid codes
- **WHEN** the model answers `["ENVI", "Environment", "XXXX"]`
- **THEN** `ENVI` is kept and one warning is issued for each of `Environment` and `XXXX`

#### Scenario: Duplicate of an existing theme
- **WHEN** the resource already has `ENVI` and the model answers `["ENVI", "AGRI"]`
- **THEN** only `AGRI` is returned

#### Scenario: Over the cap
- **WHEN** the resource has 4 themes and the model answers `["AGRI", "ECON"]`
- **THEN** only `AGRI` is returned, with a warning that the list was cut

### Requirement: Retry on an empty result
When nothing valid is left after validation, the call SHALL be repeated, up to 3 retries (at most 4 calls). When
the last retry is still empty, the result SHALL be `error`, with no themes, and an ERROR SHALL be logged naming
the resource. A failed call (the API failing after its own retries, an answer not matching the schema, or a call
that cannot be made) SHALL end the attempt at once as an `error` result, without further retries.

#### Scenario: Third retry succeeds
- **WHEN** the model answers with only invalid codes three times and then with `["ENVI"]`
- **THEN** 4 calls are made and the result is `ok` with `ENVI`

#### Scenario: Retries exhausted
- **WHEN** the model answers with only invalid codes four times
- **THEN** exactly 4 calls are made and the result is `error` with no themes

#### Scenario: Call failure
- **WHEN** the call fails with an API error
- **THEN** the result is `error` with the failure message, and no further call is made

### Requirement: Result for the caller
The result SHALL hold: the resource, the status (`ok`, `skipped` or `error`), the new themes as authority URIs
(empty unless `ok`), the existing in-vocabulary themes as authority URIs, the prompt name, version, sha256 and
model of the call (absent when no call was made), the number of calls made, and the list of issues, each with a
severity (`warning` or `error`), the resource id and a message. Nothing SHALL be written to the database.

#### Scenario: Error result carries the issue
- **WHEN** the retries are exhausted
- **THEN** the result holds one `error` issue naming the resource, along with any `warning` issues of the attempts

#### Scenario: Nothing stored
- **WHEN** themes are proposed for a resource
- **THEN** the database has no new row
