## 1. Reading the resource

- [x] 1.1 `propose.py`: `ResourceText`, the own-entity helper (Dataset: id starts with `http://data.europa.eu/88u/`; Data Service: its single `dataservice` entity; none or several -> no entity, with the reason) — verified by unit tests: one own entity, a related dataset ignored, none, two
- [x] 1.2 `read_resource(conn, resource_pk)`: title, description (`(lang, text)` pairs) and theme URIs of the own entity (`uri` value or `@id` of a `node` value) — verified by tests on a small in-memory database loaded from fixtures: a Dataset record with a related dataset, a standalone Data Service

## 2. Language and themes (pure)

- [x] 2.1 `choose_language(titles, descriptions)`: English (`en`/`eng`) first, else the alphabetically first tag, untagged last; description in the working language, else its own with the `(language: xxx) ` prefix; no title -> None — verified by tests: English present, non-English title, description in another language, two title tags, untagged, no title, no description
- [x] 2.2 `existing_themes(uris, vocabulary)`: in-vocabulary URIs only, duplicates once — verified by tests: node and uri shapes, out-of-vocabulary ignored
- [x] 2.3 `validate_codes(codes, existing, remaining, vocabulary)`: unknown code dropped with a warning each, duplicates of existing or kept dropped, cut at `remaining` with a warning, URIs returned in the model's order — verified by tests: valid, mixed, duplicate-of-existing, over-cap

## 3. The theme proposal

- [x] 3.1 `Issue`, `PromptInfo`, `ThemeResult`; `propose_themes(resource, client, vocabulary)`: skip (no entity / no title with a warning; cap reached without one), prompt values (`$vocabulary`, `$title`, `$description`, `$existing_themes`, `$remaining`), call through `LLMClient`, validation, up to 3 retries on an empty result, `LLMError` -> `error` result; WARNING/ERROR logs with the resource id — verified by `FakeBackend` tests: valid, mixed, all-invalid, duplicate, over-cap, 5 themes no call, all out-of-vocabulary themes -> call, third retry succeeds (4 calls), retries exhausted (exactly 4 calls, `error`), API failure (1 call, `error`), rendered user part has the remaining count, existing themes and the description prefix
- [x] 3.2 Verify nothing is written: the database row counts are unchanged after `propose_themes` on a loaded fixture

## 4. Docs and checks

- [x] 4.1 `CLAUDE.md` architecture notes: a line for `propose.py`; verified by reading the diff
- [x] 4.2 `uv run ruff check src/ tests/`, `uv run ruff format src/ tests/` and `uv run pytest` all pass
