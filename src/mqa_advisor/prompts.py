"""Versioned prompt files, their loader and the registry of active versions (ADR-0018).

A prompt lives in ``prompts/{name}/v{N}.md`` next to this module: a TOML frontmatter between two ``+++``
lines, then a ``string.Template`` body. Files are immutable once published; a change is a new ``vN+1.md``.
The output schema of a prompt version is looked up by the same name and version in ``proposal_schemas``;
no field of the file names a schema.

``prompts/`` has no ``__init__.py``, so ``import mqa_advisor.prompts`` is this module, not the folder.
"""

import hashlib
import logging
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from string import Template

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"

# The version each prompt uses today. A caller that builds a call for version N loads N explicitly and
# never depends on this registry.
ACTIVE: dict[str, int] = {"propose_themes": 1, "propose_keywords": 1}

_DELIMITER = "+++"
# Frontmatter keys and their allowed types. bool is excluded explicitly: it is an int in Python.
_KEYS: dict[str, tuple[type, ...]] = {
    "version": (int,),
    "model": (str,),
    "temperature": (int, float),
    "max_tokens": (int,),
    "max_items": (int,),
}


class PromptError(ValueError):
    """A prompt file or request is invalid."""


@dataclass(frozen=True)
class LoadedPrompt:
    name: str
    version: int
    model: str
    temperature: float
    max_tokens: int
    max_items: int
    text: str  # the body with every placeholder filled
    sha256: str  # of the file content (line endings normalised)


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

    ``prompts_dir`` exists so tests can point at small fixture folders.
    """
    path = prompts_dir / name / f"v{version}.md"
    if not path.is_file():
        raise PromptError(
            f"prompt {name!r} version {version} not found: no file {path}"
        )

    # CRLF -> LF so the hash is the same for a Windows and a Linux checkout of the same commit.
    content = path.read_bytes().replace(b"\r\n", b"\n")
    meta, body = _split_frontmatter(content.decode("utf-8"), path)
    if meta["version"] != version:
        raise PromptError(
            f"{path.name}: frontmatter version {meta['version']} differs from the file number {version}"
        )

    try:
        text = Template(body).substitute(values)
    except KeyError as error:
        raise PromptError(
            f"{path.name}: no value for placeholder {error.args[0]!r}"
        ) from error
    except ValueError as error:
        raise PromptError(
            f"{path.name}: invalid placeholder in the body ({error})"
        ) from error

    sha256 = hashlib.sha256(content).hexdigest()
    logger.debug("prompt loaded: name=%s version=%d sha256=%s", name, version, sha256)
    return LoadedPrompt(
        name=name,
        version=version,
        model=meta["model"],
        temperature=meta["temperature"],
        max_tokens=meta["max_tokens"],
        max_items=meta["max_items"],
        text=text,
        sha256=sha256,
    )


def _split_frontmatter(content: str, path: Path) -> tuple[dict, str]:
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
    if extra := set(meta) - set(_KEYS):
        raise PromptError(f"{path.name}: unexpected frontmatter key(s) {sorted(extra)}")
    for key, types in _KEYS.items():
        if key not in meta:
            raise PromptError(f"{path.name}: frontmatter key {key!r} is missing")
        value = meta[key]
        if isinstance(value, bool) or not isinstance(value, types):
            raise PromptError(
                f"{path.name}: frontmatter key {key!r} has the wrong type ({value!r})"
            )
    for key in ("max_tokens", "max_items"):
        if meta[key] < 1:
            raise PromptError(f"{path.name}: {key} must be at least 1, got {meta[key]}")

    return meta, "\n".join(lines[end + 1 :])
