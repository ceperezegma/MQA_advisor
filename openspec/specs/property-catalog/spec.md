# property-catalog Specification

## Purpose
Parse the reconciled property catalog (`MQA_methodology/MQA-properties-for-app.xlsx`) into a structured, type-checked, in-repo `Property` model — one collection per resource type (Dataset, Distribution, Data Service) — that the scoring engine (#4) imports as its single source of truth for which DCAT-AP checks to run, instead of hardcoding its own list.

## Requirements
### Requirement: Structured, Type-Checked Property Model
The system SHALL represent each row of the reconciled property catalog as a `Property` value carrying exactly: name, DCAT-AP property URI, Importance, Weight, and the metric description text — using the project's own domain vocabulary (`Property`, `Importance`, `Weight`), never the MQA App's JSON field names (e.g. `metric`).

#### Scenario: Field names follow CONTEXT.md vocabulary
- **WHEN** the parsed `Property` type is inspected
- **THEN** its fields are named for `name`/`property_uri`/`importance`/`weight`/`metric_description` (or equivalent CONTEXT.md-vocabulary terms), and no field is named `metric`

#### Scenario: No Dimension field exists
- **WHEN** the parsed `Property` type is inspected
- **THEN** it has no `Dimension` (or FAIR-label) field or attribute anywhere, and no Dimension value appears anywhere in the loaded catalog

### Requirement: Three Resource-Type Collections from the Reconciled Catalog
The system SHALL parse `MQA_methodology/MQA-properties-for-app.xlsx`'s three sheets (`Datasets`, `Distributions`, `Dataservices`) into exactly three collections of `Property` values — one per resource type (Dataset, Distribution, Data Service) — usable as the scoring engine's single source of truth, with no property list duplicated elsewhere in application code.

#### Scenario: Loading produces exactly three collections
- **WHEN** the property catalog is loaded from the committed xlsx
- **THEN** exactly three collections are returned, one each for Dataset, Distribution, and Data Service Properties

#### Scenario: Row counts match the reconciled xlsx
- **WHEN** the property catalog is loaded from the committed xlsx
- **THEN** the Dataset collection has 18 entries, the Distribution collection has 20 entries, and the Data Service collection has 18 entries

#### Scenario: A known row parses correctly
- **WHEN** the Dataset collection is loaded
- **THEN** it contains an entry with name "Title", property URI `dct:title`, Importance Mandatory, and Weight `1.0`

### Requirement: Cosmetic Source Defects Normalized on Load
The system SHALL normalize two known cosmetic defects present in the source xlsx during load, rather than surfacing them as-is: the stray trailing-space/backtick artifacts around the "Serves dataset" row (Data Service sheet), and the "landing page accesiblity test" spelling, which SHALL read "...accessibility test".

#### Scenario: "Serves dataset" row is normalized
- **WHEN** the Data Service collection is loaded
- **THEN** it contains an entry whose name is exactly "Serves dataset" (no trailing space) and whose Importance is exactly Recommended (no stray backtick or other artifact)

#### Scenario: Landing page accessibility test typo is corrected
- **WHEN** the Data Service collection is loaded
- **THEN** it contains an entry whose name is exactly "Landing page accessibility test" (not "...accesiblity...")

### Requirement: Importance/Weight Consistency Validation with Named Exceptions
The system SHALL validate, for every row, that Importance and Weight are internally consistent under the standard mapping (Mandatory→1.0, Recommended→0.5, Optional→0.25), and SHALL raise a clear error for any row that violates this mapping — EXCEPT for two confirmed, narrowly-identified rows where the mapping is intentionally not standard: the Distribution "Access URL accessibility test" row (`dcat:accessURL`, Mandatory/0.25) and the Data Service "Endpoint URL accessibility test" row (`dcat:endpointURL`, Mandatory/0.25). Those two rows SHALL load successfully without raising.

#### Scenario: The real xlsx, including both confirmed exceptions, loads without raising
- **WHEN** the property catalog is loaded from the committed, unmodified xlsx
- **THEN** no error is raised, and the Distribution collection contains "Access URL accessibility test" (Mandatory, Weight `0.25`) and the Data Service collection contains "Endpoint URL accessibility test" (Mandatory, Weight `0.25`)

#### Scenario: A genuinely mismatched row raises a clear error
- **WHEN** the catalog is loaded from an xlsx containing a row whose Importance does not map to its Weight under the standard mapping, and that row is not one of the two named exceptions
- **THEN** the loader raises an error whose message clearly identifies the offending sheet, property name, and the mismatched Importance/Weight values

#### Scenario: A named-exception row with the wrong weight still raises
- **WHEN** the catalog is loaded from an xlsx where a row matching one of the two named exceptions (by sheet, name, and property URI) has a Weight other than the confirmed exception value `0.25`
- **THEN** the loader raises an error — being a named exception only changes the expected weight from the standard mapping to `0.25`, it does not skip validation entirely

### Requirement: Required Cells Fail Fast on a Blank Value
The system SHALL raise a clear `PropertyCatalogError` — naming the offending sheet, row, and column — for any row where Property name, Relevance, Weight, or Metric definition is blank (missing or whitespace-only), and SHALL raise a clear `PropertyCatalogError` when Weight is present but not a valid number, rather than crashing with an unhandled built-in exception or silently producing a `Property` with a missing/invalid field.

#### Scenario: A blank required cell raises a clear error
- **WHEN** the catalog is loaded from an xlsx containing a row with a blank Property name, Relevance, or Weight cell
- **THEN** the loader raises `PropertyCatalogError` naming the sheet, row number, and the blank column — never an unhandled `AttributeError`, `TypeError`, or a `Property` with a `None` field

#### Scenario: A non-numeric Weight raises a clear error
- **WHEN** the catalog is loaded from an xlsx containing a row whose Weight cell holds a non-numeric value
- **THEN** the loader raises `PropertyCatalogError` naming the sheet, row number, and property name — never an unhandled `TypeError` or `ValueError`

### Requirement: Legitimate Duplicates Preserved
The system SHALL NOT deduplicate rows sharing the same Property name within one sheet when their DCAT-AP property URIs differ; both SHALL be kept as distinct entries.

#### Scenario: Two "Identifier" rows are both kept
- **WHEN** the Dataset collection is loaded
- **THEN** it contains two distinct entries named "Identifier" — one with property URI `dct:identifier` and one with property URI `adms:identifier` — and neither is dropped

### Requirement: Deterministic Reload
The system SHALL produce an identical result (same collections, same entries, same order) when the loader is run repeatedly against an unchanged xlsx file.

#### Scenario: Repeated loads are identical
- **WHEN** the property catalog is loaded twice in a row from the same unchanged xlsx file
- **THEN** the two resulting sets of collections are equal
