"""Prepares the catalogue database: manifest -> fetch -> database load, each step optional.

Three steps run in this fixed order; each one either runs or reuses what an earlier run left
on disk:

    manifest   generate `catalogues_to_fetch/{catalogue}.xlsx` from the environment's SPARQL
               endpoint (`manifest.generate_manifest`, ADR-0014)
    fetch      download every Dataset / Data Service of the manifest into
               `fetched_catalogues_data/{PPE|PROD}/{catalogue}/` (`ingest.PPEIngestor`)
    db         load those files into `fetched_catalogues_data/mqa.sqlite`, creating the tables
               and views on first use (`catalogue_db.load_catalogue`)

A step that is not selected is not run: the next one reads its output from the existing files.
Before anything runs, `run` checks those files exist and stops early with a `PipelineInputError`
if one is missing, so a long fetch is never started only to fail at the load.

One environment (`MQA_ENV`: `PPE` or `PROD`, default `PROD`, see `environment.py`) drives every
step and every folder. Unlike the Test Run of MQA_test, any failure of a selected step is fatal:
the database is this app's product, so it is never "best effort".

Usage:

    uv run python -m mqa_advisor.pipeline {catalogue}
    uv run python -m mqa_advisor.pipeline {catalogue} --steps db
    uv run python -m mqa_advisor.pipeline {catalogue} --steps fetch,db --force
"""

from __future__ import annotations

import argparse
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
    ManifestNotFoundError,
    PPEIngestor,
)
from mqa_advisor.manifest import ManifestGenerationError
from mqa_advisor.manifest import generate_manifest as _generate_manifest

STEPS: tuple[str, ...] = ("manifest", "fetch", "db")


class PipelineInputError(Exception):
    """A selected step needs the output of a step that was not selected, and it is missing."""


@dataclass(frozen=True)
class PipelineOutcome:
    """What a run did; a field is `None` when its step was not selected."""

    env: str
    manifest_path: Path | None = None
    fetch_report_path: Path | None = None
    load_summary: LoadSummary | None = None


def parse_steps(value: str) -> tuple[str, ...]:
    """Parse a comma-separated step list (`"fetch,db"`) into steps in pipeline order."""
    chosen = {part.strip().lower() for part in value.split(",") if part.strip()}
    unknown = chosen - set(STEPS)
    if unknown or not chosen:
        raise ValueError(
            f"steps must be a comma-separated subset of {', '.join(STEPS)}; got {value!r}"
        )
    return tuple(step for step in STEPS if step in chosen)


def _check_inputs(
    catalogue: str,
    steps: Collection[str],
    env: str,
    manifests_dir: Path,
    fetched_data_dir: Path,
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


def run(
    catalogue: str,
    *,
    steps: Collection[str] = STEPS,
    env: str | None = None,
    force: bool = False,
    manifests_dir: Path = MANIFESTS_DIR,
    fetched_data_dir: Path = FETCHED_DATA_DIR,
    db_path: Path = DB_PATH,
    session_factory: Callable[[str], object] = build_session,
    manifest_generator: Callable[..., Path] = _generate_manifest,
    ingestor_factory: Callable[..., PPEIngestor] = PPEIngestor,
    loader: Callable[..., LoadSummary] = load_catalogue,
) -> PipelineOutcome:
    """Run the selected `steps` for `catalogue` (see the module docstring).

    `force` re-fetches every resource instead of keeping the files already on disk. The last
    four parameters replace the real collaborators (tests inject fakes). Raises
    `PipelineInputError` before running anything if an unselected step's output is missing;
    errors of a selected step propagate.
    """
    unknown = set(steps) - set(STEPS)
    if unknown:
        raise ValueError(f"unknown step(s): {', '.join(sorted(unknown))}")
    run_env = resolve_env(env)
    _check_inputs(catalogue, steps, run_env, manifests_dir, fetched_data_dir)

    manifest_path = fetch_report_path = load_summary = None

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

    return PipelineOutcome(run_env, manifest_path, fetch_report_path, load_summary)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m mqa_advisor.pipeline",
        description="Generate the manifest, fetch the resources and load the database.",
    )
    parser.add_argument("catalogue", help="catalogue name, e.g. nsip-lu")
    parser.add_argument(
        "--steps",
        default=",".join(STEPS),
        help="comma-separated steps to run, from: %(default)s (default: all)."
        " The steps not run reuse the existing files.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-fetch every resource instead of keeping the files already on disk",
    )
    args = parser.parse_args(argv)

    try:
        steps = parse_steps(args.steps)
    except ValueError as e:
        parser.error(str(e))

    try:
        outcome = run(args.catalogue, steps=steps, force=args.force)
    except (
        PipelineInputError,
        ManifestGenerationError,
        ManifestNotFoundError,
        ConfigError,
        sqlite3.Error,
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


if __name__ == "__main__":
    main()
