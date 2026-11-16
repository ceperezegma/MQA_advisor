"""One model call from a loaded prompt: structured output, usage, estimated cost and the run's call log.

``LLMClient`` takes a ``LoadedPrompt`` (see ``prompts``), asks a ``Backend`` for the answer as a forced tool call
whose input schema is the prompt version's output schema, validates the answer against that schema and returns
it with its token counts, estimated cost, latency and attempt number. Every call that reached the API also
writes one block to the run's log file (``RunLog``, ADR-0019), whether it succeeded or not.

Two backends implement the same interface: ``AnthropicBackend`` (the real SDK) and ``FakeBackend`` (scripted,
for tests, so no unit test touches the network).

No sampling parameter (``temperature``, ``top_p``, ``top_k``) is ever sent: current models reject them, so a
prompt cannot set one (see ``prompts``).
"""

import functools
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, Self

import anthropic
from anthropic import DefaultHttpxClient
from dotenv import load_dotenv
from jsonschema import Draft202012Validator

from mqa_advisor.prompts import LoadedPrompt
from mqa_advisor.proposal_schemas import output_schema

if TYPE_CHECKING:
    import httpx2

logger = logging.getLogger(__name__)

API_KEY_VARIABLE = "ANTHROPIC_API_KEY"
MAX_RETRIES = (
    3  # the SDK retries 408/409/429/5xx and connection errors with exponential backoff
)
# Per phase (connect, read, write), not per call: a call with retries can take several times this.
TIMEOUT_SECONDS = 60.0
DEFAULT_LOG_DIR = Path("logs") / "propose"


@dataclass(frozen=True)
class Usage:
    """Token counts of one call."""

    input_tokens: (
        int  # uncached input (cache-creation tokens included, see ``AnthropicBackend``)
    )
    cached_tokens: int  # input read from the prompt cache
    output_tokens: int


class LLMError(Exception):
    """Base class of the errors of this module.

    The optional attributes carry what is known about the call that failed, so the run log can show it.
    """

    def __init__(
        self,
        message: str,
        *,
        attempt: int | None = None,
        raw_response: str = "",
        usage: Usage | None = None,
    ):
        super().__init__(message)
        self.attempt = attempt  # requests made, when known
        self.raw_response = raw_response
        self.usage = usage  # tokens reported by the API, when the call was billed


class LLMInputError(LLMError):
    """The call cannot be made as asked: a missing key, an unknown model, a prompt the price table can't cost."""


class LLMCallError(LLMError):
    """The API call failed after the SDK's retries."""


class LLMResponseError(LLMError):
    """The answer is unusable (no tool call, or it does not match the schema); carries the raw response."""

    def __init__(
        self,
        message: str,
        raw_response: str,
        *,
        attempt: int | None = None,
        usage: Usage | None = None,
    ):
        super().__init__(
            message, attempt=attempt, raw_response=raw_response, usage=usage
        )


# --- price table and cost ----------------------------------------------------------------------


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens."""

    input: float
    output: float
    cached_input: float  # tokens read from the prompt cache


# Anthropic's published prices on PRICES_AS_OF, for prompts of up to MAX_PRICED_PROMPT_TOKENS tokens (Haiku 5.5
# charges more above that). The cost shown is an estimate, not an invoice; update this table by hand. Add a
# model only together with a prompt version that uses it: Sonnet 5.5, for one, rejects a forced tool call.
PRICES_AS_OF = "2026-11-16"
MAX_PRICED_PROMPT_TOKENS = 100_000
PRICES: Mapping[str, ModelPrice] = MappingProxyType(
    {"claude-haiku-5-5": ModelPrice(input=0.10, output=0.50, cached_input=0.01)}
)


def estimate_cost(
    model: str,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    prices: Mapping[str, ModelPrice] = PRICES,
) -> float:
    """Estimated cost in USD. ``input_tokens`` are the uncached ones, ``cached_tokens`` those read from the cache.

    Raises ``LLMInputError`` for an unknown model, or for a prompt longer than the table's prices cover.
    """
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
    input_tokens: (
        int  # uncached input (cache-creation tokens included, see ``AnthropicBackend``)
    )
    cached_tokens: int  # input read from the prompt cache
    output_tokens: int
    attempt: int = 1  # requests made, the successful one included
    stop_reason: str | None = None  # "max_tokens" means the answer was cut off

    @property
    def usage(self) -> Usage:
        return Usage(self.input_tokens, self.cached_tokens, self.output_tokens)


class Backend(Protocol):
    def create(self, request: BackendRequest) -> BackendResponse:
        """Make the call. Raises ``LLMCallError`` when the API fails after retries, ``LLMResponseError`` on no tool call."""
        ...

    def close(self) -> None:
        """Release the connections."""
        ...


class FakeBackend:
    """A scripted backend for tests: replays ``script`` in order and records every request."""

    def __init__(self, script: list[BackendResponse | Exception]):
        self._script = deque(script)
        self.requests: list[BackendRequest] = []

    def create(self, request: BackendRequest) -> BackendResponse:
        self.requests.append(request)
        if not self._script:
            raise AssertionError("FakeBackend: more calls than scripted answers")
        item = self._script.popleft()
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        pass


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
    """The real backend, on the ``anthropic`` SDK (forced tool call, ``MAX_RETRIES`` retries, 60 s timeout).

    One instance can be shared by worker threads. Use it as a context manager (or call ``close``) to release the
    connection pool.
    """

    def __init__(self, api_key: str, *, transport: httpx2.BaseTransport | None = None):
        # The SDK does not say how many requests it made, so count them with HTTP event hooks. The counter is
        # thread-local: a sync client runs its hooks in the calling thread, and worker threads share this object.
        self._api_key = api_key  # only to scrub it from error text, never logged
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

    def _on_request(self, request: httpx2.Request) -> None:
        self._local.attempts = self._attempts() + 1

    def _on_response(self, response: httpx2.Response) -> None:
        status = response.status_code
        transient = status in _RETRYABLE_STATUS or status >= 500
        # The SDK usually retries these (it also honours an ``x-should-retry`` header), so this is only a hint;
        # the attempt number of the final result is what counts.
        if transient and self._attempts() <= MAX_RETRIES:
            logger.warning(
                "transient API error (HTTP %d) on attempt %d",
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
            # An API error body could echo the key; the message goes to the run log, so scrub it.
            detail = f"{type(error).__name__}: {error}".replace(self._api_key, "***")
            raise LLMCallError(
                f"the API call failed after {self._attempts()} attempt(s): {detail}",
                attempt=self._attempts(),
            ) from error

        raw_response = json.dumps(
            [block.model_dump() for block in message.content], ensure_ascii=False
        )
        # Cache-creation tokens are counted as plain input. Anthropic bills a cache write above the input rate,
        # so the estimate is a lower bound if prompt caching is ever turned on (it is not used today).
        usage = Usage(
            input_tokens=message.usage.input_tokens
            + (message.usage.cache_creation_input_tokens or 0),
            cached_tokens=message.usage.cache_read_input_tokens or 0,
            output_tokens=message.usage.output_tokens,
        )
        for block in message.content:
            if block.type == "tool_use" and block.name == request.tool_name:
                return BackendResponse(
                    output=block.input,
                    raw_response=raw_response,
                    input_tokens=usage.input_tokens,
                    cached_tokens=usage.cached_tokens,
                    output_tokens=usage.output_tokens,
                    attempt=self._attempts(),
                    stop_reason=message.stop_reason,
                )
        # The call was billed even though the answer is unusable: keep the usage for the log.
        raise LLMResponseError(
            f"the response has no {request.tool_name!r} tool call (stop reason {message.stop_reason!r})",
            raw_response,
            attempt=self._attempts(),
            usage=usage,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


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
    usage: (
        Usage | None
    )  # None when the API reported none (the call failed before an answer)
    cost_usd: (
        float | None
    )  # None: the call was billed but the price table cannot price it
    latency_s: float
    attempt: int | None


class _AuditFileHandler(logging.FileHandler):
    """A log file created exclusively (never reopening a file of another run) that remembers a write error.

    ``logging`` swallows I/O errors (it only prints to stderr), so a full disk would leave a run that spends
    money without its audit log. ``RunLog`` reads ``write_error`` and reports it.
    """

    def __init__(self, path: Path):
        super().__init__(path, mode="x", encoding="utf-8")
        self.write_error: BaseException | None = None
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ"
        )
        formatter.converter = time.gmtime  # UTC, as the file name
        self.setFormatter(formatter)

    def handleError(self, record: logging.LogRecord) -> None:
        if self.write_error is None:
            self.write_error = sys.exc_info()[1]
        super().handleError(record)


class RunLog:
    """The interaction log of one ``propose`` run: ``logs/propose/{catalogue}-{UTC timestamp}.log`` (ADR-0019).

    One delimited block per call, an INFO line with the cost, and the run total on ``close``. With
    ``enabled=False`` (a dry run) no file or folder is created. The file contains prompts and responses, which
    are public catalogue metadata; the API key never reaches this class. Safe to share between worker threads.

    A failure to write the file is logged at ERROR as soon as it is noticed and raised by ``close``.
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
        self.unpriced_calls = 0  # billed calls the price table could not price
        self.total_cost_usd = 0.0  # of the priced calls
        self._lock = threading.Lock()
        self._closed = False
        self._write_error_reported = False
        # Plain Logger objects, not ``logging.getLogger``: that would keep two loggers per run in logging's
        # global registry for the life of the process. The parent makes the cost lines reach the console that
        # the pipeline configures; the blocks are long and go to the file only.
        parent = logging.getLogger("mqa_advisor.propose")
        self._lines = logging.Logger(f"{parent.name}.run.{self.run_id}", logging.INFO)
        self._lines.parent = parent
        self._blocks = logging.Logger(f"{self._lines.name}.blocks", logging.INFO)
        self._blocks.propagate = False
        self._handler: _AuditFileHandler | None = None
        if enabled:
            log_dir.mkdir(parents=True, exist_ok=True)
            self._handler = self._create_handler(log_dir, catalogue, now())
            self.path = Path(self._handler.baseFilename)
            self._lines.addHandler(self._handler)
            self._blocks.addHandler(self._handler)

    @staticmethod
    def _create_handler(
        log_dir: Path, catalogue: str, moment: datetime
    ) -> _AuditFileHandler:
        """Create ``{catalogue}-{timestamp}.log``, numbering it if another run started in the same second.

        The file is created exclusively, so two runs can never share one even if they race.
        """
        stem = f"{catalogue}-{moment.astimezone(UTC):%Y%m%dT%H%M%SZ}"
        number = 1
        while True:
            name = f"{stem}.log" if number == 1 else f"{stem}-{number}.log"
            try:
                return _AuditFileHandler(log_dir / name)
            except FileExistsError:
                number += 1

    def record(self, call: CallRecord) -> None:
        """Write the block and the INFO line of one call."""
        with self._lock:
            if self._closed:
                raise LLMError("the run log is closed")
            self.calls += 1
            number = self.calls
            if call.cost_usd is None:
                self.unpriced_calls += 1
                cost = "unpriced (billed, but beyond the price table)"
            else:
                self.total_cost_usd += call.cost_usd
                cost = f"{call.cost_usd:.6f}"
            usage = call.usage
            self._blocks.info(
                "\n".join(
                    [
                        f"===== call {number} =====",
                        f"run id: {self.run_id}",
                        f"resource: {call.resource}",
                        f"prompt: {call.prompt_name} v{call.prompt_version} sha256={call.prompt_sha256}",
                        f"model: {call.model}",
                        f"outcome: {call.outcome}",
                        f"tokens: input={usage.input_tokens if usage else None} "
                        f"cached={usage.cached_tokens if usage else None} "
                        f"output={usage.output_tokens if usage else None}",
                        f"estimated cost USD: {cost}",
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
                "call %d resource=%s prompt=%s v%d outcome=%s estimated cost USD %s",
                number,
                call.resource,
                call.prompt_name,
                call.prompt_version,
                call.outcome,
                cost,
            )
            self._report_write_error()

    def _report_write_error(self) -> None:
        """Log the first failure to write the file at ERROR, once. Called with the lock held."""
        error = self._handler.write_error if self._handler else None
        if error is not None and not self._write_error_reported:
            self._write_error_reported = True
            logger.error("writing the run log %s failed: %r", self.path, error)

    def close(self) -> None:
        """Log the run total and release the file; raises if writing the file failed. Safe to call twice."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._lines.info(
                    "run %s total: %d call(s), estimated cost USD %.6f (prices as of %s), %d unpriced call(s)",
                    self.run_id,
                    self.calls,
                    self.total_cost_usd,
                    PRICES_AS_OF,
                    self.unpriced_calls,
                )
                self._report_write_error()
            finally:
                if self._handler is not None:
                    self._lines.removeHandler(self._handler)
                    self._blocks.removeHandler(self._handler)
                    self._handler.close()
            error = self._handler.write_error if self._handler else None
        if error is not None:
            raise LLMError(
                f"writing the run log {self.path} failed, so it is incomplete: {error!r}"
            ) from error

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
        """Make the call for ``resource`` and return the validated answer.

        Failures before the request (unknown model, prompt too long, invalid schema) raise without a log block:
        no call was made. Every failure after it is logged with what is known about the call, then re-raised.
        """
        _price_of(prompt.model, self._prices)  # an unknown model fails before any call
        # A token is at least one byte, so a prompt of up to 100,000 bytes has at most 100,000 tokens: the most
        # the price table covers. Longer is refused here, before anything is billed.
        size = len(prompt.system.encode()) + len(prompt.user.encode())
        if size > MAX_PRICED_PROMPT_TOKENS:
            raise LLMInputError(
                f"the prompt is {size} bytes; more than {MAX_PRICED_PROMPT_TOKENS} bytes cannot be priced"
            )
        validator = _validator(
            prompt.name, prompt.version
        )  # the schema of THIS version, not the active one
        request = BackendRequest(
            model=prompt.model,
            max_tokens=prompt.max_tokens,
            system=prompt.system,
            user=prompt.user,
            tool_name=prompt.name,
            tool_schema=output_schema(prompt.name, prompt.version),
        )

        started = time.monotonic()
        try:
            response = self._backend.create(request)
        except Exception as error:
            self._log_failure(prompt, resource, time.monotonic() - started, error)
            raise
        latency_s = time.monotonic() - started

        try:
            cost = estimate_cost(
                prompt.model,
                response.input_tokens,
                response.cached_tokens,
                response.output_tokens,
                self._prices,
            )
        except LLMInputError as error:
            # Billed, but the table cannot price it: log it as such and refuse the answer.
            self._log(
                prompt,
                resource,
                f"error: billed but not priced: {error}",
                latency_s,
                raw_response=response.raw_response,
                usage=response.usage,
                attempt=response.attempt,
                cost=None,
            )
            raise
        if problem := _first_schema_problem(validator, response.output):
            message = f"the response does not match the schema of {prompt.name} v{prompt.version}: {problem}"
            if response.stop_reason == "max_tokens":
                message += " (the answer was cut off: stop reason max_tokens)"
            self._log(
                prompt,
                resource,
                f"error: {message}",
                latency_s,
                raw_response=response.raw_response,
                usage=response.usage,
                attempt=response.attempt,
                cost=cost,
            )
            raise LLMResponseError(
                message,
                response.raw_response,
                attempt=response.attempt,
                usage=response.usage,
            )

        self._log(
            prompt,
            resource,
            "ok",
            latency_s,
            raw_response=response.raw_response,
            usage=response.usage,
            attempt=response.attempt,
            cost=cost,
        )
        return LLMResult(
            output=response.output,
            input_tokens=response.input_tokens,
            cached_tokens=response.cached_tokens,
            output_tokens=response.output_tokens,
            cost_usd=cost,
            latency_s=latency_s,
            attempt=response.attempt,
        )

    def _log_failure(
        self, prompt: LoadedPrompt, resource: str, latency_s: float, error: Exception
    ) -> None:
        """Log a call that failed, from what the error says about it."""
        if isinstance(error, LLMError):
            outcome = f"error: {error}"
            usage, attempt, raw_response = (
                error.usage,
                error.attempt,
                error.raw_response,
            )
        else:  # a bug or an SDK error nobody expected: keep the trace in the log
            outcome = f"error: unexpected {type(error).__name__}: {error}"
            usage, attempt, raw_response = None, None, ""
        cost: float | None = 0.0  # nothing was billed unless the API reported usage
        if usage is not None:
            try:
                cost = estimate_cost(
                    prompt.model,
                    usage.input_tokens,
                    usage.cached_tokens,
                    usage.output_tokens,
                    self._prices,
                )
            except LLMInputError:
                cost = None
        self._log(
            prompt,
            resource,
            outcome,
            latency_s,
            raw_response=raw_response,
            usage=usage,
            attempt=attempt,
            cost=cost,
        )

    def _log(
        self,
        prompt: LoadedPrompt,
        resource: str,
        outcome: str,
        latency_s: float,
        *,
        raw_response: str,
        usage: Usage | None,
        attempt: int | None,
        cost: float | None,
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
                usage=usage,
                cost_usd=cost,
                latency_s=latency_s,
                attempt=attempt,
            )
        )


@functools.cache
def _validator(name: str, version: int) -> Draft202012Validator:
    """The validator of a prompt version's schema, built once; the schema itself is checked first."""
    schema = output_schema(name, version)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _first_schema_problem(validator: Draft202012Validator, output: Any) -> str | None:
    """The first schema violation of ``output`` (in a stable order), or None when it is valid."""
    errors = sorted(validator.iter_errors(output), key=lambda e: e.json_path)
    if not errors:
        return None
    return f"{errors[0].json_path}: {errors[0].message}"
