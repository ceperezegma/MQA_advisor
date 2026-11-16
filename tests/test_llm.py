"""Tests for the LLM client: cost, schema validation, the backends and the run log. No test uses the network."""

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import httpx2
import pytest
from jsonschema import Draft202012Validator

from mqa_advisor import llm
from mqa_advisor.llm import (
    AnthropicBackend,
    BackendResponse,
    CallRecord,
    FakeBackend,
    LLMCallError,
    LLMClient,
    LLMError,
    LLMInputError,
    LLMResponseError,
    ModelPrice,
    RunLog,
    Usage,
    estimate_cost,
    load_api_key,
)
from mqa_advisor.prompts import load_prompt
from mqa_advisor.proposal_schemas import output_schema, schema_keys

SECRET = "sk-test-secret"


def themes_prompt(**overrides: str):
    values = {
        "vocabulary": "ENVI — Environment",
        "title": "Air quality",
        "description": "Hourly NO2 values",
        "existing_themes": "(none)",
        "remaining": "3",
    }
    values.update(overrides)
    return load_prompt("propose_themes", 1, values)


def ok_response(**overrides) -> BackendResponse:
    fields = {
        "output": {"themes": ["ENVI"]},
        "raw_response": '[{"type": "tool_use"}]',
        "input_tokens": 1000,
        "cached_tokens": 200,
        "output_tokens": 500,
        "attempt": 1,
    }
    fields.update(overrides)
    return BackendResponse(**fields)


# The cost of the default response above on Haiku 5.5, computed by hand:
# (1000 x 0.10 + 200 x 0.01 + 500 x 0.50) / 1,000,000
HAIKU_COST = 0.000352


@pytest.fixture
def new_run_log(tmp_path):
    """Create run logs under ``tmp_path`` and close them all at the end, so no file handle outlives a test."""
    created = []

    def make(catalogue="cat", **kwargs) -> RunLog:
        kwargs.setdefault("log_dir", tmp_path / "logs")
        run_log = RunLog(catalogue, **kwargs)
        created.append(run_log)
        return run_log

    yield make
    for run_log in created:
        try:
            run_log.close()
        except LLMError:
            pass  # a test of a write failure has already looked at it


@pytest.fixture
def make_client(new_run_log):
    def make(script, *, prices=None):
        backend = FakeBackend(script)
        run_log = new_run_log()
        kwargs = {} if prices is None else {"prices": prices}
        return LLMClient(backend, run_log, **kwargs), backend, run_log

    return make


def log_text(run_log: RunLog) -> str:
    run_log.close()
    return run_log.path.read_text(encoding="utf-8")


# --- price table and cost ----------------------------------------------------------------------


def test_cost_is_tokens_times_price_per_million_including_cached_tokens():
    prices = {"m": ModelPrice(input=2.0, output=10.0, cached_input=0.5)}
    cost = estimate_cost("m", 1000, 200, 500, prices)
    assert cost == pytest.approx((1000 * 2.0 + 200 * 0.5 + 500 * 10.0) / 1_000_000)


def test_the_committed_haiku_prices_give_the_hand_computed_cost():
    # Pins the real table: a typo in a price must turn this red.
    assert estimate_cost("claude-haiku-5-5", 1000, 200, 500) == pytest.approx(
        HAIKU_COST
    )
    # Each rate on its own, per million tokens (the input side stays under the 100,000-token limit).
    assert estimate_cost("claude-haiku-5-5", 100_000, 0, 0) == pytest.approx(0.01)
    assert estimate_cost("claude-haiku-5-5", 0, 100_000, 0) == pytest.approx(0.001)
    assert estimate_cost("claude-haiku-5-5", 0, 0, 1_000_000) == pytest.approx(0.50)


def test_unknown_model_raises_naming_it():
    with pytest.raises(LLMInputError, match="no-such-model"):
        estimate_cost("no-such-model", 1, 0, 1)


def test_price_table_has_an_iso_date_and_only_the_model_in_use():
    assert datetime.strptime(llm.PRICES_AS_OF, "%Y-%m-%d")
    assert set(llm.PRICES) == {"claude-haiku-5-5"}
    with pytest.raises(TypeError):
        llm.PRICES["other"] = ModelPrice(1, 1, 1)  # read-only


def test_a_prompt_longer_than_the_price_table_covers_raises():
    limit = llm.MAX_PRICED_PROMPT_TOKENS
    estimate_cost("claude-haiku-5-5", limit, 0, 1)  # exactly at the limit is priced
    with pytest.raises(LLMInputError, match="price table"):
        estimate_cost("claude-haiku-5-5", limit - 5, 6, 1)  # cached tokens count too


# --- the client with the fake backend ----------------------------------------------------------


def test_successful_call_returns_output_usage_cost_latency_and_attempt(make_client):
    client, _, _ = make_client([ok_response(attempt=2)])
    result = client.call(themes_prompt(), resource="ds-1")
    assert result.output == {"themes": ["ENVI"]}
    assert (result.input_tokens, result.cached_tokens, result.output_tokens) == (
        1000,
        200,
        500,
    )
    assert result.cost_usd == pytest.approx(HAIKU_COST)
    assert result.latency_s >= 0
    assert result.attempt == 2


def test_request_carries_system_and_user_separately_and_the_versions_schema(
    make_client,
):
    client, backend, _ = make_client([ok_response(), ok_response()])
    prompt = themes_prompt()
    client.call(prompt, resource="a")
    client.call(prompt, resource="b")
    first, second = backend.requests
    assert first.system == prompt.system and first.user == prompt.user
    assert "Air quality" in first.user and "Air quality" not in first.system
    assert "ENVI — Environment" in first.system  # the fixed list is in the system part
    assert (first.model, first.max_tokens) == (prompt.model, prompt.max_tokens)
    assert first.tool_name == "propose_themes"
    assert first.tool_schema == output_schema("propose_themes", 1)
    assert second.user == first.user  # both requests recorded, in order


def test_the_request_has_no_sampling_parameters():
    assert not set(llm.BackendRequest.__dataclass_fields__) & {
        "temperature",
        "top_p",
        "top_k",
    }


def test_the_fake_backend_fails_loudly_when_its_script_runs_out(make_client):
    client, _, _ = make_client([ok_response()])
    client.call(themes_prompt(), resource="a")
    with pytest.raises(AssertionError, match="more calls"):
        client.call(themes_prompt(), resource="b")


@pytest.mark.parametrize(
    "output",
    [
        {"themes": []},  # minItems
        {},  # missing required key
        {"themes": "ENVI"},  # wrong type
        {"themes": ["ENVI"], "extra": 1},  # additionalProperties
        ["ENVI"],  # not even an object
    ],
)
def test_response_not_matching_the_schema_raises_and_is_logged_with_its_tokens_and_cost(
    make_client, output
):
    raw = json.dumps(output)
    client, _, run_log = make_client(
        [ok_response(output=output, raw_response=raw, attempt=2)]
    )
    with pytest.raises(LLMResponseError, match="propose_themes v1") as error:
        client.call(themes_prompt(), resource="ds-1")
    assert error.value.raw_response == raw
    assert error.value.usage == Usage(1000, 200, 500) and error.value.attempt == 2
    text = log_text(run_log)
    assert raw in text and "does not match the schema" in text
    # The call was billed, so its tokens and cost are in the block and in the run total.
    assert "tokens: input=1000 cached=200 output=500" in text
    assert "attempt: 2" in text
    assert run_log.total_cost_usd == pytest.approx(HAIKU_COST)


def test_an_answer_cut_off_by_max_tokens_says_so(make_client):
    cut = ok_response(output={"themes": []}, stop_reason="max_tokens")
    client, _, _ = make_client([cut])
    with pytest.raises(LLMResponseError, match="cut off.*max_tokens"):
        client.call(themes_prompt(), resource="ds-1")


KEYWORDS_VALUES = {
    "title": "t",
    "description": "d",
    "language": "en",
    "themes": "(none)",
    "existing_keywords": "(none)",
    "remaining": "3",
}


@pytest.mark.parametrize("language", ["en", "eng", "de"])
def test_keywords_output_with_a_valid_language_is_accepted(make_client, language):
    prompt = load_prompt("propose_keywords", 1, KEYWORDS_VALUES)
    answer = {"keywords": ["river"], "language": language}
    client, _, _ = make_client([ok_response(output=answer)])
    assert client.call(prompt, resource="ds-1").output == answer


@pytest.mark.parametrize("language", ["English", "EN", "e", "unknown", "e1", ""])
def test_keywords_output_with_an_invalid_language_is_rejected(make_client, language):
    prompt = load_prompt("propose_keywords", 1, KEYWORDS_VALUES)
    client, _, _ = make_client(
        [ok_response(output={"keywords": ["river"], "language": language})]
    )
    with pytest.raises(LLMResponseError, match="language"):
        client.call(prompt, resource="ds-1")


@pytest.mark.parametrize(("name", "version"), sorted(schema_keys()))
def test_the_committed_schemas_are_valid_json_schemas(name, version):
    Draft202012Validator.check_schema(output_schema(name, version))


def test_unknown_model_makes_no_call_and_writes_no_block(make_client):
    client, backend, run_log = make_client([ok_response()], prices={})
    with pytest.raises(LLMInputError, match="claude-haiku-5-5"):
        client.call(themes_prompt(), resource="ds-1")
    assert backend.requests == [] and run_log.calls == 0


def test_a_prompt_over_the_size_limit_is_refused_before_any_request(make_client):
    # 100,000 bytes can never be more than 100,000 tokens; one byte more could be, so it is refused unsent.
    client, backend, run_log = make_client([ok_response()])
    with pytest.raises(LLMInputError, match="cannot be priced"):
        client.call(
            themes_prompt(description="x" * llm.MAX_PRICED_PROMPT_TOKENS),
            resource="ds-1",
        )
    assert backend.requests == [] and run_log.calls == 0


def test_the_size_limit_counts_bytes_not_characters(make_client):
    client, backend, _ = make_client([ok_response()])
    description = "é" * (
        llm.MAX_PRICED_PROMPT_TOKENS // 2
    )  # 2 bytes each: over the limit with the prompt
    with pytest.raises(LLMInputError):
        client.call(themes_prompt(description=description), resource="ds-1")
    assert backend.requests == []


def test_a_call_billed_over_the_price_table_range_is_logged_as_unpriced_and_raises(
    make_client,
):
    big = ok_response(input_tokens=llm.MAX_PRICED_PROMPT_TOKENS + 1)
    client, _, run_log = make_client([big, ok_response()])
    with pytest.raises(LLMInputError, match="price table"):
        client.call(themes_prompt(), resource="ds-1")
    client.call(themes_prompt(), resource="ds-2")
    text = log_text(run_log)
    assert "billed but not priced" in text and "unpriced" in text
    assert (
        f"input={llm.MAX_PRICED_PROMPT_TOKENS + 1}" in text
    )  # the real tokens are kept
    assert run_log.unpriced_calls == 1
    assert run_log.total_cost_usd == pytest.approx(
        HAIKU_COST
    )  # the unpriced call is not counted as 0 cost
    assert "1 unpriced call(s)" in text


# --- failures are logged and re-raised ---------------------------------------------------------


def test_an_api_failure_is_logged_with_no_tokens_and_zero_cost(make_client):
    failure = LLMCallError("the API call failed after 4 attempt(s)", attempt=4)
    client, _, run_log = make_client([failure, ok_response()])
    with pytest.raises(LLMCallError):
        client.call(themes_prompt(), resource="ds-1")
    client.call(themes_prompt(), resource="ds-2")
    text = log_text(run_log)
    assert "outcome: error: the API call failed" in text
    assert "attempt: 4" in text and "tokens: input=None cached=None output=None" in text
    assert "estimated cost USD: 0.000000" in text
    assert run_log.total_cost_usd == pytest.approx(
        HAIKU_COST
    )  # only the call that worked


def test_a_response_without_a_tool_call_is_logged_with_its_usage(make_client):
    failure = LLMResponseError(
        "no tool call", "[raw]", attempt=2, usage=Usage(100, 0, 20)
    )
    client, _, run_log = make_client([failure])
    with pytest.raises(LLMResponseError):
        client.call(themes_prompt(), resource="ds-1")
    text = log_text(run_log)
    assert (
        "tokens: input=100 cached=0 output=20" in text
        and "attempt: 2" in text
        and "[raw]" in text
    )
    assert run_log.total_cost_usd == pytest.approx(
        estimate_cost("claude-haiku-5-5", 100, 0, 20)
    )


def test_an_unexpected_exception_from_the_backend_is_logged_and_reraised(make_client):
    client, _, run_log = make_client([RuntimeError("boom")])
    with pytest.raises(RuntimeError, match="boom"):
        client.call(themes_prompt(), resource="ds-1")
    assert "outcome: error: unexpected RuntimeError: boom" in log_text(run_log)
    assert run_log.calls == 1


# --- the real backend, on a mock HTTP transport ------------------------------------------------


def message_json(**usage) -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [
            {
                "type": "tool_use",
                "id": "tu_1",
                "name": "propose_themes",
                "input": {"themes": ["ENVI"]},
            }
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 20} | usage,
    }


def backend_with(handler) -> AnthropicBackend:
    return AnthropicBackend(SECRET, transport=httpx2.MockTransport(handler))


def replies(*responses):
    """A handler that answers with each response in turn and records the requests."""
    seen = []
    queue = iter(responses)

    def handler(request):
        seen.append(request)
        item = next(queue)
        if isinstance(item, Exception):
            raise item
        return item

    handler.seen = seen
    return handler


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    # The SDK waits between retries; tests must not.
    monkeypatch.setattr("anthropic._base_client.time.sleep", lambda seconds: None)


def warnings_of(caplog):
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "mqa_advisor.llm"
    ]


def test_real_backend_is_configured_with_three_retries_and_a_60_second_timeout():
    backend = backend_with(lambda request: httpx2.Response(200, json=message_json()))
    assert backend._client.max_retries == 3
    assert backend._client.timeout == 60.0


def test_real_backend_can_be_closed_and_used_as_a_context_manager():
    with backend_with(
        lambda request: httpx2.Response(200, json=message_json())
    ) as backend:
        assert not backend._client.is_closed()
    assert backend._client.is_closed()


def test_real_backend_sends_a_forced_tool_call_without_sampling_parameters(new_run_log):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json=message_json(
                cache_read_input_tokens=30, cache_creation_input_tokens=5
            ),
        )

    client = LLMClient(backend_with(handler), new_run_log())
    prompt = themes_prompt()
    result = client.call(prompt, resource="ds-1")

    [body] = seen
    assert (
        body["model"] == "claude-haiku-5-5" and body["max_tokens"] == prompt.max_tokens
    )
    assert not {"temperature", "top_p", "top_k"} & set(body)
    assert body["tool_choice"] == {"type": "tool", "name": "propose_themes"}
    [tool] = body["tools"]
    assert tool["name"] == "propose_themes"
    assert tool["input_schema"] == output_schema("propose_themes", 1)
    system_text = (
        body["system"] if isinstance(body["system"], str) else body["system"][0]["text"]
    )
    assert system_text == prompt.system
    assert body["messages"] == [{"role": "user", "content": prompt.user}]
    # Cache-creation tokens are counted as plain input.
    assert (result.input_tokens, result.cached_tokens, result.output_tokens) == (
        105,
        30,
        20,
    )
    assert result.output == {"themes": ["ENVI"]} and result.attempt == 1


@pytest.mark.parametrize("status", [429, 408, 409, 500, 529])
def test_a_transient_error_is_retried_logged_as_a_warning_and_counted(
    new_run_log, caplog, status
):
    handler = replies(
        httpx2.Response(status, json={"error": "x"}),
        httpx2.Response(200, json=message_json()),
    )
    client = LLMClient(backend_with(handler), new_run_log())
    with caplog.at_level(logging.WARNING, logger="mqa_advisor.llm"):
        result = client.call(themes_prompt(), resource="ds-1")
    assert result.attempt == 2 and len(handler.seen) == 2
    [warning] = warnings_of(caplog)
    assert (
        f"HTTP {status}" in warning.getMessage() and "attempt 1" in warning.getMessage()
    )


@pytest.mark.parametrize("status", [400, 401, 404])
def test_a_client_error_is_not_retried(new_run_log, caplog, status):
    handler = replies(
        httpx2.Response(status, json={"error": {"type": "x", "message": "nope"}})
    )
    run_log = new_run_log()
    client = LLMClient(backend_with(handler), run_log)
    with caplog.at_level(logging.WARNING, logger="mqa_advisor.llm"):
        with pytest.raises(LLMCallError, match="1 attempt") as error:
            client.call(themes_prompt(), resource="ds-1")
    assert (
        len(handler.seen) == 1
        and error.value.attempt == 1
        and warnings_of(caplog) == []
    )
    assert error.value.__cause__ is not None  # chained to the SDK error
    assert "attempt: 1" in log_text(run_log)


def test_retries_exhausted_raises_call_error_after_four_requests_and_logs_it(
    new_run_log, caplog
):
    handler = replies(*[httpx2.Response(500, json={"error": "boom"}) for _ in range(4)])
    run_log = new_run_log()
    client = LLMClient(backend_with(handler), run_log)
    with caplog.at_level(logging.WARNING, logger="mqa_advisor.llm"):
        with pytest.raises(LLMCallError, match="4 attempt") as error:
            client.call(themes_prompt(), resource="ds-1")
    assert (
        len(handler.seen) == 4 and error.value.attempt == 4
    )  # first request + 3 retries
    assert len(warnings_of(caplog)) == 3  # the last one is not retried
    assert error.value.__cause__ is not None
    assert "attempt: 4" in log_text(run_log)


def test_a_retry_that_ends_in_a_client_error_counts_both_attempts(new_run_log):
    handler = replies(
        httpx2.Response(500, json={"error": "x"}),
        httpx2.Response(400, json={"error": "bad"}),
    )
    client = LLMClient(backend_with(handler), new_run_log())
    with pytest.raises(LLMCallError, match="2 attempt"):
        client.call(themes_prompt(), resource="ds-1")


def test_a_connection_error_is_retried_and_ends_in_a_call_error(new_run_log):
    handler = replies(*[httpx2.ConnectError("no route") for _ in range(4)])
    client = LLMClient(backend_with(handler), new_run_log())
    with pytest.raises(LLMCallError, match="4 attempt") as error:
        client.call(themes_prompt(), resource="ds-1")
    assert len(handler.seen) == 4 and error.value.__cause__ is not None


def test_each_thread_counts_its_own_attempts(new_run_log):
    # One backend is shared by worker threads: the thread whose first request fails must report attempt 2,
    # and the other attempt 1, whatever the interleaving.
    failed_once = set()
    lock = threading.Lock()

    def handler(request):
        title = json.loads(request.content)["messages"][0]["content"]
        with lock:
            if "slow-title" in title and "slow-title" not in failed_once:
                failed_once.add("slow-title")
                return httpx2.Response(500, json={"error": "x"})
        return httpx2.Response(200, json=message_json())

    client = LLMClient(backend_with(handler), new_run_log())

    def run(title):
        return client.call(themes_prompt(title=title), resource=title).attempt

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(pool.map(run, ["slow-title", "fast-title"]))
    assert attempts == [2, 1]


def test_a_response_without_the_tool_call_raises_with_the_raw_response_and_usage(
    new_run_log,
):
    body = message_json()
    body["content"] = [{"type": "text", "text": "I refuse"}]
    body["stop_reason"] = "end_turn"
    run_log = new_run_log()
    client = LLMClient(
        backend_with(lambda request: httpx2.Response(200, json=body)), run_log
    )
    with pytest.raises(
        LLMResponseError, match="no 'propose_themes' tool call"
    ) as error:
        client.call(themes_prompt(), resource="ds-1")
    assert "I refuse" in error.value.raw_response
    assert error.value.usage == Usage(100, 0, 20) and error.value.attempt == 1
    assert "tokens: input=100 cached=0 output=20" in log_text(
        run_log
    )  # billed, so the log shows it


def test_the_tool_call_is_found_among_other_content_blocks(new_run_log):
    body = message_json()
    other = {
        "type": "tool_use",
        "id": "tu_0",
        "name": "another_tool",
        "input": {"x": 1},
    }
    text = {"type": "text", "text": "Thinking aloud"}
    body["content"] = [text, other, *body["content"]]
    client = LLMClient(
        backend_with(lambda request: httpx2.Response(200, json=body)), new_run_log()
    )
    assert client.call(themes_prompt(), resource="ds-1").output == {"themes": ["ENVI"]}


def test_a_truncated_answer_is_a_clear_error(new_run_log):
    body = message_json()
    body["content"][0]["input"] = {"themes": []}  # what a cut-off call looks like
    body["stop_reason"] = "max_tokens"
    client = LLMClient(
        backend_with(lambda request: httpx2.Response(200, json=body)), new_run_log()
    )
    with pytest.raises(LLMResponseError, match="cut off"):
        client.call(themes_prompt(), resource="ds-1")


# --- the API key -------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_or_blank_key_raises_before_any_call(monkeypatch, value):
    monkeypatch.setattr(
        llm, "load_dotenv", lambda: None
    )  # do not read the developer's real .env
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    if value is not None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", value)
    with pytest.raises(LLMInputError, match="ANTHROPIC_API_KEY"):
        load_api_key()


def test_key_is_read_from_the_environment(monkeypatch):
    monkeypatch.setattr(llm, "load_dotenv", lambda: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", f"  {SECRET}  ")
    assert load_api_key() == SECRET


def test_the_key_never_appears_in_the_log_file_or_log_records_on_success(
    new_run_log, caplog
):
    handler = replies(
        httpx2.Response(500, json={"error": "boom"}),
        httpx2.Response(200, json=message_json()),
    )
    run_log = new_run_log()
    client = LLMClient(backend_with(handler), run_log)
    with caplog.at_level(logging.DEBUG):
        client.call(themes_prompt(), resource="ds-1")
        text = log_text(run_log)
    assert SECRET not in text
    assert all(SECRET not in record.getMessage() for record in caplog.records)


def test_the_key_is_scrubbed_when_an_api_error_echoes_it(new_run_log, caplog):
    body = {
        "error": {
            "type": "authentication_error",
            "message": f"invalid x-api-key {SECRET}",
        }
    }
    run_log = new_run_log()
    client = LLMClient(backend_with(replies(httpx2.Response(401, json=body))), run_log)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LLMCallError) as error:
            client.call(themes_prompt(), resource="ds-1")
        text = log_text(run_log)
    assert "invalid x-api-key ***" in text  # the useful part of the error is kept
    assert SECRET not in text and SECRET not in str(error.value)
    assert all(SECRET not in record.getMessage() for record in caplog.records)


# --- the run log -------------------------------------------------------------------------------


def test_the_log_block_has_every_field(make_client):
    client, _, run_log = make_client([ok_response(attempt=2)])
    prompt = themes_prompt()
    client.call(prompt, resource="ds-1")
    text = log_text(run_log)
    for expected in [
        "===== call 1 =====",
        f"run id: {run_log.run_id}",
        "resource: ds-1",
        f"propose_themes v1 sha256={prompt.sha256}",
        "model: claude-haiku-5-5",
        "outcome: ok",
        "tokens: input=1000 cached=200 output=500",
        "estimated cost USD: 0.000352",
        "latency s:",
        "attempt: 2",
        "----- system -----",
        prompt.system,
        "----- user -----",
        prompt.user,
        "----- response -----",
        '[{"type": "tool_use"}]',
        "===== end call 1 =====",
    ]:
        assert expected in text, expected


def test_non_ascii_text_is_written_as_utf_8(make_client):
    client, _, run_log = make_client([ok_response(raw_response="Ünï — €")])
    client.call(themes_prompt(title="Straße — €"), resource="ds-é")
    text = log_text(run_log)
    assert "Straße — €" in text and "Ünï — €" in text and "ds-é" in text


def test_an_info_line_per_call_carries_the_resource_and_cost(make_client, caplog):
    client, _, run_log = make_client([ok_response(), LLMCallError("down", attempt=1)])
    with caplog.at_level(logging.INFO):
        client.call(themes_prompt(), resource="ds-1")
        with pytest.raises(LLMCallError):
            client.call(themes_prompt(), resource="ds-2")
    ok_line, failed_line = [r for r in caplog.records if r.name == run_log._lines.name]
    assert ok_line.levelno == logging.INFO and failed_line.levelno == logging.INFO
    assert "ds-1" in ok_line.getMessage() and "0.000352" in ok_line.getMessage()
    assert (
        "ds-2" in failed_line.getMessage()
        and "outcome=error" in failed_line.getMessage()
    )
    # The long blocks go to the file only, never to the console.
    assert not [r for r in caplog.records if r.name == run_log._blocks.name]


def test_the_run_total_is_the_sum_of_the_call_costs(make_client):
    client, _, run_log = make_client([ok_response(), ok_response(output_tokens=1500)])
    first = client.call(themes_prompt(), resource="a")
    second = client.call(themes_prompt(), resource="b")
    text = log_text(run_log)
    total = first.cost_usd + second.cost_usd
    assert run_log.total_cost_usd == pytest.approx(total)
    assert f"estimated cost USD {total:.6f}" in text and "0 unpriced call(s)" in text


def test_a_run_with_no_calls_logs_a_zero_total(new_run_log):
    run_log = new_run_log()
    assert "0 call(s), estimated cost USD 0.000000" in log_text(run_log)


def test_two_runs_create_two_files_even_in_the_same_second(new_run_log):
    moment = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)
    first = new_run_log(now=lambda: moment)
    second = new_run_log(now=lambda: moment)
    later = new_run_log(now=lambda: moment.replace(second=5))
    names = {first.path.name, second.path.name, later.path.name}
    assert len(names) == 3
    assert first.path.name == "cat-20261116T120000Z.log"
    assert second.path.name == "cat-20261116T120000Z-2.log"


def test_the_file_name_uses_utc_whatever_the_time_zone_of_now(new_run_log):
    moment = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert new_run_log(now=lambda: moment).path.name == "cat-20261116T100000Z.log"


def test_the_default_clock_gives_a_utc_timestamp_in_the_name(new_run_log):
    assert re.fullmatch(r"cat-\d{8}T\d{6}Z\.log", new_run_log().path.name)


def test_lines_in_the_file_are_stamped_in_utc(make_client):
    client, _, run_log = make_client([ok_response()])
    client.call(themes_prompt(), resource="ds-1")
    text = log_text(run_log)
    assert re.search(
        r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ INFO call 1 ", text, re.MULTILINE
    )


def test_a_dry_run_creates_no_file_or_folder(tmp_path):
    folder = tmp_path / "logs" / "propose"
    run_log = RunLog("cat", enabled=False, log_dir=folder)
    run_log.close()
    assert (
        run_log.path is None
        and not folder.exists()
        and not (tmp_path / "logs").exists()
    )


@pytest.mark.parametrize("name", ["a-b_c.d", "dcat-ap-de", "A1", "data-gov-lt"])
def test_plain_catalogue_names_are_accepted(new_run_log, name):
    assert new_run_log(name).path.name.startswith(name + "-")


@pytest.mark.parametrize(
    "name", ["../x", "a/b", "", ".hidden", "a b", "..", "-x", "a\\b", "a\n"]
)
@pytest.mark.parametrize("enabled", [True, False])
def test_catalogue_name_cannot_leave_the_log_folder(tmp_path, name, enabled):
    with pytest.raises(LLMInputError, match="catalogue"):
        RunLog(name, enabled=enabled, log_dir=tmp_path)


def test_closing_twice_logs_the_total_once_and_releases_the_file(new_run_log):
    run_log = new_run_log()
    run_log.close()
    run_log.close()
    assert run_log.path.read_text(encoding="utf-8").count(" total: ") == 1
    run_log.path.rename(
        run_log.path.with_suffix(".old")
    )  # impossible on Windows if the handle were open


def test_recording_after_close_raises(make_client):
    client, _, run_log = make_client([ok_response()])
    run_log.close()
    with pytest.raises(LLMError, match="closed"):
        client.call(themes_prompt(), resource="ds-1")


def test_the_context_manager_closes_the_log_even_on_an_exception(tmp_path):
    with pytest.raises(ZeroDivisionError):
        with RunLog("cat", log_dir=tmp_path) as run_log:
            raise ZeroDivisionError
    assert " total: " in run_log.path.read_text(encoding="utf-8")


def test_the_run_log_is_safe_for_worker_threads(new_run_log):
    run_log = new_run_log()
    prompt = themes_prompt()
    usage = Usage(1, 0, 1)

    def record(index):
        run_log.record(
            CallRecord(
                resource=f"r{index}",
                prompt_name=prompt.name,
                prompt_version=prompt.version,
                prompt_sha256=prompt.sha256,
                model=prompt.model,
                system=prompt.system,
                user=prompt.user,
                raw_response="",
                outcome="ok",
                usage=usage,
                cost_usd=0.25,
                latency_s=0.0,
                attempt=1,
            )
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(record, range(200)))
    text = log_text(run_log)
    numbers = re.findall(r"INFO ===== call (\d+) =====", text)
    assert sorted(map(int, numbers)) == list(range(1, 201))
    assert run_log.calls == 200 and run_log.total_cost_usd == pytest.approx(50.0)


class _FailingStream:
    def write(self, data):
        raise OSError("disk full")

    def flush(self):
        pass


def test_a_log_write_failure_is_logged_at_once_and_raised_on_close(
    make_client, caplog, capsys
):
    client, _, run_log = make_client([ok_response()])
    run_log._handler.stream = _FailingStream()
    with caplog.at_level(logging.ERROR, logger="mqa_advisor.llm"):
        client.call(themes_prompt(), resource="ds-1")  # the call itself still succeeds
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "disk full" in errors[0].getMessage()
    with pytest.raises(LLMError, match="incomplete") as error:
        run_log.close()
    assert isinstance(error.value.__cause__, OSError)
    capsys.readouterr()  # logging also prints the failure to stderr


def test_the_log_folder_is_git_ignored():
    ignore = (Path(__file__).parent.parent / ".gitignore").read_text(encoding="utf-8")
    assert "logs/" in ignore.splitlines()
