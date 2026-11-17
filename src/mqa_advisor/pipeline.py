"""Prepares the catalogue database and proposes metadata: manifest -> fetch -> db -> propose, each optional.

Four steps run in this fixed order; each one either runs or reuses what an earlier run left
on disk:

    manifest   generate `catalogues_to_fetch/{catalogue}.xlsx` from the environment's SPARQL
               endpoint (`manifest.generate_manifest`, ADR-0014)
    fetch      download every Dataset / Data Service of the manifest into
               `fetched_catalogues_data/{PPE|PROD}/{catalogue}/` (`ingest.PPEIngestor`)
    db         load those files into `fetched_catalogues_data/mqa.sqlite`, creating the tables
               and views on first use (`catalogue_db.load_catalogue`)
    propose    (opt-in: not in the default steps) ask the model for new `dcat:theme` and
               `dcat:keyword` values of every resource of the catalogue in the database and
               store them in `proposed_property_value` (`propose_step.run_propose`, ADR-0017)

A step that is not selected is not run: the next one reads its output from the existing files.
Before anything runs, `run` checks those files exist and stops early with a `PipelineInputError`
if one is missing, so a long fetch is never started only to fail at the load. For `propose` it
also checks that `ANTHROPIC_API_KEY` is set (not on a dry run) and, without the `db` step, that
the catalogue is in the database.

One environment (`MQA_ENV`: `PPE` or `PROD`, default `PROD`, see `environment.py`) drives every
step and every folder. Unlike the Test Run of MQA_test, any failure of a selected step is fatal:
the database is this app's product, so it is never "best effort". In particular an incomplete
fetch (`ingest.FetchIncompleteError`, raised after the report is written) stops the run before the
db step; `--steps db` can still load the partial files, and since the database always mirrors the
folder, that load removes the rows of the resources that failed to download.

The propose step follows the same pattern (ADR-0016, ADR-0019): it attempts every resource, saves every
valid proposal, always writes `propose_report.xlsx` in the catalogue folder, and only then fails
(`ProposeIncompleteError`, exit 1) if any resource had an error. Its options are `--limit N` (the first N
resources by id), `--dry-run` (builds every prompt, calls no model, writes nothing, prints the number of
calls and a rough cost estimate) and `--workers N` (default 4); they are rejected unless `propose` is
selected. A later `db` step reloads the catalogue and deletes its proposals (the load logs how many).

Usage:

    uv run python -m mqa_advisor.pipeline {catalogue}
    uv run python -m mqa_advisor.pipeline {catalogue} --steps db
    uv run python -m mqa_advisor.pipeline {catalogue} --steps fetch,db --force
    uv run python -m mqa_advisor.pipeline {catalogue} --steps propose --limit 20 --dry-run
    uv run python -m mqa_advisor.pipeline {catalogue} --steps db,propose --workers 8
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from pathlib import Path

from mqa_advisor.auth import ConfigError
from mqa_advisor.catalogue_db import DB_PATH, LoadSummary, connect, load_catalogue
from mqa_advisor.environment import build_session, resolve_env
from mqa_advisor.ingest import (
    FETCHED_DATA_DIR,
    MANIFESTS_DIR,
    FetchIncompleteError,
    ManifestNotFoundError,
    PPEIngestor,
    ResourceFetchError,
)
from mqa_advisor.llm import LLMError, LLMInputError, load_api_key
from mqa_advisor.manifest import ManifestGenerationError
from mqa_advisor.manifest import generate_manifest as _generate_manifest
from mqa_advisor.prompts import ACTIVE, PROMPTS_DIR
from mqa_advisor.propose_step import (
    DEFAULT_WORKERS,
    ProposeIncompleteError,
    ProposeOptions,
    ProposeSummary,
    run_propose,
)
from mqa_advisor.vocabulary import VocabularyError, load_vocabulary

STEPS: tuple[str, ...] = ("manifest", "fetch", "db", "propose")
# `propose` is opt-in: it spends money.
DEFAULT_STEPS: tuple[str, ...] = ("manifest", "fetch", "db")
MAX_FAILURES_SHOWN = 10


class PipelineInputError(Exception):
    """A selected step needs the output of a step that was not selected, and it is missing."""


@dataclass(frozen=True)
class PipelineOutcome:
    """What a run did; a field is `None` when its step was not selected."""

    env: str
    manifest_path: Path | None = None
    fetch_report_path: Path | None = None
    load_summary: LoadSummary | None = None
    propose_summary: ProposeSummary | None = None


def parse_steps(value: str) -> tuple[str, ...]:
    """Parse a comma-separated step list (`"fetch,db"`) into steps in pipeline order."""
    chosen = {part.strip().lower() for part in value.split(",") if part.strip()}
    unknown = chosen - set(STEPS)
    if unknown or not chosen:
        raise ValueError(
            f"steps must be a comma-separated subset of {', '.join(STEPS)}; got {value!r}"
        )
    return tuple(step for step in STEPS if step in chosen)


def _catalogue_in_database(db_path: Path, env: str, catalogue: str) -> bool:
    """Whether the database has resources of the catalogue.

    Read-only: it never creates, migrates or otherwise changes the database file, so a missing file or a
    missing table just means "no resources".
    """
    if not db_path.exists():
        return False
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT 1 FROM resource WHERE environment = ? AND catalogue = ? LIMIT 1",
            (env, catalogue),
        ).fetchone()
    except (
        sqlite3.OperationalError
    ):  # no `resource` table: a database that was never loaded
        return False
    finally:
        conn.close()
    return row is not None


def _check_propose_setup() -> None:
    """The things the propose step needs besides the database, checked before any step runs."""
    try:
        load_vocabulary()
    except VocabularyError as e:
        raise PipelineInputError(
            f"the propose step cannot load the theme vocabulary: {e}"
        ) from e
    for name, version in ACTIVE.items():
        prompt_file = PROMPTS_DIR / name / f"v{version}.md"
        if not prompt_file.is_file():
            raise PipelineInputError(
                f"the propose step needs the prompt file {prompt_file}, which does not exist"
            )


def _check_inputs(
    catalogue: str,
    steps: Collection[str],
    env: str,
    manifests_dir: Path,
    fetched_data_dir: Path,
    db_path: Path,
    dry_run: bool,
) -> None:
    if "fetch" in steps and "manifest" not in steps:
        manifest = manifests_dir / f"{catalogue}.xlsx"
        if not manifest.exists():
            raise PipelineInputError(
                f"the fetch step needs the manifest {manifest}, which does not exist;"
                f" add the 'manifest' step"
            )
    if "db" in steps and "fetch" not in steps:
        directory = fetched_data_dir / env / catalogue
        if not directory.is_dir():
            raise PipelineInputError(
                f"the db step needs the fetched files in {directory}, which does not exist;"
                f" add the 'fetch' step"
            )
    if "propose" in steps:
        _check_propose_setup()
        if not dry_run:
            try:
                load_api_key()  # only checks the key is there; a dry run calls no model
            except LLMInputError as e:
                raise PipelineInputError(f"the propose step needs a key: {e}") from e
        if "db" not in steps and not _catalogue_in_database(db_path, env, catalogue):
            raise PipelineInputError(
                f"the propose step needs {env}/{catalogue} in the database {db_path}, which has no"
                f" such resources; add the 'db' step"
            )


def run(
    catalogue: str,
    *,
    steps: Collection[str] = DEFAULT_STEPS,
    env: str | None = None,
    force: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
    workers: int = DEFAULT_WORKERS,
    manifests_dir: Path = MANIFESTS_DIR,
    fetched_data_dir: Path = FETCHED_DATA_DIR,
    db_path: Path = DB_PATH,
    session_factory: Callable[[str], object] = build_session,
    manifest_generator: Callable[..., Path] = _generate_manifest,
    ingestor_factory: Callable[..., PPEIngestor] = PPEIngestor,
    loader: Callable[..., LoadSummary] = load_catalogue,
    proposer: Callable[..., ProposeSummary] = run_propose,
) -> PipelineOutcome:
    """Run the selected `steps` for `catalogue` (see the module docstring).

    `force` re-fetches every resource instead of keeping the files already on disk; `limit`,
    `dry_run` and `workers` belong to the propose step. The last five parameters replace the
    real collaborators (tests inject fakes). Raises `PipelineInputError` before running anything
    if an unselected step's output is missing; errors of a selected step propagate, including
    `FetchIncompleteError` (the db step then does not run, and the ingestor is still shut down)
    and `ProposeIncompleteError` (raised after the proposals are saved and the report written).
    """
    unknown = set(steps) - set(STEPS)
    if unknown:
        raise ValueError(f"unknown step(s): {', '.join(sorted(unknown))}")
    options = ProposeOptions(limit, dry_run, workers)  # validates the three
    run_env = resolve_env(env)
    _check_inputs(
        catalogue, steps, run_env, manifests_dir, fetched_data_dir, db_path, dry_run
    )

    manifest_path = fetch_report_path = load_summary = propose_summary = None

    # One session (and so one PPE login) serves the manifest query and the fetch.
    session = session_factory(run_env) if {"manifest", "fetch"} & set(steps) else None

    if "manifest" in steps:
        manifest_path = manifest_generator(
            catalogue, env=run_env, session=session, manifests_dir=manifests_dir
        )

    if "fetch" in steps:
        ingestor = ingestor_factory(
            session,
            manifests_dir=manifests_dir,
            output_root=fetched_data_dir,
            env=run_env,
        )
        try:
            fetch_report_path = ingestor.run_catalogue(catalogue, force=force)
        finally:
            ingestor.shutdown()

    if "db" in steps:
        conn = connect(db_path)
        try:
            load_summary = loader(
                run_env, catalogue, conn=conn, data_dir=fetched_data_dir
            )
        finally:
            conn.close()

    if "propose" in steps:
        # After a db step the catalogue is in the database only now, so this check cannot be earlier.
        if not _catalogue_in_database(db_path, run_env, catalogue):
            raise PipelineInputError(
                f"the propose step found no resources of {run_env}/{catalogue} in {db_path}"
            )
        conn = connect(db_path)
        try:
            propose_summary = proposer(
                run_env,
                catalogue,
                conn=conn,
                fetched_data_dir=fetched_data_dir,
                options=options,
            )
        finally:
            conn.close()

    return PipelineOutcome(
        run_env, manifest_path, fetch_report_path, load_summary, propose_summary
    )


def _describe_cause(cause: Exception) -> str:
    """One-line text for a failure's cause, for the stderr summary.

    A `ResourceFetchError` is shown by its underlying cause (its own message repeats the
    resource type, id and attempt count already printed next to it).
    """
    if isinstance(cause, ResourceFetchError):
        cause = cause.cause
    message = " ".join(str(cause).split())
    name = type(cause).__name__
    return f"{name}: {message}" if message else name


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m mqa_advisor.pipeline",
        description="Generate the manifest, fetch the resources and load the database.",
    )
    parser.add_argument("catalogue", help="catalogue name, e.g. nsip-lu")
    parser.add_argument(
        "--steps",
        default=",".join(DEFAULT_STEPS),
        help=f"comma-separated steps to run, from: {', '.join(STEPS)} (default: %(default)s;"
        " `propose` is opt-in). The steps not run reuse the existing files.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-fetch every resource instead of keeping the files already on disk",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="propose step: only the first N resources by id",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="propose step: build every prompt, call no model, write nothing, and print the number"
        " of calls and a rough cost estimate",
    )
    parser.add_argument(
        "--workers",
        type=int,
        help=f"propose step: parallel model calls (default {DEFAULT_WORKERS})",
    )
    args = parser.parse_args(argv)

    try:
        steps = parse_steps(args.steps)
    except ValueError as e:
        parser.error(str(e))
    propose_options = {
        "--limit": args.limit is not None,
        "--dry-run": args.dry_run,
        "--workers": args.workers is not None,
    }
    if "propose" not in steps:
        given = [name for name, used in propose_options.items() if used]
        if given:
            parser.error(f"{', '.join(given)} only apply to the 'propose' step")
    try:
        # The one place that validates the numbers (the same class `run` builds).
        options = ProposeOptions(
            args.limit,
            args.dry_run,
            DEFAULT_WORKERS if args.workers is None else args.workers,
        )
    except ValueError as e:
        parser.error(str(e))

    # The console shows the application's own INFO lines (the propose step logs per resource) but not the
    # third-party ones (the HTTP client logs one line per request).
    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("mqa_advisor").setLevel(logging.INFO)

    try:
        outcome = run(
            args.catalogue,
            steps=steps,
            force=args.force,
            limit=options.limit,
            dry_run=options.dry_run,
            workers=options.workers,
        )
    except FetchIncompleteError as e:
        shown = [
            f"{f.resource_type} {f.resource_id} ({_describe_cause(f.cause)})"
            for f in e.failures
        ]
        print(_incomplete_line(e, shown), file=sys.stderr)
        raise SystemExit(1) from e
    except ProposeIncompleteError as e:
        shown = [
            f"{f.resource_type} {f.resource_id} ({f.call}: {' '.join(f.message.split())})"
            for f in e.failures
        ]
        print(
            _describe_proposals(e.summary)
        )  # what the run did, next to the error line
        print(_incomplete_line(e, shown), file=sys.stderr)
        raise SystemExit(1) from e
    except (
        PipelineInputError,
        ManifestGenerationError,
        ManifestNotFoundError,
        ConfigError,
        VocabularyError,
        LLMError,
        sqlite3.Error,
        RuntimeError,  # connect(): a database with an old layout
        ValueError,
        OSError,
    ) as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1) from e

    print(f"Environment: {outcome.env}")
    if outcome.manifest_path:
        print(f"Manifest:    {outcome.manifest_path}")
    if outcome.fetch_report_path:
        print(f"Fetch report: {outcome.fetch_report_path}")
    if outcome.load_summary:
        print(f"Database:    {outcome.load_summary}")
    if outcome.propose_summary:
        print(_describe_proposals(outcome.propose_summary))


def _incomplete_line(error: Exception, failures: Sequence[str]) -> str:
    """The one stderr line of a fetch or propose step that finished with failures (at most 10 are listed)."""
    shown = ", ".join(failures[:MAX_FAILURES_SHOWN])
    hidden = len(failures) - MAX_FAILURES_SHOWN
    if hidden > 0:
        shown += f", ... and {hidden} more (see the report)"
    return f"{type(error).__name__}: {error}: {shown}"


def _describe_proposals(summary: ProposeSummary) -> str:
    """The lines printed after a propose step (a dry run prints its rough estimate instead)."""
    if summary.dry_run:
        return (
            f"Dry run:     {summary.resources} resource(s), {summary.calls} call(s); ROUGH estimate"
            f" (text length / 4): {summary.input_tokens} input and {summary.output_tokens} output"
            f" tokens, about USD {summary.cost_usd:.4f}. Nothing was written."
        )
    text = (
        f"Proposals:   {summary.themes} theme(s) and {summary.keywords} keyword(s) for"
        f" {summary.stored} of {summary.resources} resource(s), {summary.calls} call(s),"
        f" estimated cost USD {summary.cost_usd:.4f}, {summary.errors} error(s),"
        f" {summary.warnings} warning(s)"
    )
    if summary.report_path:
        text += f"\nPropose report: {summary.report_path}"
    return text


if __name__ == "__main__":
    main()
