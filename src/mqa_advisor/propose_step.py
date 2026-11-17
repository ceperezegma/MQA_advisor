"""The ``propose`` pipeline step: themes and keywords for every resource of a catalogue (ADR-0016, 0017, 0019).

Design doc ``docs/design/theme-keyword-proposals.md`` sections 7 and 8, OpenSpec capability ``propose-step``.

``run_propose`` reads the resources of one catalogue from the database (main thread), proposes themes and then
keywords for each one in a thread pool (``propose.propose_themes`` / ``propose_keywords``, which never touch
SQLite), and saves the results with ``replace_proposals`` from the main thread only, in resource-id order, so a
run stores the same rows for any number of workers. Every resource is attempted and every valid proposal saved;
``propose_report.xlsx`` is written on every non-dry run, and if any ``error`` exists the step raises
``ProposeIncompleteError`` afterwards (the ADR-0016 pattern).

A dry run goes through the same flow with ``DryRunClient`` instead of the model: it counts the calls the run
would make and a rough cost, and writes nothing.

Usage (normally through ``pipeline``):

    summary = run_propose("PROD", "nsip-lu", conn=conn, fetched_data_dir=Path("fetched_catalogues_data"))
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import openpyxl

from mqa_advisor.catalogue_db import ProposedValue, replace_proposals
from mqa_advisor.llm import (
    DEFAULT_LOG_DIR,
    PRICES_AS_OF,
    AnthropicBackend,
    Backend,
    LLMClient,
    LLMError,
    LLMResult,
    RunLog,
    estimate_cost,
    load_api_key,
)
from mqa_advisor.prompts import LoadedPrompt
from mqa_advisor.propose import (
    DCAT_KEYWORD,
    DCAT_THEME,
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


class ProposeIncompleteError(Exception):
    """Raised by ``run_propose``, after the proposals are saved and the report is written, when any ``error`` exists.

    ``failures`` are the ``error`` rows (sorted by resource id). If the report itself could not be written,
    ``report_path`` is ``None`` and ``report_error`` says why; the failures are still carried.
    """

    def __init__(
        self,
        failures: tuple[ReportRow, ...],
        resources: int,
        report_path: Path | None,
        report_error: OSError | None = None,
    ):
        where = (
            f"see {report_path}"
            if report_path
            else f"the report {REPORT_NAME} was NOT written: {report_error}"
        )
        super().__init__(
            f"{len(failures)} error(s) in {resources} resource(s); {where}"
        )
        self.failures = failures
        self.resources = resources
        self.report_path = report_path
        self.report_error = report_error


@dataclass(frozen=True)
class ProposeSummary:
    """What a run did. On a dry run the token and cost figures are rough estimates and nothing was written."""

    env: str
    catalogue: str
    dry_run: bool
    resources: int
    stored: int  # resources with at least one proposal and no error
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


# --- the model, real or pretended ---------------------------------------------------------------


class _CountingClient:
    """Wraps a client for one resource and sums the tokens and cost of its answered calls."""

    def __init__(self, client: LLMClient | DryRunClient):
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

    The canned answers validate (as many vocabulary codes for themes as the prompt asks for, taken from the end of
    the list; one keyword), so exactly the calls of a clean run are counted, without invented "list cut" warnings,
    and the keyword prompt sees effective themes.
    """

    def __init__(self, vocabulary: Vocabulary):
        self._codes = [entry.code for entry in vocabulary.entries]

    def call(self, prompt: LoadedPrompt, *, resource: str) -> LLMResult:
        size = len(prompt.system.encode()) + len(prompt.user.encode())
        tokens = size // BYTES_PER_TOKEN + 1
        if prompt.name == "propose_themes":
            asked = re.search(r"up to (\d+)", prompt.user)
            output = {
                "themes": self._codes[-int(asked.group(1)) :]
                if asked
                else self._codes[-1:]
            }
        else:
            output = {"keywords": ["dry run keyword"], "language": "eng"}
        return LLMResult(
            output=output,
            input_tokens=tokens,
            cached_tokens=0,
            output_tokens=DRY_RUN_OUTPUT_TOKENS,
            cost_usd=estimate_cost(prompt.model, tokens, 0, DRY_RUN_OUTPUT_TOKENS),
            latency_s=0.0,
            attempt=1,
        )


# --- one resource -------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Outcome:
    resource: ResourceText
    themes: ThemeResult | None
    keywords: KeywordResult | None
    unexpected: str | None  # an exception that is not an LLMError, as text
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    cost_usd: float

    @property
    def calls(self) -> int:
        return sum(r.calls for r in (self.themes, self.keywords) if r)

    @property
    def failed(self) -> bool:
        return bool(
            self.unexpected
            or any(r and r.status == "error" for r in (self.themes, self.keywords))
        )


def _process(
    resource: ResourceText, client: LLMClient | DryRunClient, vocabulary: Vocabulary
) -> _Outcome:
    """Themes, then keywords, for one resource. Runs in a worker thread; never raises."""
    counting = _CountingClient(client)
    themes = keywords = None
    unexpected = None
    try:
        themes = propose_themes(resource, counting, vocabulary)
        keywords = propose_keywords(resource, counting, themes, vocabulary)
    except Exception as error:  # noqa: BLE001
        # One resource must never stop the others (ADR-0016): anything the propose functions did not turn into
        # an issue (a broken prompt file, a bug) becomes an error row of this resource.
        unexpected = f"unexpected: {type(error).__name__}: {error}"
        logger.exception("resource %s: %s", resource.resource_id, unexpected)
    return _Outcome(
        resource,
        themes,
        keywords,
        unexpected,
        counting.input_tokens,
        counting.cached_tokens,
        counting.output_tokens,
        counting.cost_usd,
    )


def _proposed_values(outcome: _Outcome, moment: datetime) -> list[ProposedValue]:
    """The rows to store for one resource: its new themes and keywords, with their provenance."""
    resource = outcome.resource
    values: list[ProposedValue] = []
    themes, keywords = outcome.themes, outcome.keywords
    if themes and themes.status == "ok" and themes.prompt and resource.entity_id:
        values += [
            ProposedValue(
                resource.entity_id,
                resource.resource_type,
                DCAT_THEME,
                uri,
                "uri",
                None,
                themes.prompt.name,
                themes.prompt.version,
                themes.prompt.sha256,
                themes.prompt.model,
                moment,
            )
            for uri in themes.themes
        ]
    if keywords and keywords.status == "ok" and keywords.prompt and resource.entity_id:
        values += [
            ProposedValue(
                resource.entity_id,
                resource.resource_type,
                DCAT_KEYWORD,
                keyword,
                "literal",
                keywords.lang,
                keywords.prompt.name,
                keywords.prompt.version,
                keywords.prompt.sha256,
                keywords.prompt.model,
                moment,
            )
            for keyword in keywords.keywords
        ]
    return values


def _report_rows(outcome: _Outcome, moment: datetime) -> list[ReportRow]:
    """The problems of one resource as report rows; a message the keyword call repeats is listed once."""
    resource = outcome.resource
    at = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows: list[ReportRow] = []
    seen: set[tuple[str, str]] = set()
    for call, result in (("themes", outcome.themes), ("keywords", outcome.keywords)):
        if result is None:
            continue
        prompt = result.prompt
        for issue in result.issues:
            if (issue.severity, issue.message) in seen:
                continue  # the "no title" skip is reported by both calls
            seen.add((issue.severity, issue.message))
            rows.append(
                ReportRow(
                    resource.resource_type,
                    resource.resource_id,
                    call,
                    issue.severity,
                    issue.message,
                    prompt.name if prompt else None,
                    prompt.version if prompt else None,
                    prompt.model if prompt else None,
                    at,
                )
            )
    if outcome.unexpected:
        rows.append(
            ReportRow(
                resource.resource_type,
                resource.resource_id,
                "step",
                "error",
                outcome.unexpected,
                None,
                None,
                None,
                at,
            )
        )
    return rows


# --- the run ------------------------------------------------------------------------------------


def _resource_pks(
    conn: sqlite3.Connection, env: str, catalogue: str, limit: int | None
) -> list[int]:
    sql = (
        "SELECT resource_pk FROM resource WHERE environment = ? AND catalogue = ?"
        " ORDER BY resource_id, resource_type"
    )
    params: tuple = (env, catalogue)
    if limit is not None:
        sql += " LIMIT ?"
        params += (limit,)
    return [row[0] for row in conn.execute(sql, params)]


def _default_backend() -> Backend:
    return AnthropicBackend(load_api_key())


def run_propose(
    env: str,
    catalogue: str,
    *,
    conn: sqlite3.Connection,
    fetched_data_dir: Path,
    limit: int | None = None,
    dry_run: bool = False,
    workers: int = DEFAULT_WORKERS,
    backend_factory: Callable[[], Backend] = _default_backend,
    vocabulary: Vocabulary | None = None,
    log_dir: Path = DEFAULT_LOG_DIR,
) -> ProposeSummary:
    """Propose themes and keywords for the first ``limit`` resources of ``catalogue`` (all by default).

    ``conn`` must come from ``connect()`` and have no pending writes. On a dry run nothing is written (no rows, no
    log file, no report) and no key is needed; ``backend_factory`` is not called. Raises
    ``ProposeIncompleteError`` after saving and reporting when any resource had an ``error``; ``OSError`` when the
    report cannot be written and there was no error to report; ``LLMError`` when the run log could not be written.
    """
    if workers < 1 or (limit is not None and limit < 1):
        raise ValueError("workers and limit must be positive integers")
    vocabulary = vocabulary or load_vocabulary()
    resources = [
        read_resource(conn, pk) for pk in _resource_pks(conn, env, catalogue, limit)
    ]
    logger.info(
        "%s/%s: proposing for %d resource(s) with %d worker(s)%s; data-theme vocabulary %s",
        env,
        catalogue,
        len(resources),
        workers,
        " (dry run)" if dry_run else "",
        vocabulary.version,
    )

    run_log = RunLog(catalogue, enabled=not dry_run, log_dir=log_dir)
    backend = None
    try:
        if dry_run:
            client: LLMClient | DryRunClient = DryRunClient(vocabulary)
        else:
            backend = backend_factory()
            client = LLMClient(backend, run_log)
        outcomes = _process_and_store(
            conn, resources, client, vocabulary, workers, dry_run
        )
    finally:
        if backend is not None:
            backend.close()
        log_error = _close_log(run_log)

    summary = _summarise(env, catalogue, dry_run, outcomes, run_log)
    rows = [
        row for outcome in outcomes for row in _report_rows(outcome, datetime.now(UTC))
    ]
    logger.info(
        "%s/%s: %d resource(s): %d with proposals, %d skipped, %d failed; %d theme(s), %d keyword(s), %d call(s),"
        " %d error(s), %d warning(s); estimated cost USD %.6f (the run log total is the authoritative one)",
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
    if dry_run:
        return summary

    report_path = Path(fetched_data_dir) / env / catalogue / REPORT_NAME
    report_error: OSError | None = None
    try:
        write_report(report_path, summary, rows, run_log)
        summary = replace(summary, report_path=report_path)
    except OSError as error:
        report_error = error
        logger.error("writing the report %s failed: %s", report_path, error)
    failures = tuple(row for row in rows if row.severity == "error")
    if failures:
        raise ProposeIncompleteError(
            failures,
            summary.resources,
            None if report_error else report_path,
            report_error,
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
    resources: list[ResourceText],
    client: LLMClient | DryRunClient,
    vocabulary: Vocabulary,
    workers: int,
    dry_run: bool,
) -> list[_Outcome]:
    """Run the resources through the pool; save each result in resource order (not on a dry run)."""
    outcomes: list[_Outcome] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_process, r, client, vocabulary) for r in resources]
        try:
            # In submission order, whatever order the workers finish in: the writes are deterministic.
            for future in futures:
                outcome = future.result()
                values = _proposed_values(outcome, datetime.now(UTC))
                # A resource whose calls all failed keeps its earlier proposals; anything else replaces them.
                if not dry_run and (values or not outcome.failed):
                    replace_proposals(conn, outcome.resource.resource_pk, values)
                logger.info(
                    "resource %s: %d call(s), %d item(s) %s, estimated cost USD %.6f",
                    outcome.resource.resource_id,
                    outcome.calls,
                    len(values),
                    "proposed" if dry_run else "stored",
                    outcome.cost_usd,
                )
                outcomes.append(outcome)
        except BaseException:
            for future in futures:  # a write failed: do not spend money on the rest
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
    moment = datetime.now(UTC)
    themes = keywords = stored = failed = errors = warnings = 0
    for outcome in outcomes:
        values = _proposed_values(outcome, moment)
        themes += sum(v.property_uri == DCAT_THEME for v in values)
        keywords += sum(v.property_uri == DCAT_KEYWORD for v in values)
        # Each resource is in exactly one outcome class; a failed one may still have saved valid proposals.
        failed += outcome.failed
        stored += bool(values) and not outcome.failed
        for row in _report_rows(outcome, moment):
            errors += row.severity == "error"
            warnings += row.severity == "warning"
    return ProposeSummary(
        env=env,
        catalogue=catalogue,
        dry_run=dry_run,
        resources=len(outcomes),
        stored=stored,
        skipped=len(outcomes) - stored - failed,
        failed=failed,
        themes=themes,
        keywords=keywords,
        calls=sum(o.calls for o in outcomes),
        input_tokens=sum(o.input_tokens for o in outcomes),
        cached_tokens=sum(o.cached_tokens for o in outcomes),
        output_tokens=sum(o.output_tokens for o in outcomes),
        # The run log also counts failed-but-billed calls, so it is the real total; a dry run has no log.
        cost_usd=sum(o.cost_usd for o in outcomes)
        if dry_run
        else run_log.total_cost_usd,
        errors=errors,
        warnings=warnings,
        report_path=None,
        log_path=run_log.path,
    )


# --- the report ---------------------------------------------------------------------------------


def write_report(
    path: Path, summary: ProposeSummary, rows: list[ReportRow], run_log: RunLog
) -> None:
    """Write the Errors and Summary sheets to ``path`` (always, also with no rows). Raises ``OSError``."""
    workbook = openpyxl.Workbook()
    errors = workbook.active
    errors.title = "Errors"
    errors.append(list(ERROR_HEADER))
    # By resource id, errors before warnings within a resource.
    for row in sorted(rows, key=lambda r: (r.resource_id, r.severity != "error")):
        errors.append(
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
            ]
        )
    sheet = workbook.create_sheet("Summary")
    sheet.append(["item", "value"])
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
    ):
        sheet.append(list(item))
    workbook.save(path)
