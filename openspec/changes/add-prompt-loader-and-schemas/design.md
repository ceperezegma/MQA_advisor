## Context

Design doc `docs/design/theme-keyword-proposals.md` section 6 and ADR-0018 fix the layout, the file format and
the version/schema binding. This change builds that piece only: no LLM call, no database, no pipeline. The
consumers are #20 (client wrapper), #21 (themes), #22 (keywords) and #23 (step).

## Goals / Non-Goals

**Goals:** immutable versioned prompt files; one loader; one registry of active versions; schemas keyed by
`(name, version)`; tests that keep files and schemas paired.

**Non-Goals:** calling the model, building the forced tool call, computing cost, rendering the theme
vocabulary lines (that is #17), cap arithmetic and validation of the response (#21/#22).

## Decisions

**Modules.** `src/mqa_advisor/prompts.py` (loader, registry) and `src/mqa_advisor/proposal_schemas.py`
(schemas), as in the design doc section 10. The data folder `src/mqa_advisor/prompts/` sits next to
`prompts.py`. It has no `__init__.py`, so Python imports the module, not the folder. The loader finds the
files with `Path(__file__).parent / "prompts"`, never through the import system. (Discrepancy with nothing in
the ticket, but a naming trap: see "Open points".)

**Loader API.** One call does everything the ticket lists:

```python
@dataclass(frozen=True)
class LoadedPrompt:
    name: str
    version: int
    model: str
    temperature: float
    max_tokens: int
    max_items: int
    text: str        # rendered body
    sha256: str      # of the file content

def load_prompt(name: str, version: int, values: Mapping[str, str], *, prompts_dir: Path = PROMPTS_DIR) -> LoadedPrompt
def active_version(name: str) -> int
ACTIVE: dict[str, int] = {"propose_themes": 1, "propose_keywords": 1}
```

`prompts_dir` is a parameter only so tests can point at small fixture folders. Errors: one `PromptError`
(subclass of `ValueError`) with a message naming the file or request; a missing placeholder value surfaces as
`PromptError` too (wrapping the `KeyError` of `Template.substitute`, with `from`).

**Frontmatter.** The file must start with a `+++` line; the block ends at the next line that is exactly `+++`;
the rest is the body. Parsed with `tomllib`. Keys are exactly `version`, `model`, `temperature`, `max_tokens`,
`max_items`; a missing, extra or wrongly typed key is an error (this is also what stops a `schema` key from
ever existing). `version` must equal `N` of `vN.md`. `max_items` and `max_tokens` must be >= 1.

**Hash.** SHA-256 of the file's UTF-8 bytes after replacing `\r\n` with `\n`. Without the normalisation a
Windows checkout with `autocrlf` would store a different `prompt_sha256` than Linux for the same commit, and
the "was an immutable version edited?" check of ADR-0018 would give false alarms. The hash covers the whole
file, frontmatter included.

**Registry.** `ACTIVE` is a plain dict in `prompts.py`. `active_version(name)` raises `PromptError` for an
unknown name. Loading takes an explicit version, so a caller that builds a call for version N never depends on
`ACTIVE`; `load_active(name, values)` is **not** added now (YAGNI: #21/#22 can call
`load_prompt(name, active_version(name), ...)`).

**Schemas.** `proposal_schemas.output_schema(name, version, max_items) -> dict`. Internally a dict
`_BUILDERS[(name, version)] -> callable(max_items) -> dict`; each call builds a fresh dict, so callers cannot
mutate shared state. A new prompt version with an unchanged shape gets its own entry that calls the previous
builder explicitly (ADR-0018). Unknown key or `max_items < 1` raises `ValueError`.

```python
# ("propose_themes", 1)
{"type": "object", "properties": {"themes": {"type": "array", "items": {"type": "string"},
 "minItems": 1, "maxItems": max_items}}, "required": ["themes"], "additionalProperties": False}

# ("propose_keywords", 1)
{"type": "object", "properties": {
   "keywords": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": max_items},
   "language": {"type": "string", "pattern": "^[a-z]{3}$"}},
 "required": ["keywords", "language"], "additionalProperties": False}
```

The theme items are plain strings, not an `enum` of the 14 codes: the vocabulary lives in `vocabulary.py`
(#17), the schema is immutable per version, and the design says invalid codes are dropped by code with a
warning, not rejected by the schema. `language` is required: the model always returns it, and it is used only
when the title has no language tag (design 3.1).

**Wheel.** `[tool.hatch.build.targets.wheel] packages = ["src/mqa_advisor"]` already packages every file under
the package that git does not ignore, so no change is expected. Verified by building the wheel and listing it
(a manual task, not a unit test: a build in the unit suite is slow and needs the build backend).

**Logging.** `logger.debug("prompt loaded: name=%s version=%d sha256=%s", ...)` once per `load_prompt`.

## Prompt wording (v1, for review)

Placeholders are a contract with #21/#22, which fill them. All values are strings.

`propose_themes/v1.md` placeholders: `$title`, `$description`, `$existing_themes` (lines `CODE — label`, or
`none`), `$vocabulary` (lines from #17), `$remaining` (the upper limit, a number).

```
+++
version = 1
model = "claude-haiku-5-5"
temperature = 0
max_tokens = 1024
max_items = 5
+++
You assign subject themes to a dataset or data service published in an open data catalogue.

Choose only from the fixed list of themes below. Answer with theme codes exactly as written in the list.
Never invent a code, never translate a code, and never answer with a label.

Themes (CODE — label: definition):
$vocabulary

Resource to classify:
Title: $title
Description: $description

Themes the resource already has (do not repeat them):
$existing_themes

Return between 1 and $remaining additional themes that clearly fit the resource, most relevant first.
Return fewer rather than padding the list with weak matches. Base the choice only on the title and the
description above; do not assume anything that is not stated or directly implied.
```

`propose_keywords/v1.md` placeholders: `$title`, `$description`, `$language` (a 3-letter code such as `eng`,
or `unknown` when the title has no language tag), `$themes` (labels of the effective themes, or `none`),
`$existing_keywords` (comma-separated, or `none`), `$remaining`.

```
+++
version = 1
model = "claude-haiku-5-5"
temperature = 0
max_tokens = 1024
max_items = 10
+++
You propose search keywords for a dataset or data service published in an open data catalogue, so that
people looking for this data can find it.

Resource:
Title: $title
Description: $description
Language of the title: $language
Themes: $themes

Keywords it already has (do not repeat them, not even with different capitalisation):
$existing_keywords

Return between 1 and $remaining additional keywords.
- Write the keywords in the language of the title. If the language above is "unknown", work out the language
  from the title. In the `language` field of your answer return the language of the title as a lowercase
  ISO 639-3 code (for example "eng", "deu", "fra"); if the language above is a code, return that same code.
- Each keyword is a short term or phrase of one to three words that a person might type to find this
  resource. Use the natural capitalisation of the language (capitals only for proper names).
- Use only what the title and the description say or directly imply. Do not invent places, dates, organisations
  or facts, and do not repeat the title as a keyword.
- Prefer specific terms over generic ones such as "data" or "dataset".
Return fewer rather than padding the list with weak keywords.
```

## Risks / Trade-offs

- A hash computed after newline normalisation is not the git blob hash; acceptable, it only needs to detect
  edits.
- Rejecting unknown frontmatter keys means a future new field needs a code change; intended (ADR-0018 keeps
  schema names out of the file, and the format stays closed).
- Prompt wording cannot be proven by unit tests; quality is judged by the regression examples (#24). The
  wording above is a first draft.

## Open points

- **Naming trap (decided):** `prompts.py` and the folder `prompts/` share a name. It works (no `__init__.py`,
  files are found by path). Decision by the user: keep `prompts.py`, as in the design doc.
- The ticket text lists a frontmatter `version`, `model`, `temperature`, `max_tokens`, `max_items`; the design
  doc example matches. No discrepancy found with ADR-0018.
