"""Propose additional ``dcat:theme`` values for one Dataset or standalone Data Service (ADR-0017).

Design doc ``docs/design/theme-keyword-proposals.md``, sections 3, 3.1 and 4. Three layers:

- ``read_resource(conn, resource_pk)`` is the only database access: it returns the title, description and
  existing themes of the resource's *own* entity as plain values (``ResourceText``). A Dataset record can also
  hold a related ``dcat:Dataset``; only the entity whose id starts with ``http://data.europa.eu/88u/`` is the
  resource's own. A standalone Data Service has a single entity.
- Pure helpers decide the language (``choose_language``), which existing themes count (``existing_themes``)
  and which returned codes are kept (``validate_codes``).
- ``propose_themes(resource, client, vocabulary)`` calls the model, validates, retries an empty answer and
  returns a ``ThemeResult``. It takes no connection, so the ``propose`` step can run it in worker threads while
  only the main thread touches SQLite. Nothing is written to the database here.

Usage:

    resource = read_resource(conn, resource_pk)
    result = propose_themes(resource, client, load_vocabulary())
    result.status  # "ok", "skipped" or "error"; result.issues go to the report
"""

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from sqlite3 import Connection
from typing import Literal

from mqa_advisor.llm import LLMClient, LLMError
from mqa_advisor.prompts import PromptError, active_version, load_prompt
from mqa_advisor.vocabulary import Vocabulary, code_to_uri, format_entries

logger = logging.getLogger(__name__)

DCT_TITLE = "http://purl.org/dc/terms/title"
DCT_DESCRIPTION = "http://purl.org/dc/terms/description"
DCAT_THEME = "http://www.w3.org/ns/dcat#theme"

# A Dataset's own entity has an id under this prefix; any other dataset entity of the record is a related one.
OWN_DATASET_PREFIX = "http://data.europa.eu/88u/"
ENGLISH_TAGS = frozenset({"en", "eng"})
THEME_CAP = (
    5  # existing in-vocabulary themes + proposed ones; also ``max_items`` of the prompt
)
MAX_EMPTY_RETRIES = 3  # retries of an answer with nothing valid left: at most 4 calls
THEMES_PROMPT = "propose_themes"

Status = Literal["ok", "skipped", "error"]
Severity = Literal["warning", "error"]
LangText = tuple[str | None, str]  # (language tag as stored, or None; text)


# --- reading the resource ----------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceText:
    """What the theme proposal needs from one resource, read from its own entity only."""

    resource_pk: int
    resource_id: str
    resource_type: str  # "dataset" or "dataservice"
    entity_id: str | None  # the own entity; None when there is none or several
    problem: str | None  # why ``entity_id`` is None
    titles: tuple[LangText, ...] = ()
    descriptions: tuple[LangText, ...] = ()
    theme_uris: tuple[str, ...] = ()


def own_entity(
    resource_type: str, entity_ids: Iterable[str]
) -> tuple[str | None, str | None]:
    """The resource's own entity among the ids of its kind: ``(entity_id, None)`` or ``(None, reason)``."""
    ids = sorted(set(entity_ids))
    if resource_type == "dataset":
        ids = [i for i in ids if i.startswith(OWN_DATASET_PREFIX)]
        what = f"dataset entity with an id starting with {OWN_DATASET_PREFIX}"
    else:
        what = f"{resource_type} entity"
    if len(ids) == 1:
        return ids[0], None
    if not ids:
        return None, f"no {what}"
    return None, f"{len(ids)} {what}s ({', '.join(ids)})"


def read_resource(conn: Connection, resource_pk: int) -> ResourceText:
    """Read the title, description and theme URIs of the resource's own entity.

    Raises ``ValueError`` for an unknown ``resource_pk`` or a theme ``node`` value without an ``@id`` (the
    loader writes these values, so either is a bug, not a catalogue problem).
    """
    row = conn.execute(
        "SELECT resource_id, resource_type FROM resource WHERE resource_pk = ?",
        (resource_pk,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no resource with resource_pk {resource_pk}")
    resource_id, resource_type = row
    entity_ids = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT entity_id FROM property_value"
            " WHERE resource_pk = ? AND entity_kind = ?",
            (resource_pk, resource_type),
        )
    ]
    entity_id, problem = own_entity(resource_type, entity_ids)
    if entity_id is None:
        return ResourceText(resource_pk, resource_id, resource_type, None, problem)

    titles: list[LangText] = []
    descriptions: list[LangText] = []
    themes: list[str] = []
    for property_uri, value, value_kind, lang in conn.execute(
        "SELECT property_uri, value, value_kind, lang FROM property_value"
        " WHERE resource_pk = ? AND entity_kind = ? AND entity_id = ?"
        " AND property_uri IN (?, ?, ?) ORDER BY property_uri, lang, value",
        (
            resource_pk,
            resource_type,
            entity_id,
            DCT_TITLE,
            DCT_DESCRIPTION,
            DCAT_THEME,
        ),
    ):
        if property_uri == DCAT_THEME:
            # A Dataset stores a theme as a node (the concept's JSON, its URI in @id), a Data Service as a uri.
            if value_kind == "uri":
                themes.append(value)
            elif value_kind == "node":
                themes.append(_node_id(value, resource_id))
        elif value_kind == "literal":
            (titles if property_uri == DCT_TITLE else descriptions).append(
                (lang, value)
            )
    return ResourceText(
        resource_pk,
        resource_id,
        resource_type,
        entity_id,
        None,
        tuple(titles),
        tuple(descriptions),
        tuple(themes),
    )


def _node_id(value: str, resource_id: str) -> str:
    try:
        node_id = json.loads(value).get("@id")
    except (json.JSONDecodeError, AttributeError) as error:
        raise ValueError(
            f"resource {resource_id}: a dcat:theme node value is not a JSON object"
        ) from error
    if not isinstance(node_id, str):
        raise ValueError(
            f"resource {resource_id}: a dcat:theme node value has no string @id"
        )
    return node_id


# --- language and themes (pure) ----------------------------------------------------------------


@dataclass(frozen=True)
class ModelText:
    """The title and description sent to the model, and the working language (the title's tag)."""

    title: str
    description: str  # "" when the resource has none
    language: str | None


def _is_english(tag: str | None) -> bool:
    return tag is not None and tag.lower() in ENGLISH_TAGS


def _language_key(tag: str | None) -> str | None:
    """A tag compared case-insensitively, with ``en`` and ``eng`` the same language."""
    if tag is None:
        return None
    return "en" if _is_english(tag) else tag.lower()


def _preferred(texts: list[LangText]) -> LangText:
    """English first, then the alphabetically first tag, untagged last; ties broken by the text."""
    return min(
        texts,
        key=lambda t: (t[0] is None, not _is_english(t[0]), (t[0] or "").lower(), t[1]),
    )


def choose_language(
    titles: Iterable[LangText], descriptions: Iterable[LangText]
) -> ModelText | None:
    """Apply the language rule of design section 3.1; None when there is no (non-blank) title."""
    titles = [t for t in titles if t[1].strip()]
    descriptions = [d for d in descriptions if d[1].strip()]
    if not titles:
        return None
    language, title = _preferred(titles)
    description = ""
    key = _language_key(language)
    if same := [d for d in descriptions if _language_key(d[0]) == key]:
        description = _preferred(same)[1]
    elif descriptions:
        # No description in the working language: send one in its own language, labelled with it. Prompt v1 has
        # no placeholder for the label, so it goes in the value.
        tag, text = _preferred(descriptions)
        description = f"(language: {tag}) {text}" if tag else text
    return ModelText(title, description, language)


def existing_themes(uris: Iterable[str], vocabulary: Vocabulary) -> tuple[str, ...]:
    """The existing theme URIs that are in the vocabulary, each once, in their order."""
    known = set(vocabulary.uris_by_code.values())
    return tuple(dict.fromkeys(u for u in uris if u in known))


def validate_codes(
    codes: Iterable[str],
    existing: Iterable[str],
    remaining: int,
    vocabulary: Vocabulary,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Keep the returned codes that are new vocabulary codes, up to ``remaining``.

    Returns the kept themes as authority URIs (in the model's order) and one warning message per unknown code
    and per cut list. Duplicates of existing or already kept themes are dropped without a warning.
    """
    seen = set(existing)
    kept: list[str] = []
    warnings: list[str] = []
    for code in codes:
        uri = code_to_uri(vocabulary, code)  # exact match only: never guesses
        if uri is None:
            warnings.append(f"dropped theme {code!r}: not a code of the vocabulary")
        elif uri not in seen:
            seen.add(uri)
            kept.append(uri)
    if len(kept) > remaining:
        warnings.append(
            f"theme list cut: {len(kept)} new themes returned, {remaining} asked for,"
            f" {len(kept) - remaining} dropped"
        )
        kept = kept[:remaining]
    return tuple(kept), tuple(warnings)


# --- the theme proposal ------------------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    """One row of the report: a warning or an error about a resource."""

    severity: Severity
    resource_id: str
    message: str


@dataclass(frozen=True)
class PromptInfo:
    """Which prompt made the proposals: stored with them (provenance)."""

    name: str
    version: int
    sha256: str
    model: str


@dataclass(frozen=True)
class ThemeResult:
    resource_id: str
    status: Status
    themes: tuple[str, ...]  # new themes as authority URIs; empty unless "ok"
    existing_themes: tuple[str, ...]  # existing in-vocabulary themes as authority URIs
    prompt: PromptInfo | None  # None when no call was made
    calls: int
    issues: tuple[Issue, ...]


def propose_themes(
    resource: ResourceText, client: LLMClient, vocabulary: Vocabulary
) -> ThemeResult:
    """Propose between 1 and (5 - existing) new themes for ``resource`` (design section 3).

    Skipped (no call) when the resource has no own entity or no title (with a warning) or already has 5
    in-vocabulary themes (without one). An answer with nothing valid left is retried up to 3 times; a failed
    call (``LLMError``) ends at once. Both give an ``error`` result with no themes.
    """
    resource_id = resource.resource_id
    issues: list[Issue] = []

    def report(severity: Severity, message: str) -> None:
        log = logger.error if severity == "error" else logger.warning
        log("resource %s: %s", resource_id, message)
        issues.append(Issue(severity, resource_id, message))

    def result(
        status: Status,
        existing: tuple[str, ...] = (),
        themes: tuple[str, ...] = (),
        prompt: PromptInfo | None = None,
        calls: int = 0,
    ) -> ThemeResult:
        return ThemeResult(
            resource_id, status, themes, existing, prompt, calls, tuple(issues)
        )

    if resource.entity_id is None:
        report("warning", f"skipped: {resource.problem}")
        return result("skipped")
    existing = existing_themes(resource.theme_uris, vocabulary)
    text = choose_language(resource.titles, resource.descriptions)
    if text is None:
        report("warning", "skipped: no title")
        return result("skipped", existing)
    remaining = THEME_CAP - len(existing)
    if remaining <= 0:
        return result("skipped", existing)

    prompt = load_prompt(
        THEMES_PROMPT,
        active_version(THEMES_PROMPT),
        {
            "vocabulary": "\n".join(format_entries(vocabulary)),
            "title": text.title,
            "description": text.description,
            "existing_themes": _existing_lines(existing, vocabulary),
            "remaining": str(remaining),
        },
    )
    if prompt.max_items != THEME_CAP:
        raise PromptError(
            f"{THEMES_PROMPT} v{prompt.version}: max_items is {prompt.max_items}, the theme cap is {THEME_CAP}"
        )
    info = PromptInfo(prompt.name, prompt.version, prompt.sha256, prompt.model)

    calls = 0
    for _ in range(1 + MAX_EMPTY_RETRIES):
        calls += 1
        try:
            answer = client.call(prompt, resource=resource_id)
        except LLMError as error:
            report("error", f"theme call failed: {error}")
            return result("error", existing, prompt=info, calls=calls)
        themes, warnings = validate_codes(
            answer.output["themes"], existing, remaining, vocabulary
        )
        for message in warnings:
            report("warning", message)
        if themes:
            return result("ok", existing, themes, info, calls)
        logger.info("resource %s: no valid new theme in answer %d", resource_id, calls)
    report("error", f"no valid new theme after {calls} calls")
    return result("error", existing, prompt=info, calls=calls)


def _existing_lines(existing: tuple[str, ...], vocabulary: Vocabulary) -> str:
    """The existing themes for the prompt, one ``CODE — label`` line each, or ``none``."""
    by_uri = {entry.uri: entry for entry in vocabulary.entries}
    lines = [f"{by_uri[u].code} — {by_uri[u].label}" for u in existing]
    return "\n".join(lines) or "none"
