## Purpose

Read the committed EU data-theme vocabulary (`vocabularies/data-theme-skos.rdf`) so the theme proposal feature can show the allowed themes to the model and validate the codes it answers with.

## ADDED Requirements

### Requirement: Data-theme entries from the committed vocabulary file
The system SHALL read `vocabularies/data-theme-skos.rdf` and return one entry per non-deprecated concept, each carrying its authority URI, its code, its English `skos:prefLabel` and its English `skos:definition`. The definition SHALL be absent (None) when the concept has no English definition. The system SHALL also return the file's version, read from `owl:versionInfo`.

#### Scenario: The committed file yields the 14 data themes
- **WHEN** the vocabulary is read from the committed file
- **THEN** the codes are exactly AGRI, ECON, EDUC, ENER, ENVI, GOVE, HEAL, INTR, JUST, OP_DATPRO, REGI, SOCI, TECH, TRAN

#### Scenario: Version of the committed file
- **WHEN** the vocabulary is read from the committed file
- **THEN** the version is `20241211-0`

#### Scenario: An entry carries its URI, code, label and definition
- **WHEN** the entry for ECON is read
- **THEN** its URI is `http://publications.europa.eu/resource/authority/data-theme/ECON`, its label is `Economy and finance`, and its definition is the English `skos:definition` text

#### Scenario: A concept without an English definition
- **WHEN** the entry for OP_DATPRO is read
- **THEN** its English label is present and its definition is None

#### Scenario: A deprecated concept is skipped
- **WHEN** the file contains a concept with `at:deprecated="true"`
- **THEN** that concept is not among the returned entries

### Requirement: Entries formatted as prompt lines
The system SHALL format the entries as one line per entry, `CODE — English label: English definition`, and as `CODE — English label` when the entry has no definition.

#### Scenario: Line with a definition
- **WHEN** an entry has a label and a definition
- **THEN** its line is `CODE — label: definition`

#### Scenario: Line without a definition
- **WHEN** an entry has no definition
- **THEN** its line is `CODE — label` with no colon and no trailing text

### Requirement: Code to URI lookup rejects unknown codes
The system SHALL map a data-theme code to its authority URI, and SHALL return nothing for a code that is not in the vocabulary. The match is exact and case-sensitive, and the system SHALL NOT guess, normalize or correct a code.

#### Scenario: Known code
- **WHEN** the code `TECH` is looked up
- **THEN** the result is `http://publications.europa.eu/resource/authority/data-theme/TECH`

#### Scenario: Unknown code
- **WHEN** the code `XXXX` is looked up
- **THEN** nothing is returned

#### Scenario: Lowercase code
- **WHEN** the code `tech` is looked up
- **THEN** nothing is returned

### Requirement: A missing or malformed vocabulary file fails loudly
The system SHALL raise a clear error when the vocabulary file is missing, is not well-formed XML, or yields no concepts, and SHALL NOT return an empty vocabulary.

#### Scenario: Missing file
- **WHEN** the vocabulary is read from a path that does not exist
- **THEN** an error is raised that names the path

#### Scenario: Malformed XML
- **WHEN** the vocabulary is read from a file that is not well-formed XML
- **THEN** an error is raised that names the file

#### Scenario: No concepts
- **WHEN** the vocabulary is read from a well-formed file that contains no usable concept
- **THEN** an error is raised and no empty vocabulary is returned

### Requirement: Load is logged once
The system SHALL log at INFO level, once per load, the number of concepts read and the vocabulary version.

#### Scenario: Load log line
- **WHEN** the vocabulary is loaded from the committed file
- **THEN** one INFO message states 14 concepts and version `20241211-0`
