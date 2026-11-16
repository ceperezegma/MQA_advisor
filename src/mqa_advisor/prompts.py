"""Versioned prompt files, their loader and the registry of active versions (ADR-0018).

A prompt lives in ``prompts/{name}/v{N}.md`` next to this module: a TOML frontmatter between two ``+++``
lines, then a ``string.Template`` body in two parts split by a ``---USER---`` line: the system part (role,
rules, fixed lists) and the user part (the resource data and the counts). Files are immutable once published; a change is a new ``vN+1.md``.
The output schema of a prompt version is looked up by the same name and version in ``proposal_schemas``;
no field of the file names a schema.

``prompts/`` has no ``__init__.py``, so ``import mqa_advisor.prompts`` is this module, not the folder.
"""

import hashlib
import logging
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import TypedDict

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"

# The version each prompt uses today. A caller that builds a call for version N loads N explicitly and
# never depends on this registry.
ACTIVE: dict[str, int] = {"propose_themes": 1, "propose_keywords": 1}

# The value callers pass as $language when the resource title has no language tag. The keywords prompt
# tells the model what to do with this exact word, and a test pins that the prompt mentions it.
UNKNOWN_LANGUAGE = "unknown"

_DELIMITER = "+++"
_USER_SEPARATOR = (
    "---USER---"  # the line between the system part and the user part of the body
)
_NAME = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*"
)  # a plain ASCII identifier: cannot leave the prompts folder


class PromptError(ValueError):
    """A prompt file or request is invalid."""


class Frontmatter(TypedDict):
    """The validated frontmatter of a prompt file."""

    version: int
    model: str
    max_tokens: int
    max_items: int


@dataclass(frozen=True)
class LoadedPrompt:
    """A prompt file read, validated and rendered: what a caller needs to build one model call."""

    name: str
    version: int
    model: str
    max_tokens: int
    max_items: int  # the cap on proposed values; the callers enforce it in code, it is not in the schema
    system: str  # the system part of the body with every placeholder filled
    user: str  # the user part of the body with every placeholder filled
    sha256: str  # of the file content (line endings normalised), to detect an edit of an immutable version


def active_version(name: str) -> int:
    """The active version of prompt ``name``."""
    try:
        return ACTIVE[name]
    except KeyError:
        raise PromptError(f"unknown prompt {name!r}; known: {sorted(ACTIVE)}") from None


def load_prompt(
    name: str,
    version: int,
    values: Mapping[str, str],
    *,
    prompts_dir: Path = PROMPTS_DIR,
) -> LoadedPrompt:
    """Load ``{name}/v{version}.md``, check its frontmatter and render the body with ``values``.

    Every value must be a string and must be used by the body, and every placeholder of the body must have
    a value. ``prompts_dir`` exists so tests can point at small fixture folders.
    """
    if not _NAME.fullmatch(name):
        raise PromptError(
            f"invalid prompt name {name!r}: it must be a plain identifier"
        )
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise PromptError(
            f"invalid prompt version {version!r}: it must be an integer >= 1"
        )
    path = prompts_dir / name / f"v{version}.md"
    if not path.is_file():
        raise PromptError(
            f"prompt {name!r} version {version} not found: no file {path}"
        )

    # CRLF -> LF so the hash is the same for a Windows and a Linux checkout of the same commit.
    content = path.read_bytes().replace(b"\r\n", b"\n")
    try:
        text = content.decode(
            "utf-8-sig"
        )  # -sig: a BOM written by an editor is accepted
    except UnicodeDecodeError as error:
        raise PromptError(
            f"{path.name}: the file is not valid UTF-8 ({error})"
        ) from error
    meta, body = _split_frontmatter(text, path)
    if meta["version"] != version:
        raise PromptError(
            f"{path.name}: frontmatter version {meta['version']} differs from the file number {version}"
        )

    system, user = _render(_split_parts(body, path), values, path)
    sha256 = hashlib.sha256(content).hexdigest()
    logger.debug("prompt loaded: name=%s version=%d sha256=%s", name, version, sha256)
    return LoadedPrompt(
        name=name,
        version=version,
        model=meta["model"],
        max_tokens=meta["max_tokens"],
        max_items=meta["max_items"],
        system=system,
        user=user,
        sha256=sha256,
    )


def _split_parts(body: str, path: Path) -> tuple[str, str]:
    """Split the template body into its system and user parts at the single ``---USER---`` line.

    This runs on the template, before any value is filled in: a value (catalogue text) containing a
    ``---USER---`` line can then never move the boundary between the two parts.
    """
    lines = body.split("\n")
    marks = [i for i, line in enumerate(lines) if line == _USER_SEPARATOR]
    if len(marks) != 1:
        raise PromptError(
            f"{path.name}: the body must have exactly one {_USER_SEPARATOR} line, found {len(marks)}"
        )
    system = "\n".join(lines[: marks[0]]).strip()
    user = "\n".join(lines[marks[0] + 1 :]).strip()
    if not system or not user:
        raise PromptError(
            f"{path.name}: the system part and the user part must not be empty"
        )
    return system, user


def _render(
    parts: tuple[str, str], values: Mapping[str, str], path: Path
) -> tuple[str, str]:
    """Fill the placeholders of both parts; any mismatch with ``values`` is an error, never ignored.

    A value may be used by either part, but it must be used by at least one.
    """
    for key, value in values.items():
        if not isinstance(value, str):
            raise PromptError(
                f"{path.name}: the value for {key!r} must be a string, got {type(value).__name__}"
            )
    templates = [Template(part) for part in parts]
    # a stray "$" or "$1"; "$$" is a literal dollar and is valid
    if not all(template.is_valid() for template in templates):
        raise PromptError(
            f"{path.name}: the body has an invalid placeholder (a stray '$'; write '$$' for one)"
        )
    used = {name for template in templates for name in template.get_identifiers()}
    if unused := set(values) - used:
        raise PromptError(
            f"{path.name}: value(s) {sorted(unused)} are not placeholders of the body"
        )
    try:
        system, user = (template.substitute(values) for template in templates)
    except KeyError as error:
        raise PromptError(
            f"{path.name}: no value for placeholder {error.args[0]!r}"
        ) from error
    # A part that is only a placeholder can still render empty, and the API rejects an empty message.
    if not system.strip() or not user.strip():
        raise PromptError(
            f"{path.name}: the system part or the user part is empty after filling in the values"
        )
    return system, user


def _split_frontmatter(content: str, path: Path) -> tuple[Frontmatter, str]:
    """Return the validated frontmatter and the body of a prompt file."""
    lines = content.split("\n")
    if lines[0] != _DELIMITER:
        raise PromptError(f"{path.name}: the file must start with a {_DELIMITER} line")
    try:
        end = lines.index(_DELIMITER, 1)
    except ValueError:
        raise PromptError(
            f"{path.name}: the frontmatter has no closing {_DELIMITER} line"
        ) from None

    try:
        meta = tomllib.loads("\n".join(lines[1:end]))
    except tomllib.TOMLDecodeError as error:
        raise PromptError(f"{path.name}: invalid TOML frontmatter ({error})") from error

    # The format is closed: an unexpected key (for example a schema name, or a temperature: current
    # models reject sampling parameters) is an error, not ignored.
    if extra := set(meta) - set(Frontmatter.__annotations__):
        raise PromptError(f"{path.name}: unexpected frontmatter key(s) {sorted(extra)}")
    version = _int(meta, "version", path)
    max_tokens = _int(meta, "max_tokens", path)
    max_items = _int(meta, "max_items", path)
    model = _get(meta, "model", path)
    if not isinstance(model, str):
        raise PromptError(
            f"{path.name}: frontmatter key 'model' has the wrong type ({model!r})"
        )

    frontmatter = Frontmatter(
        version=version,
        model=model,
        max_tokens=max_tokens,
        max_items=max_items,
    )
    for key in ("version", "max_tokens", "max_items"):
        if frontmatter[key] < 1:
            raise PromptError(
                f"{path.name}: {key} must be at least 1, got {frontmatter[key]}"
            )
    return frontmatter, "\n".join(lines[end + 1 :])


def _get(meta: dict, key: str, path: Path):
    if key not in meta:
        raise PromptError(f"{path.name}: frontmatter key {key!r} is missing")
    return meta[key]


def _int(meta: dict, key: str, path: Path) -> int:
    value = _get(meta, key, path)
    if isinstance(value, bool) or not isinstance(
        value, int
    ):  # bool is an int in Python
        raise PromptError(
            f"{path.name}: frontmatter key {key!r} has the wrong type ({value!r})"
        )
    return value
