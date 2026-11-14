"""Fetch a catalogue's Datasets and standalone Data Services from PPE or production and
persist them locally.

The environment (`MQA_ENV`, `PPE` or `PROD`) only labels the output folder; the host
fetched from is the session's `base_url` — an authenticated `PPEAuth` for PPE, an
unauthenticated `PublicSession` for production, whose metadata endpoints are public
(ADR-0013).

Input is a per-catalogue Excel manifest at ``catalogues_to_fetch/{catalogue}.xlsx``
with columns ``resource_uri``, ``type``, ``resource_id`` (one resource per row).
``type`` is ``dataset`` or ``dataservice`` (matched case-insensitively).

``dataset`` and ``dataservice`` rows are both fetched; each uses a different
Registry/Search API endpoint and file extension (``build_fetch_url``,
``_FILE_EXTENSION_BY_TYPE`` below) — see ADR-0010 for why standalone Data
Services fetch via the Search/Hub API rather than the Registry API used for
every other resource type.

Output is files on disk, not an in-memory result:
    - ``fetched_catalogues_data/{env}/{catalogue}/dataset_{resource_id}.jsonld`` and
      ``fetched_catalogues_data/{env}/{catalogue}/dataservice_{resource_id}.json`` —
      the raw, unparsed response body for each successfully-fetched resource.
    - ``fetched_catalogues_data/{env}/{catalogue}/report.xlsx`` — one row per manifest
      entry (``resource_uri``, ``type``, ``resource_id``, ``file_name``,
      ``download_result``), where ``download_result`` is ``Success``, ``Failed``,
      or ``Skipped`` (skipped only for a row whose ``type`` isn't fetchable),
      determined by actually checking for each expected file on disk after
      every fetch has been attempted — not by trusting whatever the in-process
      fetch logic believed happened.

Every fetchable resource is attempted and the report is always written. If any of
them failed (any exception of its fetch, not only a download failure),
`run_catalogue` then raises `FetchIncompleteError`, which carries the report path
and the failures, so a caller never goes on with a partial catalogue by accident.
A fully successful run returns the report path.

Resumability: by default (``force=False``), a resource whose expected output
file already exists on disk is skipped entirely — no network call is made
for it, and the report still records it as ``Success`` (ground truth is
disk state, not fetch provenance). Pass ``force=True`` to re-fetch every
resource unconditionally, overwriting whatever's already on disk.

Usage:

    auth = PPEAuth(Config.from_env())
    auth.login()
    ingestor = PPEIngestor(auth, env="PPE")
    report_path = ingestor.run_catalogue("data-gov-lt")  # may raise FetchIncompleteError

    # Production: no login, no credentials.
    ingestor = PPEIngestor(PublicSession(), env="PROD")
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import openpyxl
import requests

from mqa_advisor.auth import PPEAuth, SessionExpiredError
from mqa_advisor.environment import ensure_session_matches_env, resolve_env, session_env
from mqa_advisor.filenames import safe_file_component

DEFAULT_MAX_CONCURRENCY = 10
MAX_FETCH_ATTEMPTS = 4

MANIFESTS_DIR = Path("catalogues_to_fetch")
FETCHED_DATA_DIR = Path("fetched_catalogues_data")

_MANIFEST_COLUMNS = ("resource_uri", "type", "resource_id")
# The columns of `report.xlsx`, in order; `failed_resources` reads them back by name.
REPORT_COLUMNS = ("resource_uri", "type", "resource_id", "file_name", "download_result")
# Single source of truth for which manifest `type`s are fetched: _FETCHABLE_TYPES
# is derived from this dict's keys so the two can never drift apart.
_FILE_EXTENSION_BY_TYPE = {"dataset": "jsonld", "dataservice": "json"}
_FETCHABLE_TYPES = tuple(_FILE_EXTENSION_BY_TYPE)
_ENVELOPE_VALIDATED_TYPES = ("dataservice",)


class ManifestNotFoundError(Exception):
    """Raised when a catalogue's manifest xlsx file doesn't exist, or is malformed."""


class ResourceFetchError(Exception):
    """Raised when a resource's response body couldn't be downloaded after all retries."""

    def __init__(self, resource_type: str, resource_id: str, cause: Exception) -> None:
        self.resource_type = resource_type
        self.resource_id = resource_id
        self.cause = cause
        super().__init__(
            f"Failed to fetch {resource_type} {resource_id} after "
            f"{MAX_FETCH_ATTEMPTS} attempts: {cause}"
        )


@dataclass(frozen=True)
class FetchFailure:
    """One fetchable resource whose fetch raised, and why."""

    resource_type: str
    resource_id: str
    cause: Exception


class FetchIncompleteError(Exception):
    """Raised by `run_catalogue`, after the report is written, when any resource failed.

    If the report itself could not be written (for example `report.xlsx` is open in Excel),
    `report_path` is `None` and `report_error` holds why: the failures are kept either way.
    """

    def __init__(
        self,
        report_path: Path | None,
        failures: Sequence[FetchFailure],
        total: int,
        report_error: OSError | None = None,
    ) -> None:
        self.report_path = report_path
        self.failures = tuple(failures)
        self.total = total
        self.report_error = report_error
        if report_path is None and report_error is not None:
            reason = " ".join(f"{type(report_error).__name__}: {report_error}".split())
            where = f"report not written: {reason}"
        else:
            where = f"report: {report_path}"
        super().__init__(f"{len(failures)} of {total} resources failed ({where})")


class _ResponseEnvelopeError(Exception):
    """Raised when a response body's own status envelope reports failure.

    The Search/Hub API can return HTTP 200 while its JSON body's `status`
    field is not `"success"` (ADR-0010) — an API-level error that
    `response.raise_for_status()` never catches, since HTTP-wise the
    request succeeded.
    """


@dataclass
class ManifestRow:
    resource_uri: str
    type: str
    resource_id: str


def build_fetch_url(base_url: str, resource_type: str, resource_id: str) -> str:
    """Build the full Registry/Search API URL for one resource.

    `dataservice` uses a different API (`/api/hub/search/`, no `.jsonld`
    extension) than `dataset` (`/api/hub/repo/`) — not just a different
    `{type}` segment of one shared template.
    """
    if resource_type == "dataset":
        return f"{base_url}api/hub/repo/datasets/{resource_id}.jsonld"
    if resource_type == "dataservice":
        return f"{base_url}api/hub/search/dataservices/{resource_id}"
    raise ValueError(f"Unsupported resource type: {resource_type!r}")


def load_manifest(
    catalogue: str, manifests_dir: Path = MANIFESTS_DIR
) -> list[ManifestRow]:
    """Read `manifests_dir/{catalogue}.xlsx` into a list of `ManifestRow`."""
    manifest_path = manifests_dir / f"{catalogue}.xlsx"
    if not manifest_path.exists():
        raise ManifestNotFoundError(
            f"No manifest found for catalogue {catalogue!r} — expected {manifest_path}"
        )

    workbook = openpyxl.load_workbook(manifest_path, read_only=True, data_only=True)
    try:
        rows_iter = workbook.active.iter_rows(values_only=True)
        header = next(rows_iter, ())
        column_index = {
            str(name).strip().lower(): i
            for i, name in enumerate(header)
            if name is not None
        }
        missing = [c for c in _MANIFEST_COLUMNS if c not in column_index]
        if missing:
            raise ManifestNotFoundError(
                f"Manifest {manifest_path} is missing required column(s): "
                f"{', '.join(missing)}"
            )

        rows = []
        for values in rows_iter:
            if values is None or all(v is None for v in values):
                continue
            uri = values[column_index["resource_uri"]]
            resource_type = values[column_index["type"]]
            resource_id = values[column_index["resource_id"]]
            rows.append(
                ManifestRow(
                    resource_uri=str(uri) if uri is not None else "",
                    type=str(resource_type).strip().lower()
                    if resource_type is not None
                    else "",
                    resource_id=str(resource_id) if resource_id is not None else "",
                )
            )
        return rows
    finally:
        workbook.close()


def _validate_response_envelope(resource_type: str, content: bytes) -> None:
    """Raise `_ResponseEnvelopeError` if an envelope-validated type's body
    doesn't report `"status": "success"` — see `_ResponseEnvelopeError`."""
    if resource_type not in _ENVELOPE_VALIDATED_TYPES:
        return
    try:
        body = json.loads(content)
    except json.JSONDecodeError as exc:
        raise _ResponseEnvelopeError(f"Response body is not valid JSON: {exc}") from exc
    status = body.get("status") if isinstance(body, dict) else None
    if status != "success":
        raise _ResponseEnvelopeError(
            f"Response envelope status was {status!r}, expected 'success'"
        )


def fetch_and_store(
    auth: PPEAuth,
    base_url: str,
    catalogue_dir: Path,
    resource_type: str,
    resource_id: str,
    force: bool = False,
) -> None:
    """Fetch one resource and write its raw response body to `catalogue_dir`.

    Retries up to `MAX_FETCH_ATTEMPTS` times on any download failure — a
    non-success HTTP status, a transport-level/session error, or (for
    envelope-validated types like `dataservice`) an HTTP-200 response whose
    own `status` field isn't `"success"` — treating every failure the same
    way (no distinction between transient and permanent). Raises
    `ResourceFetchError` only after all attempts fail.

    When `force` is `False` (the default) and the expected output file
    already exists on disk, the fetch is skipped entirely — no network
    call is made and the existing file is left untouched. `force=True`
    re-fetches unconditionally, overwriting any existing file. The
    response body is written to a temporary file and atomically renamed
    into place, so a file at the expected path is never partial — an
    interrupted write (process kill, crash) never leaves behind a file
    that a later `force=False` run would mistake for a completed fetch.
    """
    url = build_fetch_url(base_url, resource_type, resource_id)
    extension = _FILE_EXTENSION_BY_TYPE[resource_type]
    file_path = (
        catalogue_dir
        / f"{resource_type}_{safe_file_component(resource_id)}.{extension}"
    )
    if not force and file_path.exists():
        return

    last_error: Exception | None = None

    for _ in range(MAX_FETCH_ATTEMPTS):
        try:
            response = auth.get(url)
            response.raise_for_status()
            _validate_response_envelope(resource_type, response.content)
        except (
            requests.RequestException,
            SessionExpiredError,
            _ResponseEnvelopeError,
        ) as exc:
            last_error = exc
            continue

        catalogue_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = catalogue_dir / f"{file_path.name}.tmp"
        tmp_path.write_bytes(response.content)
        tmp_path.replace(file_path)
        return

    raise ResourceFetchError(resource_type, resource_id, last_error)


def _expected_file_name(row: ManifestRow) -> str:
    extension = _FILE_EXTENSION_BY_TYPE.get(row.type, "jsonld")
    return f"{row.type}_{safe_file_component(row.resource_id)}.{extension}"


def _download_result(row: ManifestRow, catalogue_dir: Path) -> str:
    if row.type not in _FETCHABLE_TYPES:
        return "Skipped"
    if (catalogue_dir / _expected_file_name(row)).exists():
        return "Success"
    return "Failed"


def write_report(catalogue_dir: Path, rows: list[ManifestRow]) -> Path:
    """Write `report.xlsx`, one row per manifest entry, status from disk ground truth."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(list(REPORT_COLUMNS))

    for row in rows:
        sheet.append(
            [
                row.resource_uri,
                row.type,
                row.resource_id,
                _expected_file_name(row),
                _download_result(row, catalogue_dir),
            ]
        )

    catalogue_dir.mkdir(parents=True, exist_ok=True)
    report_path = catalogue_dir / "report.xlsx"
    workbook.save(report_path)
    return report_path


def failed_resources(report_path: Path) -> list[tuple[str, str]]:
    """The (type, id) of every resource a fetch `report.xlsx` records as `Failed`.

    A resource is `Failed` when its file is absent from the catalogue folder, so these are
    exactly the resources a full database load would prune. Columns are found by name: a
    workbook without them is not a fetch report and raises `ValueError`, rather than being
    read as "nothing failed".
    """
    workbook = openpyxl.load_workbook(report_path, read_only=True, data_only=True)
    try:
        rows = workbook.active.iter_rows(values_only=True)
        header = next(rows, ())
        missing = [name for name in REPORT_COLUMNS if name not in header]
        if missing:
            raise ValueError(
                f"{report_path} is not a fetch report: missing column(s) {', '.join(missing)}"
            )
        type_i, id_i, result_i = (
            header.index(name) for name in ("type", "resource_id", "download_result")
        )
        return [
            (row[type_i], row[id_i])
            for row in rows
            if len(row) > result_i and row[result_i] == "Failed"
        ]
    finally:
        workbook.close()


class PPEIngestor:
    """Owns one session (`PPEAuth` for PPE, `PublicSession` for production) and a shared,
    bounded thread pool for a catalogue run.

    `env`/`output_dir` expose the resolved environment and where its fetched data is
    written (`{output_root}/{env}/{catalogue}/`), so PPE and PROD never overwrite each
    other and readers (`test_run`) use the same directory the ingestor wrote to. `env`
    labels the output folder only — the host fetched from is the session's `base_url` —
    so it defaults from the session kind (`PublicSession` -> `PROD`, `PPEAuth` -> `PPE`)
    and an explicit `env` contradicting the session raises `ConfigError`.

    The concurrency cap is global: every fetchable row's fetch (`dataset` or
    `dataservice`), regardless of manifest position, is submitted to the same
    executor — never a separate pool per call — so `max_concurrency` bounds
    total in-flight requests for the whole run, not per batch.
    """

    def __init__(
        self,
        auth: PPEAuth,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        manifests_dir: Path = MANIFESTS_DIR,
        output_root: Path = FETCHED_DATA_DIR,
        env: str | None = None,
    ) -> None:
        self._auth = auth
        self._manifests_dir = manifests_dir
        # With no explicit env, the session decides (a public session is production,
        # a PPE login is PPE): the folder label can never disagree with the host
        # actually fetched from. An explicit env that contradicts it is an error.
        self.env = resolve_env(env if env is not None else session_env(auth))
        ensure_session_matches_env(auth, self.env)
        self.output_dir = output_root / self.env
        self._executor = ThreadPoolExecutor(max_workers=max_concurrency)

    def run_catalogue(self, catalogue: str, force: bool = False) -> Path:
        """Fetch every `dataset`/`dataservice` row in the manifest, then write the report.

        When `force` is `False` (the default), a row whose expected output
        file already exists on disk is skipped — no network call is made
        for it. `force=True` re-fetches every row unconditionally.

        Raises `FetchIncompleteError` once the report is written if any resource failed.
        """
        rows = load_manifest(catalogue, self._manifests_dir)
        catalogue_dir = self.output_dir / catalogue

        # Remember which resource each future fetches, to attribute a failure to it.
        futures: dict[Future, tuple[str, str]] = {
            self._executor.submit(
                fetch_and_store,
                self._auth,
                self._auth.base_url,
                catalogue_dir,
                row.type,
                row.resource_id,
                force,
            ): (row.type, row.resource_id)
            for row in rows
            if row.type in _FETCHABLE_TYPES
        }

        failures: list[FetchFailure] = []
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:  # one resource must never abort the others
                resource_type, resource_id = futures[future]
                failures.append(FetchFailure(resource_type, resource_id, exc))

        report_path: Path | None = None
        report_error: OSError | None = None
        try:
            report_path = write_report(catalogue_dir, rows)
        except OSError as exc:
            if not failures:
                raise
            report_error = exc  # keep the failures: they matter more than the report

        if failures:
            # Completion order depends on thread timing; sort so the error is deterministic.
            failures.sort(key=lambda f: (f.resource_type, f.resource_id))
            raise FetchIncompleteError(
                report_path, failures, total=len(futures), report_error=report_error
            ) from failures[0].cause
        return report_path

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True)
