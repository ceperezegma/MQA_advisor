"""Versioned prompt files, their loader and the registry of active versions (ADR-0018).

A prompt lives in ``prompts/{name}/v{N}.md`` next to this module: a TOML frontmatter between two ``+++``
lines, then a ``string.Template`` body. Files are immutable once published; a change is a new ``vN+1.md``.
The output schema of a prompt version is looked up by the same name and version in ``proposal_schemas``;
no field of the file names a schema.

``prompts/`` has no ``__init__.py``, so ``import mqa_advisor.prompts`` is this module, not the folder.
"""

import hashlib
import logging
import math
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
_NAME = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*"
)  # a plain ASCII identifier: cannot leave the prompts folder


class PromptError(ValueError):
    """A prompt file or request is invalid."""


class Frontmatter(TypedDict):
    """The validated frontmatter of a prompt file."""

    version: int
    model: str
    temperature: float
    max_tokens: int
    max_items: int


@dataclass(frozen=True)
class LoadedPrompt:
    """A prompt file read, validated and rendered: what a caller needs to build one model call."""

    name: str
    version: int
    model: str
    temperature: float
    max_tokens: int
    max_items: int  # the cap on proposed values; the callers enforce it in code, it is not in the schema
    text: str  # the body with every placeholder filled
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

    rendered = _render(body, values, path)
    sha256 = hashlib.sha256(content).hexdigest()
    logger.debug("prompt loaded: name=%s version=%d sha256=%s", name, version, sha256)
    return LoadedPrompt(
        name=name,
        version=version,
        model=meta["model"],
        temperature=meta["temperature"],
        max_tokens=meta["max_tokens"],
        max_items=meta["max_items"],
        text=rendered,
        sha256=sha256,
    )


def _render(body: str, values: Mapping[str, str], path: Path) -> str:
    """Fill the placeholders of ``body``; any mismatch with ``values`` is an error, never ignored."""
    for key, value in values.items():
        if not isinstance(value, str):
            raise PromptError(
                f"{path.name}: the value for {key!r} must be a string, got {type(value).__name__}"
            )
    template = Template(body)
    if (
        not template.is_valid()
    ):  # a stray "$" or "$1"; "$$" is a literal dollar and is valid
        raise PromptError(
            f"{path.name}: the body has an invalid placeholder (a stray '$'; write '$$' for one)"
        )
    if unused := set(values) - set(template.get_identifiers()):
        raise PromptError(
            f"{path.name}: value(s) {sorted(unused)} are not placeholders of the body"
        )
    try:
        return template.substitute(values)
    except KeyError as error:
        raise PromptError(
            f"{path.name}: no value for placeholder {error.args[0]!r}"
        ) from error


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

    # The format is closed: an unexpected key (for example a schema name) is an error, not ignored.
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
    temperature = _get(meta, "temperature", path)
    if isinstance(temperature, bool) or not isinstance(temperature, int | float):
        raise PromptError(
            f"{path.name}: frontmatter key 'temperature' has the wrong type ({temperature!r})"
        )
    if not math.isfinite(temperature) or not 0 <= temperature <= 1:
        raise PromptError(
            f"{path.name}: temperature must be between 0 and 1, got {temperature}"
        )

    frontmatter = Frontmatter(
        version=version,
        model=model,
        temperature=float(temperature),
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
