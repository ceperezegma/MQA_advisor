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

#### Scenario: Only a non-English definition
- **WHEN** a concept has a `skos:definition` only in German
- **THEN** its definition is None

#### Scenario: English definition after a non-English one
- **WHEN** a concept has a German `skos:definition` followed by an English one
- **THEN** its definition is the English text

#### Scenario: Empty English definition
- **WHEN** a concept's English `skos:definition` is empty or whitespace-only
- **THEN** its definition is None, and a later English element with text is used instead

#### Scenario: Padded text is stripped
- **WHEN** a label or definition has leading or trailing whitespace
- **THEN** the returned text has none

### Requirement: Concepts must be well-formed and unique
The system SHALL raise an error naming the concept for a concept without a usable `rdf:about` URI or whose URI gives an empty code, for two concepts with the same code or the same URI, and for an `at:deprecated` value other than `"true"`, `"false"` or absent. It SHALL NOT guess or skip such a concept.

#### Scenario: Missing or empty URI
- **WHEN** a concept has no `rdf:about`, an empty one, or a URI ending in `/`
- **THEN** an error is raised naming the concept's English label

#### Scenario: Duplicate code
- **WHEN** two concepts have the same code
- **THEN** an error is raised naming the code

#### Scenario: Duplicate URI
- **WHEN** two concepts have the same URI
- **THEN** an error is raised naming the URI

#### Scenario: Invalid deprecated value
- **WHEN** a concept has `at:deprecated="True"` or `at:deprecated="1"`
- **THEN** an error is raised naming the concept

#### Scenario: Deprecated attribute absent or false
- **WHEN** a concept has no `at:deprecated` attribute or `at:deprecated="false"`
- **THEN** the concept is kept

### Requirement: Entries formatted as prompt lines
The system SHALL format the entries as one line per entry, `CODE — English label: English definition`, and as `CODE — English label` when the entry has no definition.

#### Scenario: Line with a definition
- **WHEN** an entry has a label and a definition
- **THEN** its line is `CODE — label: definition`

#### Scenario: Line without a definition
- **WHEN** an entry has no definition
- **THEN** its line is `CODE — label` with no colon and no trailing text

#### Scenario: Lines for the committed file
- **WHEN** the entries of the committed file are formatted
- **THEN** there are 14 lines, and the OP_DATPRO line is `OP_DATPRO — Provisional data`

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
The system SHALL raise a clear error when the vocabulary file cannot be read (missing, a directory, or any other operating-system error), is not well-formed XML (including an empty file), has no `owl:versionInfo` (including XML that is not a vocabulary), or yields no concepts, and SHALL NOT return an empty vocabulary.

#### Scenario: Missing file
- **WHEN** the vocabulary is read from a path that does not exist
- **THEN** an error is raised that names the path

#### Scenario: Malformed XML
- **WHEN** the vocabulary is read from a file that is not well-formed XML
- **THEN** an error is raised that names the file

#### Scenario: Empty file
- **WHEN** the vocabulary is read from a 0-byte file
- **THEN** an error is raised that names the file

#### Scenario: XML that is not a vocabulary
- **WHEN** the vocabulary is read from a well-formed XML file such as `<foo/>`
- **THEN** an error is raised that names the file

#### Scenario: Path is a directory
- **WHEN** the vocabulary is read from a directory path
- **THEN** an error is raised that names the path

#### Scenario: No concepts
- **WHEN** the vocabulary is read from a well-formed file that contains no usable concept
- **THEN** an error is raised and no empty vocabulary is returned

### Requirement: Load is logged once
The system SHALL log at INFO level, once per load and as a single message, the number of concepts read, the number of deprecated concepts skipped and the vocabulary version.

#### Scenario: Load log line
- **WHEN** the vocabulary is loaded from the committed file
- **THEN** exactly one INFO message states `14 data-theme concepts`, 0 deprecated skipped and version `20241211-0`
