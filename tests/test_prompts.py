"""Tests for the prompt loader, the registry and the pairing with the output schemas."""

import hashlib
import logging
from pathlib import Path
from string import Template

import pytest

from mqa_advisor import prompts
from mqa_advisor.prompts import PROMPTS_DIR, PromptError, load_prompt
from mqa_advisor.proposal_schemas import output_schema, schema_keys

FRONTMATTER = 'version = {version}\nmodel = "m"\ntemperature = 0\nmax_tokens = 100\nmax_items = 4\n'


def write_prompt(
    tmp_path: Path,
    name: str,
    version: int,
    body: str = "Hello $who",
    *,
    frontmatter: str | None = None,
) -> Path:
    """Write ``{name}/v{version}.md`` under ``tmp_path`` and return the prompts folder."""
    if frontmatter is None:
        frontmatter = FRONTMATTER.format(version=version)
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"v{version}.md").write_text(
        f"+++\n{frontmatter}+++\n{body}", encoding="utf-8", newline=""
    )
    return tmp_path


def committed_prompts() -> set[tuple[str, int]]:
    return {(p.parent.name, int(p.stem[1:])) for p in PROMPTS_DIR.glob("*/v*.md")}


def placeholders_of(name: str, version: int) -> set[str]:
    text = (PROMPTS_DIR / name / f"v{version}.md").read_text(encoding="utf-8")
    return set(Template(text).get_identifiers())


# --- loading -----------------------------------------------------------------------------------


def test_load_returns_frontmatter_text_and_hash(tmp_path):
    folder = write_prompt(tmp_path, "p", 1, "Hello $who, bye")
    loaded = load_prompt("p", 1, {"who": "Ann"}, prompts_dir=folder)
    assert (loaded.name, loaded.version) == ("p", 1)
    assert (loaded.model, loaded.temperature, loaded.max_tokens, loaded.max_items) == (
        "m",
        0,
        100,
        4,
    )
    assert loaded.text == "Hello Ann, bye"
    assert (
        loaded.sha256
        == hashlib.sha256((folder / "p" / "v1.md").read_bytes()).hexdigest()
    )


def test_hash_follows_content(tmp_path):
    folder = write_prompt(tmp_path, "p", 1, "Hello $who")
    first = load_prompt("p", 1, {"who": "x"}, prompts_dir=folder).sha256
    assert load_prompt("p", 1, {"who": "x"}, prompts_dir=folder).sha256 == first
    write_prompt(tmp_path, "p", 1, "Hello $who!")
    assert load_prompt("p", 1, {"who": "x"}, prompts_dir=folder).sha256 != first


def test_crlf_file_has_the_same_hash_as_lf(tmp_path):
    lf = write_prompt(tmp_path / "lf", "p", 1, "line one\nline two $who\n")
    crlf = write_prompt(tmp_path / "crlf", "p", 1, "line one\nline two $who\n")
    path = crlf / "p" / "v1.md"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    values = {"who": "x"}
    assert (
        load_prompt("p", 1, values, prompts_dir=crlf).sha256
        == load_prompt("p", 1, values, prompts_dir=lf).sha256
    )


def test_missing_placeholder_value_raises_naming_it(tmp_path):
    folder = write_prompt(tmp_path, "p", 1, "Hello $who and $other")
    with pytest.raises(PromptError, match="other"):
        load_prompt("p", 1, {"who": "Ann"}, prompts_dir=folder)


def test_extra_values_are_ignored(tmp_path):
    folder = write_prompt(tmp_path, "p", 1, "Hello $who")
    assert (
        load_prompt("p", 1, {"who": "Ann", "unused": "z"}, prompts_dir=folder).text
        == "Hello Ann"
    )


def test_load_logs_name_version_and_hash_at_debug(tmp_path, caplog):
    folder = write_prompt(tmp_path, "p", 1)
    with caplog.at_level(logging.DEBUG, logger="mqa_advisor.prompts"):
        loaded = load_prompt("p", 1, {"who": "x"}, prompts_dir=folder)
    [record] = caplog.records
    message = record.getMessage()
    assert record.levelno == logging.DEBUG
    assert "name=p" in message and "version=1" in message and loaded.sha256 in message


# --- errors ------------------------------------------------------------------------------------


def test_version_must_equal_the_file_number(tmp_path):
    folder = write_prompt(tmp_path, "p", 2, frontmatter=FRONTMATTER.format(version=1))
    with pytest.raises(PromptError, match=r"v2\.md.*version 1.*number 2"):
        load_prompt("p", 2, {"who": "x"}, prompts_dir=folder)


def test_extra_frontmatter_key_raises(tmp_path):
    folder = write_prompt(
        tmp_path, "p", 1, frontmatter=FRONTMATTER.format(version=1) + 'schema = "x"\n'
    )
    with pytest.raises(PromptError, match="schema"):
        load_prompt("p", 1, {"who": "x"}, prompts_dir=folder)


@pytest.mark.parametrize(
    "frontmatter",
    [
        'version = 1\nmodel = "m"\ntemperature = 0\nmax_tokens = 100\n',  # max_items missing
        "version = 1\nmodel = 5\ntemperature = 0\nmax_tokens = 100\nmax_items = 4\n",  # wrong type
        'version = true\nmodel = "m"\ntemperature = 0\nmax_tokens = 100\nmax_items = 4\n',  # bool is not int
        'version = 1\nmodel = "m"\ntemperature = 0\nmax_tokens = 100\nmax_items = 0\n',  # below 1
        'version = 1\nmodel = "m"\ntemperature = 0\nmax_tokens = 0\nmax_items = 4\n',  # below 1
        "version = 1\nmodel = \n",  # invalid TOML
    ],
)
def test_bad_frontmatter_raises(tmp_path, frontmatter):
    folder = write_prompt(tmp_path, "p", 1, frontmatter=frontmatter)
    with pytest.raises(PromptError, match=r"v1\.md"):
        load_prompt("p", 1, {"who": "x"}, prompts_dir=folder)


@pytest.mark.parametrize(
    "text", ["no frontmatter at all\n", "+++\nversion = 1\nno closing line\n"]
)
def test_missing_frontmatter_delimiters_raise(tmp_path, text):
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "v1.md").write_text(text, encoding="utf-8")
    with pytest.raises(PromptError, match=r"v1\.md"):
        load_prompt("p", 1, {}, prompts_dir=tmp_path)


def test_unknown_name_or_version_raises(tmp_path):
    folder = write_prompt(tmp_path, "p", 1)
    with pytest.raises(PromptError, match="nope"):
        load_prompt("nope", 1, {}, prompts_dir=folder)
    with pytest.raises(PromptError, match="version 7"):
        load_prompt("p", 7, {}, prompts_dir=folder)


# --- registry ----------------------------------------------------------------------------------


def test_active_versions():
    assert prompts.active_version("propose_themes") == 1
    assert prompts.active_version("propose_keywords") == 1


def test_active_version_of_unknown_name_raises():
    with pytest.raises(PromptError, match="nope"):
        prompts.active_version("nope")


def test_active_version_without_a_file_raises(monkeypatch):
    monkeypatch.setitem(prompts.ACTIVE, "propose_themes", 99)
    with pytest.raises(PromptError, match="version 99"):
        load_prompt("propose_themes", prompts.active_version("propose_themes"), {})


# --- committed files and pairing with the schemas ----------------------------------------------


def test_every_prompt_file_has_a_schema_and_the_reverse():
    assert committed_prompts() == schema_keys()


def test_active_versions_exist_as_files():
    assert set(prompts.ACTIVE.items()) <= committed_prompts()


@pytest.mark.parametrize(("name", "version"), sorted(committed_prompts()))
def test_committed_prompt_loads_with_a_value_for_every_placeholder(name, version):
    placeholders = placeholders_of(name, version)
    loaded = load_prompt(name, version, {key: f"<{key}>" for key in placeholders})
    assert loaded.model == "claude-haiku-5-5"
    assert loaded.temperature == 0
    assert loaded.max_tokens >= 1 and loaded.max_items >= 1
    assert all(f"<{key}>" in loaded.text for key in placeholders)
    assert "$" not in loaded.text
    # The schema of the same name and version accepts the prompt's own limit.
    assert output_schema(name, version, loaded.max_items)


def test_a_call_for_version_n_uses_schema_n_even_when_active_differs(monkeypatch):
    monkeypatch.setitem(prompts.ACTIVE, "propose_keywords", 2)
    values = {key: "x" for key in placeholders_of("propose_keywords", 1)}
    loaded = load_prompt("propose_keywords", 1, values)
    schema = output_schema(loaded.name, loaded.version, loaded.max_items)
    assert schema == output_schema("propose_keywords", 1, loaded.max_items)
