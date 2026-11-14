"""Generate a catalogue's manifest (`catalogues_to_fetch/{catalogue}.xlsx`) from EDP's SPARQL
endpoint, so the list of Datasets and Dataservices to fetch never has to be built by hand.

The query (`sparql/catalogue_resources.rq`) lists every Dataset and Dataservice of
`http://data.europa.eu/88u/catalogue/{catalogue}`. `type` and `resource_id` are derived in
Python (they used to be Excel formulas), so the workbook holds plain values and
`ingest.load_manifest` reads it with no spreadsheet recalculation.

Any failure — unreachable endpoint, HTTP error, a response that isn't SPARQL JSON, zero
resources — raises `ManifestGenerationError` with a message naming the catalogue, the
environment, the endpoint and the cause. Nothing is written in that case, and there is no
fallback to an older manifest: the rest of a Test Run cannot work without the resource list.

Usage:

    path = generate_manifest("europol", env="PROD")
"""

from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path

import openpyxl
import requests

from mqa_advisor.auth import SessionExpiredError
from mqa_advisor.environment import build_session, resolve_env
from mqa_advisor.ingest import MANIFESTS_DIR

SPARQL_PATH = "sparql"
SPARQL_RESULTS_MEDIA_TYPE = "application/sparql-results+json"
# Explicit, longer than the default `auth.REQUEST_TIMEOUT_SECONDS`, so the SPARQL query
# keeps its own limit.
SPARQL_TIMEOUT_SECONDS = 60

DCAT_DATASET = "http://www.w3.org/ns/dcat#Dataset"
DCAT_DATA_SERVICE = "http://www.w3.org/ns/dcat#DataService"

MANIFEST_HEADER = ("resource_uri", "type_uri", "type", "resource_id")
MANIFEST_SHEET = "Manifest"

# Catalogue ids look like `nsip-lu`, `data-gov-lt`, `berec_office`. The name is substituted
# into the SPARQL text, so only these characters are allowed.
_CATALOGUE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_SERVICE_PREFIX = "88u/service/"
_DATASET_PREFIX = "88u/dataset/"


class ManifestGenerationError(Exception):
    """Raised when a catalogue's manifest cannot be generated (the message is meant to be
    shown to the user as is)."""


def manifest_type(type_uri: str) -> str:
    """`dataservice`/`dataset` for the two DCAT types, empty for anything else."""
    if type_uri == DCAT_DATA_SERVICE:
        return "dataservice"
    if type_uri == DCAT_DATASET:
        return "dataset"
    return ""


def manifest_resource_id(resource_uri: str) -> str:
    """The whole text after `88u/service/` (else after `88u/dataset/`), `~` suffix
    included; empty when the URI has neither prefix."""
    for prefix in (_SERVICE_PREFIX, _DATASET_PREFIX):
        _, found, rest = resource_uri.partition(prefix)
        if found:
            return rest
    return ""


def _failure(
    catalogue: str, env: str, endpoint: str, cause: str
) -> ManifestGenerationError:
    return ManifestGenerationError(
        f"Could not generate the manifest for {catalogue!r} ({env}): {cause} "
        f"(SPARQL endpoint {endpoint}). Nothing was fetched."
    )


def _load_query(catalogue: str) -> str:
    if not _CATALOGUE_NAME.fullmatch(catalogue):
        raise ManifestGenerationError(
            f"Could not generate the manifest: {catalogue!r} is not a valid catalogue name "
            "(use only letters, digits, '-', '_' and '.', starting with a letter or digit)."
        )
    template = (
        resources.files("mqa_advisor")
        .joinpath("sparql", "catalogue_resources.rq")
        .read_text(encoding="utf-8")
    )
    # Not `str.format`: the SPARQL has braces of its own.
    return template.replace("{catalogue}", catalogue)


def _run_query(
    session, endpoint: str, query: str, catalogue: str, env: str
) -> list[tuple[str, str]]:
    """Run the query and return `(resource_uri, type_uri)` pairs in the endpoint's order."""
    try:
        response = session.get(
            endpoint,
            params={"query": query},
            headers={"Accept": SPARQL_RESULTS_MEDIA_TYPE},
            timeout=SPARQL_TIMEOUT_SECONDS,
        )
    except SessionExpiredError as e:
        raise _failure(
            catalogue, env, endpoint, f"the login session was not accepted ({e})"
        ) from e
    except requests.RequestException as e:
        raise _failure(
            catalogue, env, endpoint, f"the endpoint could not be reached ({e})"
        ) from e

    if not response.ok:
        raise _failure(
            catalogue,
            env,
            endpoint,
            f"the endpoint returned HTTP {response.status_code}",
        )

    try:
        bindings = json.loads(response.content)["results"]["bindings"]
        pairs = [(b["resource"]["value"], b["type"]["value"]) for b in bindings]
    except (ValueError, KeyError, TypeError) as e:
        raise _failure(
            catalogue,
            env,
            endpoint,
            "the response could not be understood (it is not SPARQL JSON results)",
        ) from e

    if not pairs:
        raise _failure(
            catalogue,
            env,
            endpoint,
            f"no resources were found for catalogue {catalogue!r}; check the catalogue "
            "name, or pass --no-generate to use an existing manifest",
        )
    return pairs


def _write_workbook(pairs: list[tuple[str, str]], path: Path) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = MANIFEST_SHEET
    sheet.append(MANIFEST_HEADER)
    for resource_uri, type_uri in pairs:
        sheet.append(
            (
                resource_uri,
                type_uri,
                manifest_type(type_uri),
                manifest_resource_id(resource_uri),
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def generate_manifest(
    catalogue: str,
    *,
    env: str | None = None,
    session=None,
    manifests_dir: Path = MANIFESTS_DIR,
) -> Path:
    """Query the selected environment's SPARQL endpoint for `catalogue`'s resources and
    write `manifests_dir/{catalogue}.xlsx`, overwriting any existing file.

    `env` resolves like everywhere else (`MQA_ENV`, default `PROD`). `session` is the
    session to query with (`PublicSession` for `PROD`, a logged-in `PPEAuth` for `PPE`);
    if omitted, one is built for the environment. Raises `ManifestGenerationError` and
    writes nothing when the query fails or returns no resources.
    """
    query = _load_query(catalogue)
    run_env = resolve_env(env)
    if session is None:
        session = build_session(run_env)
    endpoint = f"{session.base_url}{SPARQL_PATH}"

    pairs = _run_query(session, endpoint, query, catalogue, run_env)

    path = manifests_dir / f"{catalogue}.xlsx"
    try:
        _write_workbook(pairs, path)
    except OSError as e:
        # Typically the manifest is open in Excel (PermissionError on Windows).
        raise ManifestGenerationError(
            f"Could not generate the manifest for {catalogue!r} ({run_env}): {path} could "
            f"not be written ({e}). Close it if it is open in another program."
        ) from e
    return path
