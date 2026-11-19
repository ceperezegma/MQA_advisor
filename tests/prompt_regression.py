"""The prompt regression examples and their loose checks (issue #24, ADR-0018).

``load_examples()`` reads ``tests/fixtures/prompts/regression_examples.json``. ``run_example()`` runs the active
theme and keyword prompts on one example through any ``LLMClient`` and ``check_example()`` returns the failures,
each naming the example and the property. The checks never ask for an exact match: they only say whether an
answer is usable.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from mqa_advisor.llm import LLMClient
from mqa_advisor.propose import (
    KEYWORD_CAP,
    THEME_CAP,
    KeywordResult,
    ResourceText,
    ThemeResult,
    propose_keywords,
    propose_themes,
)
from mqa_advisor.vocabulary import Vocabulary

EXAMPLES_PATH = (
    Path(__file__).parent / "fixtures" / "prompts" / "regression_examples.json"
)
THEME_URI_PREFIX = "http://publications.europa.eu/resource/authority/data-theme/"
ENGLISH_TAGS = {"en", "eng"}


@dataclass(frozen=True)
class Example:
    name: str
    case: str
    resource_type: str
    title: str
    title_language: str | None  # the title's tag; None = untagged
    description: str  # "" = no description
    existing_themes: tuple[str, ...]  # vocabulary codes
    existing_keywords: tuple[str, ...]
    accepted_themes: frozenset[str]  # vocabulary codes, at least one must be proposed
    keyword_pattern: (
        str | None
    )  # optional regex every keyword must match (script of the language)

    def resource(self) -> ResourceText:
        """The example as the resource the proposal functions read from the database."""
        tag = self.title_language
        return ResourceText(
            resource_pk=0,
            resource_id=self.name,
            resource_type=self.resource_type,
            entity_id=self.name,
            problem=None,
            titles=((tag, self.title),),
            descriptions=((tag, self.description),) if self.description else (),
            theme_uris=tuple(THEME_URI_PREFIX + c for c in self.existing_themes),
            keywords=tuple((None, k) for k in self.existing_keywords),
        )


def load_examples(path: Path = EXAMPLES_PATH) -> list[Example]:
    return [
        Example(
            name=o["name"],
            case=o["case"],
            resource_type=o["resource_type"],
            title=o["title"],
            title_language=o["title_language"],
            description=o["description"],
            existing_themes=tuple(o["existing_themes"]),
            existing_keywords=tuple(o["existing_keywords"]),
            accepted_themes=frozenset(o["accepted_themes"]),
            keyword_pattern=o.get("keyword_pattern"),
        )
        for o in json.loads(path.read_text(encoding="utf-8"))
    ]


def run_example(
    example: Example, client: LLMClient, vocabulary: Vocabulary
) -> tuple[ThemeResult, KeywordResult]:
    """Run the active theme prompt, then the keyword prompt with the proposed themes as context."""
    resource = example.resource()
    themes = propose_themes(resource, client, vocabulary)
    keywords = propose_keywords(resource, client, themes, vocabulary)
    return themes, keywords


def _is_english(tag: str | None) -> bool:
    return tag is not None and tag.lower() in ENGLISH_TAGS


def check_example(
    example: Example,
    themes: ThemeResult,
    keywords: KeywordResult,
    vocabulary: Vocabulary,
) -> list[str]:
    """The failed checks, each ``"{example}: {property}: {detail}"``; empty when all pass."""
    failures: list[str] = []

    def fail(prop: str, detail: str) -> None:
        failures.append(f"{example.name}: {prop}: {detail}")

    if themes.status != "ok":
        fail("themes", f"status {themes.status}: {[i.message for i in themes.issues]}")
    else:
        known = set(vocabulary.uris_by_code.values())
        if unknown := [t for t in themes.themes if t not in known]:
            fail("theme codes in the vocabulary", f"not in it: {unknown}")
        limit = THEME_CAP - len(themes.existing_themes)
        if not 1 <= len(themes.themes) <= limit:
            fail("number of new themes", f"{len(themes.themes)}, expected 1..{limit}")
        accepted = {THEME_URI_PREFIX + c for c in example.accepted_themes}
        if not accepted & set(themes.themes):
            got = [t.rsplit("/", 1)[1] for t in themes.themes]
            fail(
                "accepted themes",
                f"none of {sorted(example.accepted_themes)} in {got}",
            )

    if keywords.status != "ok":
        fail(
            "keywords",
            f"status {keywords.status}: {[i.message for i in keywords.issues]}",
        )
        return failures
    limit = KEYWORD_CAP - keywords.existing_count
    if not 1 <= len(keywords.keywords) <= limit:
        fail("number of new keywords", f"{len(keywords.keywords)}, expected 1..{limit}")
    if example.title_language is not None:
        if keywords.lang != example.title_language:
            fail(
                "keyword language",
                f"{keywords.lang!r}, expected the title's {example.title_language!r}",
            )
    elif not keywords.lang_inferred or _is_english(keywords.lang):
        # An untagged title: the model names the language. The only untagged example is not English.
        fail("keyword language", f"{keywords.lang!r} inferred for a non-English title")
    if example.keyword_pattern and (
        bad := [
            k for k in keywords.keywords if not re.search(example.keyword_pattern, k)
        ]
    ):
        fail("keywords in the title's language", f"no {example.keyword_pattern}: {bad}")
    return failures
