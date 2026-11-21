## Purpose

Lets a maintainer check a prompt version against hand-picked real resources on the live model, with loose
deterministic checks, before the version becomes the active one.

## ADDED Requirements

### Requirement: Regression examples are committed fixtures
The repository SHALL contain 8 regression examples under `tests/fixtures/prompts/`. Each SHALL record its source
(catalogue and resource id), the title with its language tag (or none), the description (possibly empty), the
existing themes (vocabulary codes), the existing keywords, and the set of accepted new theme codes. Together they
SHALL include a standalone Data Service, a resource with an existing theme, a resource with no description and a
resource whose title has no language tag.

#### Scenario: Required cases are present
- **WHEN** the examples are loaded
- **THEN** there are 8, each accepted theme is a code of the vocabulary, and the four required cases are each covered
  by at least one example

### Requirement: Live regression tests apply loose checks
For each example, an `integration` test SHALL run the active theme prompt and then the active keyword prompt (with
the proposed themes as context) on the live model and SHALL check: every theme code returned is in the vocabulary;
the number of new themes is between 1 and the theme cap minus the existing themes; the number of new keywords is
between 1 and the keyword cap minus the existing keywords; the keywords are in the title's language (for an
untagged title, the model's language code is not English when the title text is not English); and at least one new
theme is in the example's accepted set. The checks SHALL NOT require an exact match. A failure SHALL name the
example and the property that failed.

#### Scenario: A good answer passes
- **WHEN** the answer has valid codes, counts within the caps, keywords in the title's language and one accepted theme
- **THEN** every check passes

#### Scenario: A failing check names example and property
- **WHEN** no proposed theme is in the accepted set of example `X`
- **THEN** the failure message contains `X` and `accepted themes`

### Requirement: Integration tests are opt-in and skip without a key
The live tests SHALL be marked `integration` (excluded from the default run) and SHALL be skipped with a reason
naming `ANTHROPIC_API_KEY` when it is not set. Their calls SHALL be logged like any run, with the cost per call.

#### Scenario: Missing key
- **WHEN** `uv run pytest -m integration` runs without `ANTHROPIC_API_KEY`
- **THEN** the regression tests are reported as skipped with a reason mentioning `ANTHROPIC_API_KEY`

#### Scenario: Default run
- **WHEN** `uv run pytest` runs
- **THEN** no regression test calls the model
