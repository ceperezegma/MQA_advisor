## ADDED Requirements

### Requirement: Prompts are versioned files with TOML frontmatter
The system SHALL keep each prompt as a file `src/mqa_advisor/prompts/{prompt_name}/v{N}.md`. The file SHALL start
with a TOML frontmatter block between two lines containing only `+++`, followed by the body, a
`string.Template`. The frontmatter SHALL contain exactly the keys `version`, `model`, `temperature`,
`max_tokens` and `max_items`, and no other key. Two prompts SHALL exist in version 1: `propose_themes` and
`propose_keywords`, both with `model = "claude-haiku-5-5"` and `temperature = 0`.

#### Scenario: Committed prompt files are valid
- **WHEN** `propose_themes` v1 and `propose_keywords` v1 are loaded
- **THEN** each yields its model `claude-haiku-5-5`, temperature `0`, a positive `max_tokens` and a positive
  `max_items`

#### Scenario: Frontmatter version must equal the file number
- **WHEN** a file `v2.md` has `version = 1` in its frontmatter
- **THEN** loading it raises an error naming the file and both numbers

#### Scenario: Frontmatter names a schema
- **WHEN** a prompt file's frontmatter contains a key other than the five allowed ones (for example `schema`)
- **THEN** loading it raises an error naming the unexpected key

#### Scenario: Malformed or missing frontmatter
- **WHEN** a prompt file has no opening or closing `+++` line, invalid TOML, a missing key or a value of the
  wrong type
- **THEN** loading it raises an error naming the file and the problem, and never returns a partial prompt

### Requirement: The loader renders the prompt and fingerprints the file
The system SHALL provide a loader that, given a prompt name, a version and the placeholder values, returns the
parsed frontmatter, the rendered body text and the SHA-256 (hex) of the file content. Line endings SHALL be
normalised to `\n` before hashing, so the hash does not depend on the checkout's line-ending setting.
Rendering SHALL fail when a placeholder in the body has no value.

#### Scenario: All placeholders are filled
- **WHEN** a prompt is loaded with a value for every placeholder in its body
- **THEN** the returned text contains those values and no `$placeholder` remains

#### Scenario: A placeholder value is missing
- **WHEN** a prompt is loaded and a placeholder of its body has no value
- **THEN** an error naming the placeholder is raised and no text is returned

#### Scenario: Hash follows the content
- **WHEN** two prompt files differ in one character, or the same file is loaded twice
- **THEN** the hashes differ in the first case and are equal in the second, and a file with CRLF line endings
  has the same hash as the same content with LF endings

### Requirement: Registry of active versions
The system SHALL provide, in code, a registry giving the active version of each prompt name (`propose_themes`
and `propose_keywords`, both 1 at this change). Loading by explicit version SHALL NOT depend on the active
version. An unknown prompt name or an unknown version SHALL raise an error and never fall back to another
version.

#### Scenario: Active version lookup
- **WHEN** the active version of `propose_themes` is requested
- **THEN** it is 1

#### Scenario: Unknown name or version
- **WHEN** a prompt is requested with an unknown name, or with a version that has no file
- **THEN** an error naming the request is raised

#### Scenario: Active version without a file
- **WHEN** the registry names an active version whose file does not exist
- **THEN** loading the active prompt raises an error

### Requirement: Output schemas are bound to the prompt version
The system SHALL define the output schemas in code, keyed by `(prompt_name, version)`. A prompt version SHALL
always use the schema with the same name and the same version number, and no field of the prompt file SHALL
name a schema. The schema function SHALL take the prompt name, the version and `max_items`, and return a
JSON Schema object whose list has `minItems` 1 and `maxItems` equal to the given `max_items`. The themes
schema (`propose_themes`, 1) SHALL require a `themes` list of strings. The keywords schema
(`propose_keywords`, 1) SHALL require a `keywords` list of strings and a `language` string (a 3-letter
lowercase code), the latter used when the title has no language tag. Each call SHALL return an independent
copy, so a caller changing it does not affect later calls.

#### Scenario: Limits are passed per call
- **WHEN** the schema of `propose_themes` v1 is requested with `max_items` 3
- **THEN** its `themes` list has `minItems` 1 and `maxItems` 3, and a request with `max_items` 4 afterwards is
  not affected by the first

#### Scenario: Keyword schema carries the language
- **WHEN** the schema of `propose_keywords` v1 is requested
- **THEN** it requires `keywords` and `language`, and `language` accepts exactly three lowercase letters

#### Scenario: A call for version N uses schema N
- **WHEN** a prompt of version N is loaded while the active version is different, and its schema is requested
  with the loaded prompt's name and version
- **THEN** the schema of version N is returned, not the active one's

#### Scenario: Unknown schema or invalid limit
- **WHEN** a schema is requested for an unknown name or version, or with `max_items` below 1
- **THEN** an error is raised

### Requirement: Prompt files and schemas stay paired
The system SHALL guarantee, with automated tests, that every committed prompt file has a schema with the same
name and version, that every schema has a prompt file, and that the registry's active versions exist in both.

#### Scenario: Pairing holds
- **WHEN** the test suite scans `src/mqa_advisor/prompts/`
- **THEN** the set of `(name, version)` pairs of prompt files equals the set of schema keys

### Requirement: The wheel ships the prompt files
The system SHALL include the prompt files in the built wheel, so an installed package loads its prompts.

#### Scenario: Built wheel contains prompts
- **WHEN** the wheel is built
- **THEN** it contains `mqa_advisor/prompts/propose_themes/v1.md` and `mqa_advisor/prompts/propose_keywords/v1.md`

### Requirement: Prompt loading is observable
The system SHALL log at DEBUG level, each time a prompt is loaded, the prompt name, version and SHA-256.

#### Scenario: Debug line on load
- **WHEN** a prompt is loaded with DEBUG logging enabled
- **THEN** one record contains the name, the version and the hash
