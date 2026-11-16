"""Output schemas of the theme/keyword proposals, keyed by ``(prompt_name, version)``.

A prompt version always uses the schema with the same name and version (ADR-0018): no field of the
prompt file names a schema. The schemas are immutable per version; when a new prompt version keeps the
same shape, give it its own entry that calls the previous builder.

There is deliberately no ``maxItems``: Anthropic's structured outputs support ``minItems`` only as 0 or 1.
The cap is ``max_items`` of the prompt frontmatter, enforced in code by the callers.
"""

from collections.abc import Callable
from typing import Any


def _themes_v1() -> dict[str, Any]:
    # Items are plain strings, not an enum of the theme codes: invalid codes are dropped in code
    # with a warning (design doc), not rejected by the schema.
    return {
        "type": "object",
        "properties": {
            "themes": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        },
        "required": ["themes"],
        "additionalProperties": False,
    }


def _keywords_v1() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "keywords": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            # Required on purpose: the model always reports the language it wrote the keywords in, and
            # the caller uses it when the resource title has no language tag. ISO 639-1 (2 letters) is
            # what the prompt asks for; 3 letters (ISO 639-3) cover a language without a 2-letter code.
            "language": {"type": "string", "pattern": "^[a-z]{2,3}$"},
        },
        "required": ["keywords", "language"],
        "additionalProperties": False,
    }


_BUILDERS: dict[tuple[str, int], Callable[[], dict[str, Any]]] = {
    ("propose_themes", 1): _themes_v1,
    ("propose_keywords", 1): _keywords_v1,
}


def schema_keys() -> set[tuple[str, int]]:
    """The ``(prompt_name, version)`` pairs that have a schema."""
    return set(_BUILDERS)


def output_schema(name: str, version: int) -> dict[str, Any]:
    """Return a fresh JSON Schema for the prompt ``name`` at ``version``."""
    builder = _BUILDERS.get((name, version))
    if builder is None:
        raise ValueError(f"no output schema for prompt {name!r} version {version}")
    return builder()
