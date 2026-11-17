"""The ``propose`` pipeline step: themes and keywords for every resource of a catalogue (ADR-0016, 0017, 0019).

Design doc ``docs/design/theme-keyword-proposals.md`` sections 7 and 8, OpenSpec capability ``propose-step``.

``run_propose`` reads the resources of one catalogue from the database (main thread), proposes themes and then
keywords for each one in a thread pool (``propose.propose_themes`` / ``propose_keywords``, which never touch
SQLite), and saves the results with ``replace_proposals`` from the main thread only, in resource-id order, so a
run stores the same rows for any number of workers.

Everything that goes wrong for one resource (a failed call, an unreadable resource, a result that cannot be
stored, an unexpected exception) becomes an ``error`` of that resource and the run goes on; only a ``PromptError``
(a broken prompt file, a setup problem of the whole run) stops it. A call that failed never deletes the earlier
proposals of its property. Every valid proposal is saved, ``propose_report.xlsx`` is written on every non-dry run,
and if any resource failed the step raises ``ProposeIncompleteError`` afterwards (the ADR-0016 pattern).

A dry run goes through the same flow with ``DryRunClient`` instead of the model: it counts the calls the run
would make and a rough cost, and writes nothing.

Usage (normally through ``pipeline``):

    summary = run_propose("PROD", "nsip-lu", conn=conn, fetched_data_dir=Path("fetched_catalogues_data"))
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import openpyxl
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.worksheet.worksheet import Worksheet

from mqa_advisor.catalogue_db import ProposedValue, read_proposals, replace_proposals
from mqa_advisor.llm import (
    DEFAULT_LOG_DIR,
    PRICES_AS_OF,
    AnthropicBackend,
    Backend,
    LLMClient,
    LLMError,
    LLMInputError,
    LLMResult,
    RunLog,
    estimate_cost,
    load_api_key,
)
from mqa_advisor.prompts import LoadedPrompt, PromptError
from mqa_advisor.propose import (
    DCAT_KEYWORD,
    DCAT_THEME,
    THEMES_PROMPT,
    KeywordResult,
    ResourceText,
    ThemeResult,
    propose_keywords,
    propose_themes,
    read_resource,
)
from mqa_advisor.vocabulary import Vocabulary, load_vocabulary

logger = logging.getLogger(__name__)

REPORT_NAME = "propose_report.xlsx"
DEFAULT_WORKERS = 4
# The dry-run estimate is crude on purpose: about 4 bytes of text per token, and a fixed answer size.
BYTES_PER_TOKEN = 4
DRY_RUN_OUTPUT_TOKENS = 100
ERROR_HEADER = (
    "resource_type",
    "resource_id",
    "call",
    "severity",
    "message",
    "prompt_name",
    "prompt_version",
    "model",
    "time_utc",
)


@dataclass(frozen=True)
class ProposeOptions:
    """The options of the step: ``limit`` (the first N resources by id), ``dry_run`` and ``workers``."""

    limit: int | None = None
    dry_run: bool = False
    workers: int = DEFAULT_WORKERS

    def __post_init__(self) -> None:
        # The one place that validates them: `main`, `pipeline.run` and `run_propose` all build or receive this.
        if self.workers < 1 or (self.limit is not None and self.limit < 1):
            raise ValueError("--workers and --limit must be positive integers")


@dataclass(frozen=True)
class ReportRow:
    """One row of the Errors sheet: a warning or an error about a resource."""

    resource_type: str
    resource_id: str
    call: str  # "themes", "keywords" or "step" (a failure outside both calls)
    severity: str  # "error" or "warning"
    message: str
    prompt_name: str | None
    prompt_version: int | None
    model: str | None
    time_utc: str


@dataclass(frozen=True)
class ProposeSummary:
    """What a run did. On a dry run the token and cost figures are rough estimates and nothing was written.

    The token figures cover the answered calls; ``cost_usd`` is the run log's total, which also includes failed
    calls that were billed (on a dry run it is the sum of the estimates).
    """

    env: str
    catalogue: str
    dry_run: bool
    resources: int
    stored: int  # resources with at least one new proposal and no error
    skipped: int  # resources with nothing to propose and no error
    failed: (
        int  # resources with at least one error (their valid proposals are still saved)
    )
    themes: int
    keywords: int
    calls: int
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    cost_usd: float
    errors: int
    warnings: int
    report_path: Path | None
    log_path: Path | None


class ProposeIncompleteError(Exception):
    """Raised by ``run_propose``, after the proposals are saved and the report is written, when a resource failed.

    ``failures`` are the ``error`` rows (sorted by resource id) and ``summary`` what the run did. If the report
    could not be written, ``report_path`` is ``None`` and ``report_error`` says why; on a dry run there is no
    report at all. ``log_error`` is set when the run log could not be written either. The failures are carried
    in every case.
    """

    def __init__(
        self,
        failures: tuple[ReportRow, ...],
        summary: ProposeSummary,
        report_path: Path | None,
        report_error: Exception | None = None,
        log_error: LLMError | None = None,
    ):
        if summary.dry_run:
            where = "dry run, nothing written"
        elif report_path:
            where = f"see {report_path}"
        else:
            where = f"the report {REPORT_NAME} was NOT written: {report_error}"
        message = (
            f"{len(failures)} error(s) in {summary.resources} resource(s); {where}"
        )
        if log_error:
            message += f"; the run log is incomplete: {log_error}"
        super().__init__(message)
        self.failures = failures
        self.summary = summary
        self.report_path = report_path
        self.report_error = report_error
        self.log_error = log_error


# --- the model, real or pretended ---------------------------------------------------------------


class _Caller(Protocol):
    """What the propose functions need from a client."""

    def call(self, prompt: LoadedPrompt, *, resource: str) -> LLMResult: ...


class _CountingClient:
    """Wraps a client for one resource and sums the tokens and cost of its answered calls."""

    def __init__(self, client: _Caller):
        self._client = client
        self.input_tokens = self.cached_tokens = self.output_tokens = 0
        self.cost_usd = 0.0

    def call(self, prompt: LoadedPrompt, *, resource: str) -> LLMResult:
        result = self._client.call(prompt, resource=resource)
        self.input_tokens += result.input_tokens
        self.cached_tokens += result.cached_tokens
        self.output_tokens += result.output_tokens
        self.cost_usd += result.cost_usd
        return result


class DryRunClient:
    """Stands in for ``LLMClient`` on a dry run: no model, no log, a rough cost from the text length.

    The canned answers validate (as many vocabulary codes for themes as the prompt asks for, none of them one the
    prompt already lists as existing; one keyword), so exactly the calls of a clean run are counted, without
    invented "list cut" warnings, and the keyword prompt sees effective themes. The number asked for is read from
    the prompt's "up to N" wording; if that wording is gone the call raises, so the dry run fails loudly instead of
    guessing. A model missing from the price table costs zero here, with one warning.
    """

    def __init__(self, vocabulary: Vocabulary):
        self._codes = [entry.code for entry in vocabulary.entries]
        self._unpriced: set[str] = set()
        self._lock = threading.Lock()

    def call(self, prompt: LoadedPrompt, *, resource: str) -> LLMResult:
        size = len(prompt.system.encode()) + len(prompt.user.encode())
        tokens = size // BYTES_PER_TOKEN + 1
        if prompt.name == THEMES_PROMPT:
            asked = re.search(r"up to (\d+)", prompt.user)
            if asked is None:
                raise ValueError(
                    f'the {THEMES_PROMPT} prompt no longer says "up to N": the dry run cannot size its answer'
                )
            free = [c for c in self._codes if f"{c} —" not in prompt.user]
            output = {"themes": free[-max(1, int(asked.group(1))) :]}
        else:
            output = {"keywords": ["dry run keyword"], "language": "eng"}
        try:
            cost = estimate_cost(prompt.model, tokens, 0, DRY_RUN_OUTPUT_TOKENS)
        except LLMInputError as error:
            cost = 0.0
            with self._lock:
                if prompt.model not in self._unpriced:
                    self._unpriced.add(prompt.model)
                    logger.warning(
                        "dry run: %s is not priced, its calls count as USD 0 (%s)",
                        prompt.model,
                        error,
                    )
        return LLMResult(
            output=output,
            input_tokens=tokens,
            cached_tokens=0,
            output_tokens=DRY_RUN_OUTPUT_TOKENS,
            cost_usd=cost,
            latency_s=0.0,
            attempt=1,
        )


# --- one resource -------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Outcome:
    """A resource after its calls (from a worker) and, once finalised, its stored rows and report rows."""

    resource: ResourceText
    themes: ThemeResult | None
    keywords: KeywordResult | None
    problems: tuple[
        str, ...
    ]  # failures outside the two calls: unexpected exceptions, read and store errors
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    values: tuple[
        ProposedValue, ...
    ] = ()  # the new rows (stored; on a dry run, only proposed)
    rows: tuple[ReportRow, ...] = ()

    @property
    def calls(self) -> int:
        return sum(r.calls for r in (self.themes, self.keywords) if r)

    @property
    def failed(self) -> bool:
        return bool(
            self.problems
            or any(r and r.status == "error" for r in (self.themes, self.keywords))
        )


def _process(
    resource: ResourceText, client: _Caller, vocabulary: Vocabulary
) -> _Outcome:
    """Themes, then keywords, for one resource. Runs in a worker thread.

    Raises only ``PromptError``; any other exception becomes a problem of this resource.
    """
    counting = _CountingClient(client)
    themes = keywords = None
    problems: tuple[str, ...] = ()
    try:
        themes = propose_themes(resource, counting, vocabulary)
        keywords = propose_keywords(resource, counting, themes, vocabulary)
    except PromptError:
        raise  # a broken prompt file is a setup problem of the whole run: stop it
    except Exception as error:  # noqa: BLE001
        # One resource must never stop the others (ADR-0016): anything the propose functions did not turn into
        # an issue (a bug) becomes an error of this resource.
        problems = (f"unexpected: {type(error).__name__}: {error}",)
        logger.exception("resource %s: %s", resource.resource_id, problems[0])
    return _Outcome(
        resource,
        themes,
        keywords,
        problems,
        counting.input_tokens,
        counting.cached_tokens,
        counting.output_tokens,
        counting.cost_usd,
    )


def _unreadable(
    pk: int, resource_id: str, resource_type: str, error: Exception
) -> Future:
    """A finished future for a resource that could not be read: it has no calls and one problem."""
    problem = f"could not read the resource: {type(error).__name__}: {error}"
    logger.error("resource %s: %s", resource_id, problem)
    stub = ResourceText(pk, resource_id, resource_type, None, "unreadable")
    future: Future = Future()
    future.set_result(_Outcome(stub, None, None, (problem,)))
    return future


def _new_values(outcome: _Outcome, moment: datetime) -> tuple[ProposedValue, ...]:
    """The rows to store for one resource: its new themes and keywords, with their provenance.

    Raises ``ValueError`` when a call succeeded but its result cannot be stored (no own entity or prompt): that
    would otherwise be a paid answer thrown away silently.
    """
    resource = outcome.resource
    values: list[ProposedValue] = []
    for call, result, prop, kind in (
        ("themes", outcome.themes, DCAT_THEME, "uri"),
        ("keywords", outcome.keywords, DCAT_KEYWORD, "literal"),
    ):
        if result is None or result.status != "ok":
            continue
        if not resource.entity_id or result.prompt is None:
            raise ValueError(
                f"the {call} call succeeded but its result cannot be stored (no own entity or prompt)"
            )
        items = result.themes if isinstance(result, ThemeResult) else result.keywords
        lang = None if isinstance(result, ThemeResult) else result.lang
        values += [
            ProposedValue(
                resource.entity_id,
                resource.resource_type,
                prop,
                item,
                kind,
                lang,
                result.prompt.name,
                result.prompt.version,
                result.prompt.sha256,
                result.prompt.model,
                moment,
            )
            for item in items
        ]
    return tuple(values)


def _kept_rows(conn: sqlite3.Connection, outcome: _Outcome) -> list[ProposedValue]:
    """The earlier proposals of every property whose call failed: a failed call never deletes them."""
    failed = {
        prop
        for prop, result in (
            (DCAT_THEME, outcome.themes),
            (DCAT_KEYWORD, outcome.keywords),
        )
        if result is None or result.status == "error"
    }
    if not failed:
        return []
    return [
        old
        for old in read_proposals(conn, outcome.resource.resource_pk)
        if old.property_uri in failed
    ]


def _report_rows(outcome: _Outcome, moment: datetime) -> tuple[ReportRow, ...]:
    """The problems of one resource as report rows.

    A failed call always gets an ``error`` row (one is made up if the call recorded none), so the report and the
    failure count cannot disagree. The "no title" warning the keyword call repeats is listed once.
    """
    resource = outcome.resource
    at = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows: list[ReportRow] = []
    seen_warnings: set[str] = set()
    for call, result in (("themes", outcome.themes), ("keywords", outcome.keywords)):
        if result is None:
            continue
        prompt = result.prompt
        made = [
            (issue.severity, issue.message)
            for issue in result.issues
            if not (issue.severity == "warning" and issue.message in seen_warnings)
        ]
        seen_warnings.update(m for s, m in made if s == "warning")
        if result.status == "error" and not any(s == "error" for s, _ in made):
            made.append(("error", f"the {call} call failed without giving a reason"))
        rows += [
            ReportRow(
                resource.resource_type,
                resource.resource_id,
                call,
                severity,
                message,
                prompt.name if prompt else None,
                prompt.version if prompt else None,
                prompt.model if prompt else None,
                at,
            )
            for severity, message in made
        ]
    rows += [
        ReportRow(
            resource.resource_type,
            resource.resource_id,
            "step",
            "error",
            problem,
            None,
            None,
            None,
            at,
        )
        for problem in outcome.problems
    ]
    return tuple(rows)


def _finalise(conn: sqlite3.Connection, outcome: _Outcome, dry_run: bool) -> _Outcome:
    """Store the outcome's proposals (not on a dry run) and fill in its rows. Main thread only.

    A store problem (a bad row, a database error) is an error of this resource: the others go on, and
    ``replace_proposals`` has rolled the resource back.
    """
    moment = datetime.now(UTC)
    problems = list(outcome.problems)
    values: tuple[ProposedValue, ...] = ()
    if outcome.themes is not None or outcome.keywords is not None:
        try:
            values = _new_values(outcome, moment)
            if not dry_run:
                replace_proposals(
                    conn,
                    outcome.resource.resource_pk,
                    [*values, *_kept_rows(conn, outcome)],
                )
        except (ValueError, RuntimeError, sqlite3.Error) as error:
            values = ()
            problems.append(
                f"the proposals could not be stored: {type(error).__name__}: {error}"
            )
            logger.error("resource %s: %s", outcome.resource.resource_id, problems[-1])
    final = replace(outcome, problems=tuple(problems), values=values)
    return replace(final, rows=_report_rows(final, moment))


# --- the run ------------------------------------------------------------------------------------


def _resource_keys(
    conn: sqlite3.Connection, env: str, catalogue: str, limit: int | None
) -> list[tuple[int, str, str]]:
    sql = (
        "SELECT resource_pk, resource_id, resource_type FROM resource"
        " WHERE environment = ? AND catalogue = ? ORDER BY resource_id, resource_type"
    )
    params: tuple = (env, catalogue)
    if limit is not None:
        sql += " LIMIT ?"
        params += (limit,)
    return [tuple(row) for row in conn.execute(sql, params)]


def _default_backend() -> Backend:
    return AnthropicBackend(load_api_key())


def run_propose(
    env: str,
    catalogue: str,
    *,
    conn: sqlite3.Connection,
    fetched_data_dir: Path,
    options: ProposeOptions | None = None,
    backend_factory: Callable[[], Backend] = _default_backend,
    vocabulary: Vocabulary | None = None,
    log_dir: Path = DEFAULT_LOG_DIR,
) -> ProposeSummary:
    """Propose themes and keywords for the first ``options.limit`` resources of ``catalogue`` (all by default).

    ``conn`` must come from ``connect()`` and have no pending writes. On a dry run nothing is written (no rows, no
    log file, no report) and no key is needed; ``backend_factory`` is not called. Raises
    ``ProposeIncompleteError`` after saving and reporting when any resource failed (on a dry run, without a
    report); ``PromptError`` when a prompt file is broken (the run stops); the report's exception when it cannot be
    written and nothing failed; ``LLMError`` when the run log could not be written and nothing else failed.
    """
    options = options or ProposeOptions()
    dry_run = options.dry_run
    vocabulary = vocabulary or load_vocabulary()
    keys = _resource_keys(conn, env, catalogue, options.limit)
    logger.info(
        "%s/%s: proposing for %d resource(s) with %d worker(s)%s; data-theme vocabulary %s",
        env,
        catalogue,
        len(keys),
        options.workers,
        " (dry run)" if dry_run else "",
        vocabulary.version,
    )

    run_log = RunLog(catalogue, enabled=not dry_run, log_dir=log_dir)
    backend = None
    try:
        if dry_run:
            client: _Caller = DryRunClient(vocabulary)
        else:
            backend = backend_factory()
            client = LLMClient(backend, run_log)
        outcomes = _process_and_store(
            conn, keys, client, vocabulary, options.workers, dry_run
        )
    finally:
        if backend is not None:
            backend.close()
        log_error = _close_log(run_log)

    summary = _summarise(env, catalogue, dry_run, outcomes, run_log)
    rows = [row for outcome in outcomes for row in outcome.rows]
    failures = tuple(row for row in rows if row.severity == "error")
    logger.info(
        "%s/%s: %d resource(s): %d with proposals, %d skipped, %d failed; %d theme(s), %d keyword(s), %d call(s),"
        " %d error(s), %d warning(s); estimated cost USD %.6f (the run log total is the authoritative one; the"
        " token counts cover the answered calls only)",
        env,
        catalogue,
        summary.resources,
        summary.stored,
        summary.skipped,
        summary.failed,
        summary.themes,
        summary.keywords,
        summary.calls,
        summary.errors,
        summary.warnings,
        summary.cost_usd,
    )
    failed = any(outcome.failed for outcome in outcomes)
    if dry_run:
        if failed:
            raise ProposeIncompleteError(failures, summary, None)
        return summary

    report_path = Path(fetched_data_dir) / env / catalogue / REPORT_NAME
    report_error: Exception | None = None
    try:
        write_report(report_path, summary, rows, run_log)
        summary = replace(summary, report_path=report_path)
    except Exception as error:  # noqa: BLE001
        # Whatever goes wrong while building or saving the workbook (a full disk, a file open in Excel, a value the
        # library rejects) must not hide the failures of the run: it is reported alongside them.
        report_error = error
        logger.error("writing the report %s failed: %s", report_path, error)
    if failed:
        raise ProposeIncompleteError(
            failures,
            summary,
            None if report_error else report_path,
            report_error,
            log_error,
        )
    if report_error:
        raise report_error
    if log_error:
        raise log_error
    return summary


def _close_log(run_log: RunLog) -> LLMError | None:
    """Close the run log; a failure to write it was already logged at ERROR, so it is returned, not raised."""
    try:
        run_log.close()
    except LLMError as error:
        return error
    return None


def _process_and_store(
    conn: sqlite3.Connection,
    keys: list[tuple[int, str, str]],
    client: _Caller,
    vocabulary: Vocabulary,
    workers: int,
    dry_run: bool,
) -> list[_Outcome]:
    """Run the resources through the pool; finalise (store) each result in resource order."""
    outcomes: list[_Outcome] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures: list[Future] = []
        for pk, resource_id, resource_type in keys:
            try:
                resource = read_resource(conn, pk)
            except (ValueError, sqlite3.Error) as error:
                futures.append(_unreadable(pk, resource_id, resource_type, error))
            else:
                futures.append(pool.submit(_process, resource, client, vocabulary))
        try:
            # In submission order, whatever order the workers finish in: the writes are deterministic.
            for future in futures:
                outcome = _finalise(conn, future.result(), dry_run)
                logger.info(
                    "resource %s: %d call(s), %d item(s) %s, estimated cost USD %.6f",
                    outcome.resource.resource_id,
                    outcome.calls,
                    len(outcome.values),
                    "proposed" if dry_run else "stored",
                    outcome.cost_usd,
                )
                outcomes.append(outcome)
        except BaseException:
            # A broken prompt or an unexpected error stops the run. This only cancels the calls that have not
            # started; those already running finish (and are billed) before the pool shuts down.
            for future in futures:
                future.cancel()
            raise
    return outcomes


def _summarise(
    env: str,
    catalogue: str,
    dry_run: bool,
    outcomes: list[_Outcome],
    run_log: RunLog,
) -> ProposeSummary:
    failed = sum(o.failed for o in outcomes)
    stored = sum(bool(o.values) and not o.failed for o in outcomes)
    all_rows = [row for o in outcomes for row in o.rows]
    return ProposeSummary(
        env=env,
        catalogue=catalogue,
        dry_run=dry_run,
        resources=len(outcomes),
        stored=stored,
        skipped=len(outcomes) - stored - failed,
        failed=failed,
        themes=sum(v.property_uri == DCAT_THEME for o in outcomes for v in o.values),
        keywords=sum(
            v.property_uri == DCAT_KEYWORD for o in outcomes for v in o.values
        ),
        calls=sum(o.calls for o in outcomes),
        input_tokens=sum(o.input_tokens for o in outcomes),
        cached_tokens=sum(o.cached_tokens for o in outcomes),
        output_tokens=sum(o.output_tokens for o in outcomes),
        # The run log also counts failed-but-billed calls, so it is the real total; a dry run has no log.
        cost_usd=sum(o.cost_usd for o in outcomes)
        if dry_run
        else run_log.total_cost_usd,
        errors=sum(r.severity == "error" for r in all_rows),
        warnings=sum(r.severity == "warning" for r in all_rows),
        report_path=None,
        log_path=run_log.path,
    )


# --- the report ---------------------------------------------------------------------------------


def _append(sheet: Worksheet, values: list) -> None:
    """Append a row. Text is cleaned of characters a workbook cannot hold and never becomes a formula."""
    sheet.append(
        [ILLEGAL_CHARACTERS_RE.sub("", v) if isinstance(v, str) else v for v in values]
    )
    for cell in sheet[sheet.max_row]:
        if isinstance(cell.value, str):
            cell.data_type = (
                "s"  # a text starting with "=" would otherwise be saved as a formula
            )


def write_report(
    path: Path, summary: ProposeSummary, rows: list[ReportRow], run_log: RunLog
) -> None:
    """Write the Errors and Summary sheets to ``path`` (always, also with no rows).

    The workbook is saved next to ``path`` and renamed over it, so a failed write never leaves a truncated file
    as the report. Raises whatever the workbook library or the file system raises.
    """
    workbook = openpyxl.Workbook()
    errors = workbook.active
    errors.title = "Errors"
    _append(errors, list(ERROR_HEADER))
    # By resource id, errors before warnings within a resource.
    for row in sorted(rows, key=lambda r: (r.resource_id, r.severity != "error")):
        _append(
            errors,
            [
                row.resource_type,
                row.resource_id,
                row.call,
                row.severity,
                row.message,
                row.prompt_name,
                row.prompt_version,
                row.model,
                row.time_utc,
            ],
        )
    sheet = workbook.create_sheet("Summary")
    _append(sheet, ["item", "value"])
    for item in (
        ("environment", summary.env),
        ("catalogue", summary.catalogue),
        ("run id", run_log.run_id),
        ("log file", str(summary.log_path) if summary.log_path else ""),
        ("resources processed", summary.resources),
        ("resources with proposals", summary.stored),
        ("resources skipped", summary.skipped),
        ("resources with errors", summary.failed),
        ("themes stored", summary.themes),
        ("keywords stored", summary.keywords),
        ("errors", summary.errors),
        ("warnings", summary.warnings),
        ("calls", summary.calls),
        ("input tokens", summary.input_tokens),
        ("cached input tokens", summary.cached_tokens),
        ("output tokens", summary.output_tokens),
        ("estimated cost USD", round(summary.cost_usd, 6)),
        ("prices as of", PRICES_AS_OF),
        (
            "note",
            "token counts cover the answered calls; the cost is the run log total (failed calls included)",
        ),
    ):
        _append(sheet, list(item))

    temporary = path.with_name(path.name + ".tmp")
    try:
        workbook.save(temporary)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
