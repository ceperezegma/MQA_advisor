"""Tests for the LLM client: cost, schema validation, the backends and the run log. No test uses the network."""

import json
import logging
from datetime import UTC, datetime

import httpx2
import pytest
from jsonschema import Draft202012Validator

from mqa_advisor import llm
from mqa_advisor.llm import (
    AnthropicBackend,
    BackendResponse,
    FakeBackend,
    LLMCallError,
    LLMClient,
    LLMInputError,
    LLMResponseError,
    ModelPrice,
    RunLog,
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


def make_client(tmp_path, script, *, prices=None):
    backend = FakeBackend(script)
    run_log = RunLog("cat", log_dir=tmp_path / "logs")
    kwargs = {} if prices is None else {"prices": prices}
    return LLMClient(backend, run_log, **kwargs), backend, run_log


# --- price table and cost ----------------------------------------------------------------------


def test_cost_is_tokens_times_price_per_million_including_cached_tokens():
    prices = {"m": ModelPrice(input=2.0, output=10.0, cached_input=0.5)}
    cost = estimate_cost("m", 1000, 200, 500, prices)
    assert cost == pytest.approx((1000 * 2.0 + 200 * 0.5 + 500 * 10.0) / 1_000_000)


def test_unknown_model_raises_naming_it():
    with pytest.raises(LLMInputError, match="no-such-model"):
        estimate_cost("no-such-model", 1, 0, 1)


def test_price_table_has_an_iso_date_and_the_two_models_of_the_design():
    assert datetime.strptime(llm.PRICES_AS_OF, "%Y-%m-%d")
    assert {"claude-haiku-5-5", "claude-sonnet-5-5"} <= set(llm.PRICES)


def test_a_prompt_longer_than_the_price_table_covers_raises():
    limit = llm.MAX_PRICED_PROMPT_TOKENS
    estimate_cost("claude-haiku-5-5", limit, 0, 1)  # exactly at the limit is priced
    with pytest.raises(LLMInputError, match="price table"):
        estimate_cost("claude-haiku-5-5", limit - 5, 6, 1)  # cached tokens count too


# --- the client with the fake backend ----------------------------------------------------------


def test_successful_call_returns_output_usage_cost_latency_and_attempt(tmp_path):
    client, _, _ = make_client(tmp_path, [ok_response(attempt=2)])
    result = client.call(themes_prompt(), resource="ds-1")
    assert result.output == {"themes": ["ENVI"]}
    assert (result.input_tokens, result.cached_tokens, result.output_tokens) == (
        1000,
        200,
        500,
    )
    assert result.cost_usd == pytest.approx(
        estimate_cost("claude-haiku-5-5", 1000, 200, 500)
    )
    assert result.latency_s >= 0
    assert result.attempt == 2


def test_request_carries_system_and_user_separately_and_the_versions_schema(tmp_path):
    client, backend, _ = make_client(tmp_path, [ok_response(), ok_response()])
    prompt = themes_prompt()
    client.call(prompt, resource="a")
    client.call(prompt, resource="b")
    first, second = backend.requests
    assert first.system == prompt.system and first.user == prompt.user
    assert "Air quality" in first.user and "Air quality" not in first.system
    assert (first.model, first.max_tokens) == (prompt.model, prompt.max_tokens)
    assert first.tool_name == "propose_themes"
    assert first.tool_schema == output_schema("propose_themes", 1)
    assert second.user == first.user  # both requests recorded, in order


def test_the_request_has_no_sampling_parameters():
    fields = set(llm.BackendRequest.__dataclass_fields__)
    assert not fields & {"temperature", "top_p", "top_k"}


@pytest.mark.parametrize(
    "output",
    [
        {"themes": []},  # minItems
        {},  # missing required key
        {"themes": "ENVI"},  # wrong type
        {"themes": ["ENVI"], "extra": 1},  # additionalProperties
    ],
)
def test_response_not_matching_the_schema_raises_with_the_raw_response(
    tmp_path, output
):
    raw = json.dumps(output)
    client, _, run_log = make_client(
        tmp_path, [ok_response(output=output, raw_response=raw)]
    )
    with pytest.raises(LLMResponseError, match="propose_themes v1") as error:
        client.call(themes_prompt(), resource="ds-1")
    assert error.value.raw_response == raw
    run_log.close()
    text = run_log.path.read_text(encoding="utf-8")
    assert (
        raw in text and "does not match the schema" in text
    )  # the raw response is in the log too


def test_keywords_language_pattern_is_validated(tmp_path):
    values = {
        "title": "t",
        "description": "d",
        "language": "en",
        "themes": "(none)",
        "existing_keywords": "(none)",
        "remaining": "3",
    }
    prompt = load_prompt("propose_keywords", 1, values)
    bad = {"keywords": ["river"], "language": "English"}
    client, _, _ = make_client(tmp_path, [ok_response(output=bad)])
    with pytest.raises(LLMResponseError, match="language"):
        client.call(prompt, resource="ds-1")


@pytest.mark.parametrize(("name", "version"), sorted(schema_keys()))
def test_the_committed_schemas_are_valid_json_schemas(name, version):
    Draft202012Validator.check_schema(output_schema(name, version))


def test_unknown_model_makes_no_call(tmp_path):
    client, backend, _ = make_client(tmp_path, [ok_response()], prices={})
    with pytest.raises(LLMInputError, match="claude-haiku-5-5"):
        client.call(themes_prompt(), resource="ds-1")
    assert backend.requests == []


def test_api_failure_is_raised_and_logged(tmp_path):
    failure = LLMCallError("the API call failed after 4 attempt(s)", attempt=4)
    client, _, run_log = make_client(tmp_path, [failure])
    with pytest.raises(LLMCallError):
        client.call(themes_prompt(), resource="ds-1")
    run_log.close()
    text = run_log.path.read_text(encoding="utf-8")
    assert "outcome: error: the API call failed" in text and "attempt: 4" in text


def test_a_call_over_the_price_table_range_raises_and_is_logged(tmp_path):
    big = ok_response(input_tokens=llm.MAX_PRICED_PROMPT_TOKENS + 1)
    client, _, run_log = make_client(tmp_path, [big])
    with pytest.raises(LLMInputError, match="price table"):
        client.call(themes_prompt(), resource="ds-1")
    run_log.close()
    assert "price table" in run_log.path.read_text(encoding="utf-8")


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


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    # The SDK waits between retries; tests must not.
    monkeypatch.setattr("anthropic._base_client.time.sleep", lambda seconds: None)


def test_real_backend_is_configured_with_three_retries_and_a_60_second_timeout():
    backend = backend_with(lambda request: httpx2.Response(200, json=message_json()))
    assert backend._client.max_retries == 3
    assert backend._client.timeout == 60.0


def test_real_backend_sends_a_forced_tool_call_without_sampling_parameters(tmp_path):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json=message_json(
                cache_read_input_tokens=30, cache_creation_input_tokens=5
            ),
        )

    client = LLMClient(backend_with(handler), RunLog("cat", log_dir=tmp_path))
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
    # Cache-creation tokens are billed at the input rate, so they are counted as input.
    assert (result.input_tokens, result.cached_tokens, result.output_tokens) == (
        105,
        30,
        20,
    )
    assert result.output == {"themes": ["ENVI"]} and result.attempt == 1


def test_a_retried_error_is_logged_as_a_warning_and_counted_as_an_attempt(
    tmp_path, caplog
):
    answers = iter(
        [
            httpx2.Response(500, json={"error": "boom"}),
            httpx2.Response(200, json=message_json()),
        ]
    )
    client = LLMClient(
        backend_with(lambda request: next(answers)), RunLog("cat", log_dir=tmp_path)
    )
    with caplog.at_level(logging.WARNING, logger="mqa_advisor.llm"):
        result = client.call(themes_prompt(), resource="ds-1")
    assert result.attempt == 2
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "HTTP 500" in warning.getMessage() and "attempt 1" in warning.getMessage()


def test_retries_exhausted_raises_call_error_after_four_requests(tmp_path, caplog):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx2.Response(500, json={"error": "boom"})

    client = LLMClient(backend_with(handler), RunLog("cat", log_dir=tmp_path))
    with caplog.at_level(logging.WARNING, logger="mqa_advisor.llm"):
        with pytest.raises(LLMCallError, match="4 attempt") as error:
            client.call(themes_prompt(), resource="ds-1")
    assert len(requests) == 4 and error.value.attempt == 4  # first request + 3 retries
    assert (
        len([r for r in caplog.records if r.levelno == logging.WARNING]) == 3
    )  # the last one is not retried


def test_a_response_without_the_tool_call_raises_with_the_raw_response(tmp_path):
    body = message_json()
    body["content"] = [{"type": "text", "text": "I refuse"}]
    body["stop_reason"] = "end_turn"
    client = LLMClient(
        backend_with(lambda request: httpx2.Response(200, json=body)),
        RunLog("cat", log_dir=tmp_path),
    )
    with pytest.raises(
        LLMResponseError, match="no 'propose_themes' tool call"
    ) as error:
        client.call(themes_prompt(), resource="ds-1")
    assert "I refuse" in error.value.raw_response


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


def test_the_key_never_appears_in_the_log_file_or_log_records(tmp_path, caplog):
    answers = iter(
        [
            httpx2.Response(500, json={"error": "boom"}),
            httpx2.Response(200, json=message_json()),
        ]
    )
    run_log = RunLog("cat", log_dir=tmp_path)
    client = LLMClient(backend_with(lambda request: next(answers)), run_log)
    with caplog.at_level(logging.DEBUG):
        client.call(themes_prompt(), resource="ds-1")
        run_log.close()
    assert SECRET not in run_log.path.read_text(encoding="utf-8")
    assert all(SECRET not in record.getMessage() for record in caplog.records)


# --- the run log -------------------------------------------------------------------------------


def test_the_log_block_has_every_field(tmp_path):
    client, _, run_log = make_client(tmp_path, [ok_response(attempt=2)])
    prompt = themes_prompt()
    client.call(prompt, resource="ds-1")
    run_log.close()
    text = run_log.path.read_text(encoding="utf-8")
    for expected in [
        "===== call 1 =====",
        f"run id: {run_log.run_id}",
        "resource: ds-1",
        f"propose_themes v1 sha256={prompt.sha256}",
        "model: claude-haiku-5-5",
        "outcome: ok",
        "tokens: input=1000 cached=200 output=500",
        "estimated cost USD:",
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


def test_an_info_line_per_call_carries_the_resource_and_cost(tmp_path, caplog):
    client, _, run_log = make_client(tmp_path, [ok_response()])
    with caplog.at_level(logging.INFO):
        client.call(themes_prompt(), resource="ds-1")
    [line] = [r for r in caplog.records if r.name == run_log._lines.name]
    cost = estimate_cost("claude-haiku-5-5", 1000, 200, 500)
    assert "ds-1" in line.getMessage() and f"{cost:.6f}" in line.getMessage()
    # The long block goes to the file only, never to the console.
    assert not [r for r in caplog.records if r.name == run_log._blocks.name]


def test_the_run_total_is_the_sum_of_the_call_costs(tmp_path):
    client, _, run_log = make_client(
        tmp_path, [ok_response(), ok_response(output_tokens=1500)]
    )
    first = client.call(themes_prompt(), resource="a")
    second = client.call(themes_prompt(), resource="b")
    run_log.close()
    total = first.cost_usd + second.cost_usd
    assert run_log.total_cost_usd == pytest.approx(total)
    assert f"estimated cost USD {total:.6f}" in run_log.path.read_text(encoding="utf-8")


def test_two_runs_create_two_files_even_in_the_same_second(tmp_path):
    moment = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)
    first = RunLog("cat", log_dir=tmp_path, now=lambda: moment)
    second = RunLog("cat", log_dir=tmp_path, now=lambda: moment)
    later = RunLog("cat", log_dir=tmp_path, now=lambda: moment.replace(second=5))
    for run_log in (first, second, later):
        run_log.close()
    names = {first.path.name, second.path.name, later.path.name}
    assert len(names) == 3
    assert first.path.name == "cat-20261116T120000Z.log"


def test_a_dry_run_creates_no_file_or_folder(tmp_path):
    folder = tmp_path / "logs" / "propose"
    run_log = RunLog("cat", enabled=False, log_dir=folder)
    run_log.close()
    assert (
        run_log.path is None
        and not folder.exists()
        and not (tmp_path / "logs").exists()
    )


@pytest.mark.parametrize("name", ["../x", "a/b", "", ".hidden", "a b"])
def test_catalogue_name_cannot_leave_the_log_folder(tmp_path, name):
    with pytest.raises(LLMInputError, match="catalogue"):
        RunLog(name, log_dir=tmp_path)


def test_the_log_folder_is_git_ignored():
    from pathlib import Path

    ignore = (Path(__file__).parent.parent / ".gitignore").read_text(encoding="utf-8")
    assert "logs/" in ignore.split()
