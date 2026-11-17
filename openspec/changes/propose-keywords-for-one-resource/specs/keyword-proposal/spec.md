## Purpose

Propose additional `dcat:keyword` values for one Dataset or standalone Data Service from its title, description and
effective themes, and hand the validated result to the caller (ADR-0017).

## ADDED Requirements

### Requirement: Existing keywords and the cap
The system SHALL read the `dcat:keyword` literal values of the resource's own entity (any language). When 10 or
more non-blank keywords exist, no call SHALL be made and the result SHALL be `skipped` without a warning.
Otherwise the call SHALL ask for between 1 and (10 minus the existing count) new keywords and SHALL list the
existing keywords so they are not repeated. A resource with no own entity or no title SHALL be skipped with a
warning and no call, as for themes.

#### Scenario: Cap reached
- **WHEN** the resource has 10 keywords
- **THEN** no call is made and the result is `skipped` with no warning

#### Scenario: Keywords in another language count
- **WHEN** the resource has 6 English and 4 French keywords
- **THEN** no call is made

#### Scenario: Remaining count
- **WHEN** the resource has 3 keywords
- **THEN** the call asks for up to 7 additional keywords and lists the 3 existing ones

### Requirement: Theme context
The keyword call SHALL receive the labels of the effective themes: the existing in-vocabulary themes plus the themes
just proposed. When the theme call failed (`error`) or no theme result is given, the call SHALL be made without
theme context.

#### Scenario: Themes present
- **WHEN** the resource has the existing theme `ENVI` and `AGRI` was just proposed
- **THEN** the rendered prompt contains the labels of both

#### Scenario: Theme call failed
- **WHEN** the theme result is `error`
- **THEN** the keyword call is still made and the rendered prompt has no theme labels

### Requirement: Language of the keywords
The working language and the title and description sent SHALL follow the same rule as the theme proposal. Keywords
SHALL be stored with the title's own language tag. When the title has no tag, the language returned by the model
SHALL be used instead and an INFO SHALL be logged that it was inferred; the description's tag SHALL never be used.

#### Scenario: Tagged title
- **WHEN** the English title is tagged `eng`
- **THEN** the result language is `eng`

#### Scenario: Untagged title
- **WHEN** the title has no tag and the model returns language `en`
- **THEN** the result language is `en`, flagged as inferred, and an INFO is logged

### Requirement: Validation of the answer
Each returned keyword SHALL be trimmed and dropped when empty. A keyword equal, ignoring case, to an existing or
an already kept keyword SHALL be dropped. The case SHALL be kept. When more remain than were asked for, the list
SHALL be cut to that number, in the model's order, with a WARNING.

#### Scenario: Valid answer
- **WHEN** the model answers `["Air quality", "Noise"]`
- **THEN** both are returned unchanged

#### Scenario: Duplicates and blanks
- **WHEN** the resource has `Air quality` and the model answers `[" air QUALITY ", "", "  ", "Noise", "noise"]`
- **THEN** only `Noise` is returned

#### Scenario: Over the cap
- **WHEN** the resource has 8 keywords and the model answers 3 new ones
- **THEN** the first 2 are returned, with a warning that the list was cut

### Requirement: Retry on an empty result
When nothing valid is left after validation, the call SHALL be repeated up to 3 times (at most 4 calls); still
empty gives an `error` result and an ERROR log naming the resource. A failed call SHALL end at once as `error`.

#### Scenario: All duplicates then success
- **WHEN** the first answer only repeats existing keywords and the second has a new one
- **THEN** 2 calls are made and the result is `ok`

#### Scenario: Retries exhausted
- **WHEN** four answers in a row leave nothing valid
- **THEN** exactly 4 calls are made and the result is `error` with no keywords

### Requirement: Result for the caller
The result SHALL hold the resource id, the status, the new keywords (empty unless `ok`), their language and whether
it was inferred, the prompt name, version, sha256 and model (absent when no call was made), the number of calls and
the issues. Nothing SHALL be written to the database.

#### Scenario: Nothing stored
- **WHEN** keywords are proposed
- **THEN** the database has no new row
