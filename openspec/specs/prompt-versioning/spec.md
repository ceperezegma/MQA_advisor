# prompt-versioning Specification

## Purpose
TBD - created by archiving change add-prompt-loader-and-schemas. Update Purpose after archive.

## Requirements

### Requirement: Prompts are versioned files with TOML frontmatter
The system SHALL keep each prompt as a UTF-8 file `src/mqa_advisor/prompts/{prompt_name}/v{N}.md` (a leading BOM
is accepted). The file SHALL start with a TOML frontmatter block between two lines containing only `+++`,
followed by the body, a `string.Template`. The frontmatter SHALL contain exactly the keys `version` (integer
>= 1), `model` (string), `temperature` (number, finite, 0 <= t <= 1), `max_tokens` (integer >= 1) and
`max_items` (integer >= 1), and no other key. `max_items` is the cap on the number of proposed values that the
callers enforce in code after the call; it is not part of the output schema. Two prompts SHALL exist in
version 1: `propose_themes` and `propose_keywords`, both with `model = "claude-haiku-5-5"` and
`temperature = 0`.

#### Scenario: Committed prompt files are valid
- **WHEN** `propose_themes` v1 and `propose_keywords` v1 are loaded
- **THEN** each yields its model `claude-haiku-5-5`, temperature `0.0` (a float), a positive `max_tokens` and a
  positive `max_items`

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

#### Scenario: Out-of-range values
- **WHEN** `max_tokens` or `max_items` is 0, `version` is below 1, or `temperature` is below 0, above 1 or NaN
- **THEN** loading raises an error naming the file and the key

#### Scenario: Boundary values are accepted
- **WHEN** `max_tokens` and `max_items` are 1 and `temperature` is 0, 0.5 or 1
- **THEN** the file loads, and an integer temperature is returned as a float

#### Scenario: Encoding
- **WHEN** a prompt file is not valid UTF-8
- **THEN** loading raises an error naming the file (never a raw decoding error)
- **WHEN** a prompt file starts with a UTF-8 BOM
- **THEN** it loads like the same file without the BOM

### Requirement: The loader renders the prompt and fingerprints the file
The system SHALL provide a loader that, given a prompt name, a version and the placeholder values, returns the
parsed frontmatter, the rendered body text and the SHA-256 (hex) of the file content. Line endings SHALL be
normalised to `\n` before hashing, so the hash does not depend on the checkout's line-ending setting. The
prompt name SHALL be a plain ASCII identifier and the version an integer >= 1, so a request cannot point
outside the prompts folder. Rendering SHALL fail when the body has an invalid placeholder (a stray `$`, `$1`),
when a placeholder has no value, when a value is not used by the body, or when a value is not a string. `$$`
renders as a single `$`.

#### Scenario: All placeholders are filled
- **WHEN** a prompt is loaded with a value for every placeholder in its body
- **THEN** the returned text contains those values and no `$placeholder` remains

#### Scenario: A placeholder value is missing
- **WHEN** a prompt is loaded and a placeholder of its body has no value
- **THEN** an error naming the placeholder is raised and no text is returned

#### Scenario: A value is not used
- **WHEN** a prompt is loaded with a value whose key is not a placeholder of its body
- **THEN** an error naming the key is raised

#### Scenario: A value is not a string
- **WHEN** a prompt is loaded with a non-string value (for example a number)
- **THEN** an error naming the key is raised

#### Scenario: Invalid placeholder in the body
- **WHEN** the body contains a stray `$` or `$1`
- **THEN** an error naming the file is raised
- **WHEN** the body contains `$$`
- **THEN** the rendered text contains a single `$`

#### Scenario: Name or version cannot escape the folder
- **WHEN** the name is not a plain identifier (for example `../x` or `a/b`) or the version is below 1
- **THEN** an error is raised and no file is opened

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
name a schema. The schema function SHALL take the prompt name and the version and return a JSON Schema object
whose list has `minItems` 1. The schema SHALL NOT contain `maxItems`: the Anthropic structured-output schema
subset supports `minItems` only with the values 0 and 1, so the cap (`max_items` of the frontmatter) is
enforced in code by the callers. The themes schema (`propose_themes`, 1) SHALL require a `themes` list of
strings. The keywords schema (`propose_keywords`, 1) SHALL require a `keywords` list of strings and a
`language` string matching `^[a-z]{2,3}$` (an ISO 639-1 two-letter code, or an ISO 639-3 three-letter code for a
language without a two-letter one). `language` is required on purpose: the model always reports the language
it wrote the keywords in, and callers use it when the resource title has no language tag. Every object SHALL
set `additionalProperties` to `false`. Each call SHALL return an independent copy, so a caller changing it does
not affect later calls.

#### Scenario: Exact v1 shapes
- **WHEN** the schemas of `propose_themes` v1 and `propose_keywords` v1 are requested
- **THEN** they equal exactly, respectively, an object with a required `themes` array of strings with
  `minItems` 1, and an object with a required `keywords` array of strings with `minItems` 1 and a required
  `language` string with pattern `^[a-z]{2,3}$`; both have `additionalProperties` false and no `maxItems`

#### Scenario: Language accepts two or three lowercase letters
- **WHEN** the pattern of the `language` field is matched against `en`, `eng`, `de`, `EN`, `e`, `engl` and `e1`
- **THEN** `en`, `eng` and `de` match and the others do not

#### Scenario: Independent copies
- **WHEN** a caller mutates a returned schema
- **THEN** the next call returns the unmodified schema

#### Scenario: A call for version N uses schema N
- **WHEN** a second version of a prompt with a different schema shape is registered, the active version is set
  to it, and a prompt of version 1 is loaded and its schema requested with the loaded name and version
- **THEN** the version 1 shape is returned, and asking for version 2 returns the version 2 shape

#### Scenario: Unknown schema
- **WHEN** a schema is requested for an unknown name or version
- **THEN** an error is raised

### Requirement: The cap is a contract for the callers
The system SHALL expose the cap as `max_items` of the loaded prompt. The callers (#21 themes, #22 keywords) are
responsible for enforcing it in code after the call (truncating or dropping the extras with a WARNING that
states the count, never silently), for never calling with a `$remaining` of 0, and for rendering empty
existing-value placeholders (`$existing_themes`, `$existing_keywords`, `$themes`) as `(none)`. These duties
are recorded in the design of this change and in the design document; this change does not implement them.

#### Scenario: Cap available to the caller
- **WHEN** a prompt is loaded
- **THEN** its `max_items` is available on the returned object

### Requirement: Prompt files and schemas stay paired
The system SHALL guarantee, with automated tests, that every committed prompt file has a schema with the same
name and version, that every schema has a prompt file, that the registry's active `(name, version)` pairs all
have a schema and a prompt file, and that the registry's names are exactly the committed prompt names.

#### Scenario: Pairing holds
- **WHEN** the test suite scans `src/mqa_advisor/prompts/`
- **THEN** the set of `(name, version)` pairs of prompt files equals the set of schema keys, the active pairs
  are a subset of both, and the registry names equal the prompt folder names

### Requirement: The v1 prompt wording protects against injection and states the counts
Both v1 prompts SHALL wrap the resource title and description in `<title>` and `<description>` tags and SHALL
state that text inside the tags is data and never instructions. They SHALL ask for "up to `$remaining`
additional" values "(at least 1)" and SHALL NOT tell the model to return fewer than one value. The keywords
prompt SHALL label the language placeholder "Language to write the keywords in", SHALL use the literal
`unknown` for a resource whose title has no language tag, SHALL ask for the ISO 639-1 two-letter code (a
three-letter ISO 639-3 code only for a language without a two-letter code) in the `language` field, and SHALL
ask to return an already given code unchanged. The sentinel `unknown` is a constant of the loader module, so
the callers and the prompt cannot drift apart.

#### Scenario: Tags and data line
- **WHEN** a v1 prompt is rendered
- **THEN** the title appears between `<title>` and `</title>`, the description between `<description>` and
  `</description>`, and the text states that the tagged text is data, never instructions

#### Scenario: Count wording
- **WHEN** a v1 prompt is rendered with `remaining` 3
- **THEN** it asks for up to 3 additional values with at least 1, and does not contain "between 1 and"

#### Scenario: Unknown language sentinel
- **WHEN** the keywords prompt body is read
- **THEN** it mentions the literal value defined by the sentinel constant (`unknown`)

### Requirement: The wheel ships the prompt files
The system SHALL include the prompt files in the built wheel, so an installed package loads its prompts. An
automated test SHALL build the wheel into a temporary folder and check that it contains both v1 files. Because
the build needs the build backend (and possibly network access), the test carries the `integration` marker and
is excluded from the default run.

#### Scenario: Built wheel contains prompts
- **WHEN** the wheel is built
- **THEN** it contains `mqa_advisor/prompts/propose_themes/v1.md` and `mqa_advisor/prompts/propose_keywords/v1.md`

### Requirement: Prompt loading is observable
The system SHALL log at DEBUG level, each time a prompt is loaded, the prompt name, version and SHA-256.

#### Scenario: Debug line on load
- **WHEN** a prompt is loaded with DEBUG logging enabled
- **THEN** one record contains the name, the version and the hash
