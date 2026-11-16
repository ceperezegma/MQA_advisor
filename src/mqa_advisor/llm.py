"""One model call from a loaded prompt: structured output, usage, estimated cost and the run's call log.

``LLMClient`` takes a ``LoadedPrompt`` (see ``prompts``), asks a ``Backend`` for the answer as a forced tool call
whose input schema is the prompt version's output schema, validates the answer against that schema and returns
it with its token counts, estimated cost, latency and attempt number. Every call also writes one block to the
run's log file (``RunLog``, ADR-0019).

Two backends implement the same interface: ``AnthropicBackend`` (the real SDK) and ``FakeBackend`` (scripted,
for tests, so no unit test touches the network).

No sampling parameter (``temperature``, ``top_p``, ``top_k``) is ever sent: current models reject them, so a
prompt cannot set one (see ``prompts``).
"""

import json
import logging
import os
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, Self

import anthropic
from anthropic import DefaultHttpxClient
from dotenv import load_dotenv
from jsonschema import Draft202012Validator

from mqa_advisor.prompts import LoadedPrompt
from mqa_advisor.proposal_schemas import output_schema

logger = logging.getLogger(__name__)

API_KEY_VARIABLE = "ANTHROPIC_API_KEY"
MAX_RETRIES = (
    3  # the SDK retries 408/409/429/5xx and connection errors with exponential backoff
)
TIMEOUT_SECONDS = 60.0
DEFAULT_LOG_DIR = Path("logs") / "propose"


class LLMError(Exception):
    """Base class of the errors of this module."""


class LLMInputError(LLMError):
    """The call cannot be made as asked: a missing key, an unknown model, a prompt the price table can't cost."""


class LLMCallError(LLMError):
    """The API call failed after the SDK's retries."""

    def __init__(self, message: str, attempt: int | None = None):
        super().__init__(message)
        self.attempt = attempt  # how many requests were made, when known


class LLMResponseError(LLMError):
    """The answer is unusable (no tool call, or it does not match the schema); carries the raw response."""

    def __init__(self, message: str, raw_response: str):
        super().__init__(message)
        self.raw_response = raw_response


# --- price table and cost ----------------------------------------------------------------------


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens."""

    input: float
    output: float
    cached_input: float  # tokens read from the prompt cache


# Anthropic's published prices on PRICES_AS_OF, for prompts of up to MAX_PRICED_PROMPT_TOKENS tokens (Haiku 5.5
# charges more above that). The cost shown is an estimate, not an invoice; update this table by hand.
PRICES_AS_OF = "2026-11-16"
MAX_PRICED_PROMPT_TOKENS = 100_000
PRICES: dict[str, ModelPrice] = {
    "claude-haiku-5-5": ModelPrice(input=0.10, output=0.50, cached_input=0.01),
    "claude-sonnet-5-5": ModelPrice(input=2.00, output=10.00, cached_input=0.10),
}


def estimate_cost(
    model: str,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    prices: Mapping[str, ModelPrice] = PRICES,
) -> float:
    """Estimated cost in USD. ``input_tokens`` are the uncached ones, ``cached_tokens`` those read from the cache."""
    price = _price_of(model, prices)
    if input_tokens + cached_tokens > MAX_PRICED_PROMPT_TOKENS:
        raise LLMInputError(
            f"the prompt has {input_tokens + cached_tokens} tokens; the price table only covers prompts of up to "
            f"{MAX_PRICED_PROMPT_TOKENS} tokens, so the cost would be wrong"
        )
    return (
        input_tokens * price.input
        + cached_tokens * price.cached_input
        + output_tokens * price.output
    ) / 1_000_000


def _price_of(model: str, prices: Mapping[str, ModelPrice]) -> ModelPrice:
    try:
        return prices[model]
    except KeyError:
        raise LLMInputError(
            f"model {model!r} is not in the price table; known: {sorted(prices)}"
        ) from None


# --- backends ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class BackendRequest:
    model: str
    max_tokens: int
    system: str
    user: str
    tool_name: str
    tool_schema: dict[str, Any]


@dataclass(frozen=True)
class BackendResponse:
    output: dict[str, Any]  # the input of the forced tool call
    raw_response: str  # the answer as text, for the log
    input_tokens: int  # uncached input (cache-creation tokens included: they are billed at the input rate)
    cached_tokens: int  # input read from the prompt cache
    output_tokens: int
    attempt: int = 1  # requests made, the successful one included


class Backend(Protocol):
    def create(self, request: BackendRequest) -> BackendResponse:
        """Make the call. Raises ``LLMCallError`` when the API fails after retries, ``LLMResponseError`` on no tool call."""
        ...


class FakeBackend:
    """A scripted backend for tests: replays ``script`` in order and records every request."""

    def __init__(self, script: list[BackendResponse | Exception]):
        self._script = deque(script)
        self.requests: list[BackendRequest] = []

    def create(self, request: BackendRequest) -> BackendResponse:
        self.requests.append(request)
        item = self._script.popleft()
        if isinstance(item, Exception):
            raise item
        return item


def load_api_key() -> str:
    """The Anthropic key from the environment (``.env`` is loaded first). Never log the returned value."""
    load_dotenv()
    key = os.environ.get(API_KEY_VARIABLE, "").strip()
    if not key:
        raise LLMInputError(
            f"{API_KEY_VARIABLE} is not set: add it to the .env file or the environment"
        )
    return key


_RETRYABLE_STATUS = {408, 409, 429}


class AnthropicBackend:
    """The real backend, on the ``anthropic`` SDK (forced tool call, ``MAX_RETRIES`` retries, 60 s timeout)."""

    def __init__(self, api_key: str, *, transport: Any = None):
        # The SDK does not say how many requests it made, so count them with HTTP event hooks. The counter is
        # thread-local: the propose step shares one backend between worker threads, and a sync client runs
        # its hooks in the calling thread.
        self._local = threading.local()
        http_client = DefaultHttpxClient(
            event_hooks={
                "request": [self._on_request],
                "response": [self._on_response],
            },
            transport=transport,
        )
        self._client = anthropic.Anthropic(
            api_key=api_key,
            max_retries=MAX_RETRIES,
            timeout=TIMEOUT_SECONDS,
            http_client=http_client,
        )

    def _attempts(self) -> int:
        return getattr(self._local, "attempts", 0)

    def _on_request(self, request: Any) -> None:
        self._local.attempts = self._attempts() + 1

    def _on_response(self, response: Any) -> None:
        status = response.status_code
        retried = status in _RETRYABLE_STATUS or status >= 500
        if (
            retried and self._attempts() <= MAX_RETRIES
        ):  # the SDK will send another request
            logger.warning(
                "transient API error (HTTP %d) on attempt %d; the SDK retries",
                status,
                self._attempts(),
            )

    def create(self, request: BackendRequest) -> BackendResponse:
        self._local.attempts = 0
        tool = {
            "name": request.tool_name,
            "description": "Return the answer in exactly this structure.",
            "input_schema": request.tool_schema,
        }
        try:
            message = self._client.messages.create(
                model=request.model,
                max_tokens=request.max_tokens,
                system=request.system,
                messages=[{"role": "user", "content": request.user}],
                tools=[tool],
                tool_choice={"type": "tool", "name": request.tool_name},
            )
        except anthropic.APIError as error:
            raise LLMCallError(
                f"the API call failed after {self._attempts()} attempt(s): {type(error).__name__}: {error}",
                attempt=self._attempts(),
            ) from error

        raw_response = json.dumps(
            [block.model_dump() for block in message.content], ensure_ascii=False
        )
        for block in message.content:
            if block.type == "tool_use" and block.name == request.tool_name:
                usage = message.usage
                return BackendResponse(
                    output=block.input,
                    raw_response=raw_response,
                    input_tokens=usage.input_tokens
                    + (usage.cache_creation_input_tokens or 0),
                    cached_tokens=usage.cache_read_input_tokens or 0,
                    output_tokens=usage.output_tokens,
                    attempt=self._attempts(),
                )
        raise LLMResponseError(
            f"the response has no {request.tool_name!r} tool call (stop reason {message.stop_reason!r})",
            raw_response,
        )


# --- the run log -------------------------------------------------------------------------------

_CATALOGUE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*"
)  # a plain name: cannot leave the log folder


@dataclass(frozen=True)
class CallRecord:
    """Everything the log block of one call shows."""

    resource: str
    prompt_name: str
    prompt_version: int
    prompt_sha256: str
    model: str
    system: str
    user: str
    raw_response: str
    outcome: str  # "ok" or why the call failed
    input_tokens: int | None
    cached_tokens: int | None
    output_tokens: int | None
    cost_usd: float
    latency_s: float
    attempt: int | None


class RunLog:
    """The interaction log of one ``propose`` run: ``logs/propose/{catalogue}-{UTC timestamp}.log`` (ADR-0019).

    One delimited block per call, an INFO line with the cost, and the run total on ``close``. With
    ``enabled=False`` (a dry run) no file or folder is created. The file contains prompts and responses, which
    are public catalogue metadata; the API key never reaches this class.
    """

    def __init__(
        self,
        catalogue: str,
        *,
        enabled: bool = True,
        log_dir: Path = DEFAULT_LOG_DIR,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        if not _CATALOGUE.fullmatch(catalogue):
            raise LLMInputError(f"invalid catalogue name {catalogue!r} for a log file")
        self.run_id = secrets.token_hex(4)
        self.path: Path | None = None
        self.calls = 0
        self.total_cost_usd = 0.0
        # Two loggers share one file handler: blocks go to the file only (they are long), the INFO cost lines
        # also propagate to the console configured by the pipeline.
        self._lines = logging.getLogger(f"mqa_advisor.propose.run.{self.run_id}")
        self._blocks = logging.getLogger(f"{self._lines.name}.blocks")
        self._blocks.propagate = False
        self._handler: logging.FileHandler | None = None
        if enabled:
            log_dir.mkdir(parents=True, exist_ok=True)
            self.path = self._free_path(log_dir, catalogue, now())
            self._handler = logging.FileHandler(self.path, encoding="utf-8")
            self._handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            )
            for log in (self._lines, self._blocks):
                log.setLevel(logging.INFO)
                log.addHandler(self._handler)

    @staticmethod
    def _free_path(log_dir: Path, catalogue: str, moment: datetime) -> Path:
        """``{catalogue}-{timestamp}.log``, with a number added if two runs start in the same second."""
        stem = f"{catalogue}-{moment.astimezone(UTC):%Y%m%dT%H%M%SZ}"
        path = log_dir / f"{stem}.log"
        number = 1
        while path.exists():
            number += 1
            path = log_dir / f"{stem}-{number}.log"
        return path

    def record(self, call: CallRecord) -> None:
        """Write the block and the INFO line of one call."""
        self.calls += 1
        self.total_cost_usd += call.cost_usd
        number = self.calls
        self._blocks.info(
            "\n".join(
                [
                    f"===== call {number} =====",
                    f"run id: {self.run_id}",
                    f"resource: {call.resource}",
                    f"prompt: {call.prompt_name} v{call.prompt_version} sha256={call.prompt_sha256}",
                    f"model: {call.model}",
                    f"outcome: {call.outcome}",
                    f"tokens: input={call.input_tokens} cached={call.cached_tokens} output={call.output_tokens}",
                    f"estimated cost USD: {call.cost_usd:.6f}",
                    f"latency s: {call.latency_s:.2f}",
                    f"attempt: {call.attempt}",
                    "----- system -----",
                    call.system,
                    "----- user -----",
                    call.user,
                    "----- response -----",
                    call.raw_response,
                    f"===== end call {number} =====",
                ]
            )
        )
        self._lines.info(
            "call %d resource=%s prompt=%s v%d outcome=%s estimated cost USD %.6f",
            number,
            call.resource,
            call.prompt_name,
            call.prompt_version,
            call.outcome,
            call.cost_usd,
        )

    def close(self) -> None:
        """Log the run total and release the file."""
        self._lines.info(
            "run %s total: %d call(s), estimated cost USD %.6f (prices as of %s)",
            self.run_id,
            self.calls,
            self.total_cost_usd,
            PRICES_AS_OF,
        )
        if self._handler is not None:
            for log in (self._lines, self._blocks):
                log.removeHandler(self._handler)
            self._handler.close()
            self._handler = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# --- the client --------------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMResult:
    output: dict[str, Any]  # validated against the prompt version's schema
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    cost_usd: float  # an estimate
    latency_s: float
    attempt: int  # 1 when the first request succeeded


class LLMClient:
    def __init__(
        self,
        backend: Backend,
        run_log: RunLog,
        prices: Mapping[str, ModelPrice] = PRICES,
    ):
        self._backend = backend
        self._run_log = run_log
        self._prices = prices

    def call(self, prompt: LoadedPrompt, *, resource: str) -> LLMResult:
        """Make the call for ``resource`` and return the validated answer; every outcome is logged."""
        _price_of(prompt.model, self._prices)  # an unknown model fails before any call
        schema = output_schema(
            prompt.name, prompt.version
        )  # the schema of THIS version, not the active one
        request = BackendRequest(
            model=prompt.model,
            max_tokens=prompt.max_tokens,
            system=prompt.system,
            user=prompt.user,
            tool_name=prompt.name,
            tool_schema=schema,
        )

        started = time.monotonic()
        try:
            response = self._backend.create(request)
        except LLMCallError as error:
            latency_s = time.monotonic() - started
            self._log(
                prompt,
                resource,
                f"error: {error}",
                "",
                None,
                0.0,
                latency_s,
                error.attempt,
            )
            raise
        except LLMResponseError as error:
            latency_s = time.monotonic() - started
            self._log(
                prompt,
                resource,
                f"error: {error}",
                error.raw_response,
                None,
                0.0,
                latency_s,
                None,
            )
            raise
        latency_s = time.monotonic() - started

        def log(outcome: str, cost: float) -> None:
            self._log(
                prompt,
                resource,
                outcome,
                response.raw_response,
                response,
                cost,
                latency_s,
                response.attempt,
            )

        try:
            cost = estimate_cost(
                prompt.model,
                response.input_tokens,
                response.cached_tokens,
                response.output_tokens,
                self._prices,
            )
        except LLMInputError as error:
            log(f"error: {error}", 0.0)
            raise
        if problem := _first_schema_problem(schema, response.output):
            message = f"the response does not match the schema of {prompt.name} v{prompt.version}: {problem}"
            log(f"error: {message}", cost)
            raise LLMResponseError(message, response.raw_response)

        log("ok", cost)
        return LLMResult(
            output=response.output,
            input_tokens=response.input_tokens,
            cached_tokens=response.cached_tokens,
            output_tokens=response.output_tokens,
            cost_usd=cost,
            latency_s=latency_s,
            attempt=response.attempt,
        )

    def _log(
        self,
        prompt: LoadedPrompt,
        resource: str,
        outcome: str,
        raw_response: str,
        response: BackendResponse | None,
        cost: float,
        latency_s: float,
        attempt: int | None,
    ) -> None:
        self._run_log.record(
            CallRecord(
                resource=resource,
                prompt_name=prompt.name,
                prompt_version=prompt.version,
                prompt_sha256=prompt.sha256,
                model=prompt.model,
                system=prompt.system,
                user=prompt.user,
                raw_response=raw_response,
                outcome=outcome,
                input_tokens=response.input_tokens if response else None,
                cached_tokens=response.cached_tokens if response else None,
                output_tokens=response.output_tokens if response else None,
                cost_usd=cost,
                latency_s=latency_s,
                attempt=attempt,
            )
        )


def _first_schema_problem(schema: dict[str, Any], output: Any) -> str | None:
    """The first schema violation of ``output`` (in a stable order), or None when it is valid."""
    errors = sorted(
        Draft202012Validator(schema).iter_errors(output), key=lambda e: e.json_path
    )
    if not errors:
        return None
    return f"{errors[0].json_path}: {errors[0].message}"
