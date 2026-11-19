"""Live regression of the active prompts on the hand-picked examples (issue #24, ADR-0018).

Run with ``uv run pytest -m integration tests/test_prompt_regression_integration.py`` (needs
``ANTHROPIC_API_KEY`` in the environment or ``.env``). It calls the model, so it costs a few cents; the calls are
logged to ``logs/propose/`` like any run. Run it before a new prompt version becomes the active one.
"""

import os

import pytest
from dotenv import load_dotenv

from mqa_advisor.llm import AnthropicBackend, LLMClient, RunLog, load_api_key
from mqa_advisor.vocabulary import load_vocabulary
from tests.prompt_regression import check_example, load_examples, run_example

load_dotenv()
EXAMPLES = load_examples()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        reason="ANTHROPIC_API_KEY is not set (put it in .env to run the live prompt regression)",
    ),
]


@pytest.fixture(scope="module")
def live_results():
    """Every example run once on the live model; the run log carries the cost of each call."""
    vocabulary = load_vocabulary()
    results = {}
    with AnthropicBackend(load_api_key()) as backend:
        with RunLog("prompt-regression") as run_log:
            client = LLMClient(backend, run_log)
            for example in EXAMPLES:
                try:
                    results[example.name] = run_example(example, client, vocabulary)
                except (
                    Exception
                ) as error:  # one failing example must not hide the others
                    results[example.name] = error
            print(f"\nprompt regression log: {run_log.path}")
        print(
            f"prompt regression: {run_log.calls} calls, estimated cost USD {run_log.total_cost_usd:.6f}"
        )
    return vocabulary, results


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda e: e.name)
def test_example(example, live_results):
    vocabulary, results = live_results
    outcome = results[example.name]
    if isinstance(outcome, Exception):
        pytest.fail(f"{example.name}: the run raised {outcome!r}")
    themes, keywords = outcome
    failures = check_example(example, themes, keywords, vocabulary)
    assert not failures, "\n".join(failures)
