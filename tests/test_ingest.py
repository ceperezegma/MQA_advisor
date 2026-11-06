import threading
import time
from pathlib import Path

import openpyxl
import pytest
import requests

from mqa_advisor.auth import SessionExpiredError
from mqa_advisor.ingest import (
    DEFAULT_MAX_CONCURRENCY,
    MAX_FETCH_ATTEMPTS,
    ManifestNotFoundError,
    ManifestRow,
    PPEIngestor,
    ResourceFetchError,
    _expected_file_name,
    build_fetch_url,
    fetch_and_store,
    load_manifest,
    write_report,
)


class FakeResponse:
    def __init__(self, status_code=200, content=b"{}"):
        self.status_code = status_code
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error")


class FakeAuth:
    """Duck-types the subset of PPEAuth ingest.py relies on: `.base_url`, `.get(url)`."""

    def __init__(self, base_url="https://ppe.data.europa.eu/"):
        self.base_url = base_url
        self.calls: list[str] = []
        self._outcomes: dict[str, list] = {}

    def queue(self, url, *outcomes):
        self._outcomes[url] = list(outcomes)

    def get(self, url, **kwargs):
        self.calls.append(url)
        outcomes = self._outcomes.get(url)
        if not outcomes:
            raise AssertionError(f"FakeAuth: no queued outcome for {url}")
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _write_manifest(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["resource_uri", "type", "resource_id"])
    for row in rows:
        sheet.append(list(row))
    workbook.save(path)


def _make_ingestor(tmp_path, auth, max_concurrency=DEFAULT_MAX_CONCURRENCY, env="PPE"):
    return PPEIngestor(
        auth,
        env=env,
        max_concurrency=max_concurrency,
        manifests_dir=tmp_path / "catalogues_to_fetch",
        output_root=tmp_path / "fetched_catalogues_data",
    )


# --- Manifest loading -------------------------------------------------------


def test_load_manifest_reads_all_rows_in_order(tmp_path):
    manifests_dir = tmp_path / "catalogues_to_fetch"
    _write_manifest(
        manifests_dir / "my-catalogue.xlsx",
        [
            ("https://ppe.data.europa.eu/datasets/a", "Dataset", "a1"),
            ("https://ppe.data.europa.eu/datasets/b", "DATASERVICE", "b1"),
            ("https://ppe.data.europa.eu/datasets/c", "dataset", "c1"),
        ],
    )

    rows = load_manifest("my-catalogue", manifests_dir)

    assert rows == [
        ManifestRow("https://ppe.data.europa.eu/datasets/a", "dataset", "a1"),
        ManifestRow("https://ppe.data.europa.eu/datasets/b", "dataservice", "b1"),
        ManifestRow("https://ppe.data.europa.eu/datasets/c", "dataset", "c1"),
    ]


def test_load_manifest_missing_file_raises(tmp_path):
    manifests_dir = tmp_path / "catalogues_to_fetch"

    with pytest.raises(ManifestNotFoundError, match="does-not-exist"):
        load_manifest("does-not-exist", manifests_dir)


def test_load_manifest_missing_column_raises(tmp_path):
    manifests_dir = tmp_path / "catalogues_to_fetch"
    manifests_dir.mkdir(parents=True)
    workbook = openpyxl.Workbook()
    workbook.active.append(["resource_uri", "type"])  # missing resource_id
    workbook.save(manifests_dir / "broken.xlsx")

    with pytest.raises(ManifestNotFoundError, match="resource_id"):
        load_manifest("broken", manifests_dir)


# --- URL construction --------------------------------------------------------


def test_build_fetch_url_dataset():
    url = build_fetch_url("https://ppe.data.europa.eu/", "dataset", "abc123")
    assert url == "https://ppe.data.europa.eu/api/hub/repo/datasets/abc123.jsonld"


def test_build_fetch_url_dataservice():
    url = build_fetch_url("https://ppe.data.europa.eu/", "dataservice", "xyz789")
    assert url == "https://ppe.data.europa.eu/api/hub/search/dataservices/xyz789"


def test_build_fetch_url_unsupported_type_raises():
    with pytest.raises(ValueError, match="distribution"):
        build_fetch_url("https://ppe.data.europa.eu/", "distribution", "x")


def test_build_fetch_url_dataset_on_production():
    url = build_fetch_url("https://data.europa.eu/", "dataset", "abc123")
    assert url == "https://data.europa.eu/api/hub/repo/datasets/abc123.jsonld"


def test_build_fetch_url_dataservice_on_production():
    url = build_fetch_url("https://data.europa.eu/", "dataservice", "xyz789")
    assert url == "https://data.europa.eu/api/hub/search/dataservices/xyz789"


# --- Expected file naming -----------------------------------------------------


def test_expected_file_name_dataset_uses_jsonld_extension():
    row = ManifestRow("uri", "dataset", "abc")
    assert _expected_file_name(row) == "dataset_abc.jsonld"


def test_expected_file_name_dataservice_uses_json_extension():
    row = ManifestRow("uri", "dataservice", "xyz")
    assert _expected_file_name(row) == "dataservice_xyz.json"


# --- Single resource fetch + retry -------------------------------------------


def test_fetch_and_store_writes_raw_body(tmp_path):
    auth = FakeAuth()
    url = build_fetch_url(auth.base_url, "dataset", "abc")
    auth.queue(url, FakeResponse(200, content=b'{"@id": "abc"}'))
    catalogue_dir = tmp_path / "cat"

    fetch_and_store(auth, auth.base_url, catalogue_dir, "dataset", "abc")

    assert (catalogue_dir / "dataset_abc.jsonld").read_bytes() == b'{"@id": "abc"}'
    assert not (catalogue_dir / "dataset_abc.jsonld.tmp").exists()


_DATASERVICE_SUCCESS_BODY = b'{"status": "success", "result": {"id": "xyz"}}'


def test_fetch_and_store_writes_dataservice_with_json_extension(tmp_path):
    auth = FakeAuth()
    url = build_fetch_url(auth.base_url, "dataservice", "xyz")
    auth.queue(url, FakeResponse(200, content=_DATASERVICE_SUCCESS_BODY))
    catalogue_dir = tmp_path / "cat"

    fetch_and_store(auth, auth.base_url, catalogue_dir, "dataservice", "xyz")

    assert (
        catalogue_dir / "dataservice_xyz.json"
    ).read_bytes() == _DATASERVICE_SUCCESS_BODY
    assert not (catalogue_dir / "dataservice_xyz.jsonld").exists()


def test_fetch_and_store_dataservice_envelope_error_retries_then_fails(tmp_path):
    auth = FakeAuth()
    url = build_fetch_url(auth.base_url, "dataservice", "xyz")
    auth.queue(
        url,
        FakeResponse(200, content=b'{"status": "error", "message": "not found"}'),
        FakeResponse(200, content=b'{"status": "error", "message": "not found"}'),
        FakeResponse(200, content=b'{"status": "error", "message": "not found"}'),
        FakeResponse(200, content=b'{"status": "error", "message": "not found"}'),
    )
    catalogue_dir = tmp_path / "cat"

    with pytest.raises(ResourceFetchError):
        fetch_and_store(auth, auth.base_url, catalogue_dir, "dataservice", "xyz")

    assert len(auth.calls) == 4
    assert not (catalogue_dir / "dataservice_xyz.json").exists()


def test_fetch_and_store_dataservice_malformed_json_retries_then_fails(tmp_path):
    auth = FakeAuth()
    url = build_fetch_url(auth.base_url, "dataservice", "xyz")
    auth.queue(
        url,
        FakeResponse(200, content=b"not json"),
        FakeResponse(200, content=b"not json"),
        FakeResponse(200, content=b"not json"),
        FakeResponse(200, content=b"not json"),
    )
    catalogue_dir = tmp_path / "cat"

    with pytest.raises(ResourceFetchError):
        fetch_and_store(auth, auth.base_url, catalogue_dir, "dataservice", "xyz")

    assert not (catalogue_dir / "dataservice_xyz.json").exists()


def test_fetch_and_store_dataservice_recovers_after_envelope_error(tmp_path):
    auth = FakeAuth()
    url = build_fetch_url(auth.base_url, "dataservice", "xyz")
    auth.queue(
        url,
        FakeResponse(200, content=b'{"status": "error", "message": "transient"}'),
        FakeResponse(200, content=_DATASERVICE_SUCCESS_BODY),
    )
    catalogue_dir = tmp_path / "cat"

    fetch_and_store(auth, auth.base_url, catalogue_dir, "dataservice", "xyz")

    assert (
        catalogue_dir / "dataservice_xyz.json"
    ).read_bytes() == _DATASERVICE_SUCCESS_BODY


def test_fetch_and_store_retries_then_succeeds(tmp_path):
    auth = FakeAuth()
    url = build_fetch_url(auth.base_url, "dataset", "abc")
    auth.queue(url, FakeResponse(500), FakeResponse(200, content=b"ok"))
    catalogue_dir = tmp_path / "cat"

    fetch_and_store(auth, auth.base_url, catalogue_dir, "dataset", "abc")

    assert (catalogue_dir / "dataset_abc.jsonld").read_bytes() == b"ok"
    assert len(auth.calls) == 2


def test_fetch_and_store_retries_on_session_expired(tmp_path):
    auth = FakeAuth()
    url = build_fetch_url(auth.base_url, "dataset", "abc")
    auth.queue(url, SessionExpiredError("stale"), FakeResponse(200, content=b"ok"))
    catalogue_dir = tmp_path / "cat"

    fetch_and_store(auth, auth.base_url, catalogue_dir, "dataset", "abc")

    assert (catalogue_dir / "dataset_abc.jsonld").read_bytes() == b"ok"


def test_fetch_and_store_fails_all_attempts_raises(tmp_path):
    auth = FakeAuth()
    url = build_fetch_url(auth.base_url, "dataset", "abc")
    auth.queue(
        url, FakeResponse(404), FakeResponse(404), FakeResponse(404), FakeResponse(404)
    )
    catalogue_dir = tmp_path / "cat"

    with pytest.raises(ResourceFetchError) as exc_info:
        fetch_and_store(auth, auth.base_url, catalogue_dir, "dataset", "abc")

    assert exc_info.value.resource_type == "dataset"
    assert exc_info.value.resource_id == "abc"
    assert len(auth.calls) == 4
    assert not (catalogue_dir / "dataset_abc.jsonld").exists()


def test_fetch_and_store_unsupported_type_raises_value_error(tmp_path):
    auth = FakeAuth()
    catalogue_dir = tmp_path / "cat"

    with pytest.raises(ValueError, match="distribution"):
        fetch_and_store(auth, auth.base_url, catalogue_dir, "distribution", "x")


def test_fetch_and_store_skips_when_file_exists_and_not_forced(tmp_path):
    auth = FakeAuth()
    catalogue_dir = tmp_path / "cat"
    catalogue_dir.mkdir(parents=True)
    (catalogue_dir / "dataset_abc.jsonld").write_bytes(b"already-there")

    fetch_and_store(auth, auth.base_url, catalogue_dir, "dataset", "abc")

    assert auth.calls == []
    assert (catalogue_dir / "dataset_abc.jsonld").read_bytes() == b"already-there"


def test_fetch_and_store_fetches_when_no_existing_file_regardless_of_force(tmp_path):
    auth = FakeAuth()
    url = build_fetch_url(auth.base_url, "dataset", "abc")
    auth.queue(url, FakeResponse(200, content=b"body"))
    catalogue_dir = tmp_path / "cat"

    fetch_and_store(auth, auth.base_url, catalogue_dir, "dataset", "abc", force=False)

    assert auth.calls == [url]
    assert (catalogue_dir / "dataset_abc.jsonld").read_bytes() == b"body"


def test_fetch_and_store_overwrites_existing_file(tmp_path):
    auth = FakeAuth()
    url = build_fetch_url(auth.base_url, "dataset", "abc")
    catalogue_dir = tmp_path / "cat"
    catalogue_dir.mkdir(parents=True)
    (catalogue_dir / "dataset_abc.jsonld").write_bytes(b"stale")
    auth.queue(url, FakeResponse(200, content=b"fresh"))

    fetch_and_store(auth, auth.base_url, catalogue_dir, "dataset", "abc", force=True)

    assert (catalogue_dir / "dataset_abc.jsonld").read_bytes() == b"fresh"


# --- Report -------------------------------------------------------------------


def test_write_report_reflects_disk_truth_not_in_process_belief(tmp_path):
    catalogue_dir = tmp_path / "cat"
    catalogue_dir.mkdir(parents=True)
    (catalogue_dir / "dataset_present.jsonld").write_bytes(b"{}")
    (catalogue_dir / "dataservice_ds1.json").write_bytes(b"{}")
    rows = [
        ManifestRow("uri-present", "dataset", "present"),
        ManifestRow("uri-missing", "dataset", "missing"),
        ManifestRow("uri-ds", "dataservice", "ds1"),
        ManifestRow("uri-ds-missing", "dataservice", "ds2"),
        ManifestRow("uri-other", "distribution", "d1"),
    ]

    report_path = write_report(catalogue_dir, rows)

    workbook = openpyxl.load_workbook(report_path)
    data_rows = list(workbook.active.iter_rows(min_row=2, values_only=True))
    assert data_rows == [
        ("uri-present", "dataset", "present", "dataset_present.jsonld", "Success"),
        ("uri-missing", "dataset", "missing", "dataset_missing.jsonld", "Failed"),
        ("uri-ds", "dataservice", "ds1", "dataservice_ds1.json", "Success"),
        ("uri-ds-missing", "dataservice", "ds2", "dataservice_ds2.json", "Failed"),
        ("uri-other", "distribution", "d1", "distribution_d1.jsonld", "Skipped"),
    ]


# --- Catalogue run (PPEIngestor) ----------------------------------------------


def test_run_catalogue_fetches_all_dataset_rows(tmp_path):
    auth = FakeAuth()
    manifests_dir = tmp_path / "catalogues_to_fetch"
    rows = [
        ("uri-1", "dataset", "1"),
        ("uri-2", "dataset", "2"),
        ("uri-3", "dataset", "3"),
    ]
    _write_manifest(manifests_dir / "cat.xlsx", rows)
    for _, _, resource_id in rows:
        url = build_fetch_url(auth.base_url, "dataset", resource_id)
        auth.queue(url, FakeResponse(200, content=f"body-{resource_id}".encode()))

    ingestor = _make_ingestor(tmp_path, auth)
    report_path = ingestor.run_catalogue("cat")
    ingestor.shutdown()

    catalogue_dir = tmp_path / "fetched_catalogues_data" / "PPE" / "cat"
    for _, _, resource_id in rows:
        assert (catalogue_dir / f"dataset_{resource_id}.jsonld").read_bytes() == (
            f"body-{resource_id}".encode()
        )
    workbook = openpyxl.load_workbook(report_path)
    results = [r[4] for r in workbook.active.iter_rows(min_row=2, values_only=True)]
    assert results == ["Success", "Success", "Success"]


def test_run_catalogue_fetches_dataservice_rows_too(tmp_path):
    auth = FakeAuth()
    manifests_dir = tmp_path / "catalogues_to_fetch"
    _write_manifest(
        manifests_dir / "cat.xlsx",
        [("uri-1", "dataset", "1"), ("uri-2", "dataservice", "2")],
    )
    dataset_url = build_fetch_url(auth.base_url, "dataset", "1")
    auth.queue(dataset_url, FakeResponse(200, content=b"ok"))
    dataservice_url = build_fetch_url(auth.base_url, "dataservice", "2")
    auth.queue(
        dataservice_url,
        FakeResponse(200, content=b'{"status": "success", "result": {"id": "2"}}'),
    )

    ingestor = _make_ingestor(tmp_path, auth)
    report_path = ingestor.run_catalogue("cat")
    ingestor.shutdown()

    catalogue_dir = tmp_path / "fetched_catalogues_data" / "PPE" / "cat"
    assert (catalogue_dir / "dataset_1.jsonld").read_bytes() == b"ok"
    assert (
        catalogue_dir / "dataservice_2.json"
    ).read_bytes() == b'{"status": "success", "result": {"id": "2"}}'
    assert not (catalogue_dir / "dataservice_2.jsonld").exists()
    assert dataservice_url in auth.calls

    workbook = openpyxl.load_workbook(report_path)
    results = {
        r[2]: r[4] for r in workbook.active.iter_rows(min_row=2, values_only=True)
    }
    assert results == {"1": "Success", "2": "Success"}


def test_run_catalogue_skips_unfetchable_row_types(tmp_path):
    auth = FakeAuth()
    manifests_dir = tmp_path / "catalogues_to_fetch"
    _write_manifest(
        manifests_dir / "cat.xlsx",
        [("uri-1", "dataset", "1"), ("uri-2", "distribution", "2")],
    )
    dataset_url = build_fetch_url(auth.base_url, "dataset", "1")
    auth.queue(dataset_url, FakeResponse(200, content=b"ok"))

    ingestor = _make_ingestor(tmp_path, auth)
    report_path = ingestor.run_catalogue("cat")
    ingestor.shutdown()

    catalogue_dir = tmp_path / "fetched_catalogues_data" / "PPE" / "cat"
    assert (catalogue_dir / "dataset_1.jsonld").exists()
    assert not (catalogue_dir / "distribution_2.jsonld").exists()

    workbook = openpyxl.load_workbook(report_path)
    results = {
        r[2]: r[4] for r in workbook.active.iter_rows(min_row=2, values_only=True)
    }
    assert results == {"1": "Success", "2": "Skipped"}


def test_run_catalogue_isolates_failure_among_successes(tmp_path):
    auth = FakeAuth()
    manifests_dir = tmp_path / "catalogues_to_fetch"
    rows = [
        ("uri-1", "dataset", "1"),
        ("uri-2", "dataset", "2"),
        ("uri-3", "dataset", "3"),
    ]
    _write_manifest(manifests_dir / "cat.xlsx", rows)

    auth.queue(
        build_fetch_url(auth.base_url, "dataset", "1"),
        FakeResponse(200, content=b"ok-1"),
    )
    auth.queue(
        build_fetch_url(auth.base_url, "dataset", "2"),
        FakeResponse(404),
        FakeResponse(404),
        FakeResponse(404),
        FakeResponse(404),
    )
    auth.queue(
        build_fetch_url(auth.base_url, "dataset", "3"),
        FakeResponse(200, content=b"ok-3"),
    )

    ingestor = _make_ingestor(tmp_path, auth)
    report_path = ingestor.run_catalogue("cat")
    ingestor.shutdown()

    catalogue_dir = tmp_path / "fetched_catalogues_data" / "PPE" / "cat"
    assert (catalogue_dir / "dataset_1.jsonld").read_bytes() == b"ok-1"
    assert (catalogue_dir / "dataset_3.jsonld").read_bytes() == b"ok-3"
    assert not (catalogue_dir / "dataset_2.jsonld").exists()

    workbook = openpyxl.load_workbook(report_path)
    results = {
        r[2]: r[4] for r in workbook.active.iter_rows(min_row=2, values_only=True)
    }
    assert results == {"1": "Success", "2": "Failed", "3": "Success"}


def test_run_catalogue_resumes_by_skipping_already_fetched_rows(tmp_path):
    auth = FakeAuth()
    manifests_dir = tmp_path / "catalogues_to_fetch"
    rows = [
        ("uri-1", "dataset", "1"),
        ("uri-2", "dataset", "2"),
        ("uri-3", "dataset", "3"),
    ]
    _write_manifest(manifests_dir / "cat.xlsx", rows)
    catalogue_dir = tmp_path / "fetched_catalogues_data" / "PPE" / "cat"
    catalogue_dir.mkdir(parents=True)
    (catalogue_dir / "dataset_1.jsonld").write_bytes(b"already-fetched")

    for _, _, resource_id in rows[1:]:
        url = build_fetch_url(auth.base_url, "dataset", resource_id)
        auth.queue(url, FakeResponse(200, content=f"body-{resource_id}".encode()))

    ingestor = _make_ingestor(tmp_path, auth)
    report_path = ingestor.run_catalogue("cat", force=False)
    ingestor.shutdown()

    assert len(auth.calls) == 2
    assert (catalogue_dir / "dataset_1.jsonld").read_bytes() == b"already-fetched"
    assert (catalogue_dir / "dataset_2.jsonld").read_bytes() == b"body-2"
    assert (catalogue_dir / "dataset_3.jsonld").read_bytes() == b"body-3"

    workbook = openpyxl.load_workbook(report_path)
    results = {
        r[2]: r[4] for r in workbook.active.iter_rows(min_row=2, values_only=True)
    }
    assert results == {"1": "Success", "2": "Success", "3": "Success"}


def test_run_catalogue_force_refetches_all_rows(tmp_path):
    auth = FakeAuth()
    manifests_dir = tmp_path / "catalogues_to_fetch"
    rows = [("uri-1", "dataset", "1"), ("uri-2", "dataset", "2")]
    _write_manifest(manifests_dir / "cat.xlsx", rows)
    catalogue_dir = tmp_path / "fetched_catalogues_data" / "PPE" / "cat"
    catalogue_dir.mkdir(parents=True)
    (catalogue_dir / "dataset_1.jsonld").write_bytes(b"stale-1")
    (catalogue_dir / "dataset_2.jsonld").write_bytes(b"stale-2")

    for _, _, resource_id in rows:
        url = build_fetch_url(auth.base_url, "dataset", resource_id)
        auth.queue(url, FakeResponse(200, content=f"fresh-{resource_id}".encode()))

    ingestor = _make_ingestor(tmp_path, auth)
    ingestor.run_catalogue("cat", force=True)
    ingestor.shutdown()

    assert len(auth.calls) == 2
    assert (catalogue_dir / "dataset_1.jsonld").read_bytes() == b"fresh-1"
    assert (catalogue_dir / "dataset_2.jsonld").read_bytes() == b"fresh-2"


def test_run_catalogue_report_treats_resumed_skip_same_as_fresh_success(tmp_path):
    auth = FakeAuth()
    manifests_dir = tmp_path / "catalogues_to_fetch"
    rows = [
        ("uri-1", "dataset", "1"),  # already fetched -> skipped
        ("uri-2", "dataset", "2"),  # freshly fetched -> success
        ("uri-3", "dataset", "3"),  # freshly fetched -> fails all retries
    ]
    _write_manifest(manifests_dir / "cat.xlsx", rows)
    catalogue_dir = tmp_path / "fetched_catalogues_data" / "PPE" / "cat"
    catalogue_dir.mkdir(parents=True)
    (catalogue_dir / "dataset_1.jsonld").write_bytes(b"already-fetched")

    auth.queue(
        build_fetch_url(auth.base_url, "dataset", "2"),
        FakeResponse(200, content=b"ok-2"),
    )
    auth.queue(
        build_fetch_url(auth.base_url, "dataset", "3"),
        FakeResponse(404),
        FakeResponse(404),
        FakeResponse(404),
        FakeResponse(404),
    )

    ingestor = _make_ingestor(tmp_path, auth)
    report_path = ingestor.run_catalogue("cat", force=False)
    ingestor.shutdown()

    assert len(auth.calls) == 5  # nothing for row 1, 1 for row 2, 4 for row 3

    workbook = openpyxl.load_workbook(report_path)
    results = {
        r[2]: r[4] for r in workbook.active.iter_rows(min_row=2, values_only=True)
    }
    assert results == {"1": "Success", "2": "Success", "3": "Failed"}


# --- Concurrency ---------------------------------------------------------------


class _ConcurrencyTrackingAuth:
    def __init__(self, base_url="https://ppe.data.europa.eu/", delay=0.05):
        self.base_url = base_url
        self._delay = delay
        self._lock = threading.Lock()
        self._current = 0
        self.max_concurrent = 0
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        with self._lock:
            self.calls.append(url)
            self._current += 1
            self.max_concurrent = max(self.max_concurrent, self._current)
        time.sleep(self._delay)
        with self._lock:
            self._current -= 1
        return FakeResponse(200, content=url.encode())


def test_max_concurrency_respected_and_no_cross_thread_corruption(tmp_path):
    n_resources = 20
    max_concurrency = 4
    delay = 0.05
    auth = _ConcurrencyTrackingAuth(delay=delay)
    manifests_dir = tmp_path / "catalogues_to_fetch"
    rows = [(f"uri-{i}", "dataset", str(i)) for i in range(n_resources)]
    _write_manifest(manifests_dir / "cat.xlsx", rows)

    ingestor = _make_ingestor(tmp_path, auth, max_concurrency=max_concurrency)
    start = time.monotonic()
    ingestor.run_catalogue("cat")
    elapsed = time.monotonic() - start
    ingestor.shutdown()

    assert auth.max_concurrent <= max_concurrency

    serial_time = n_resources * delay
    assert elapsed < serial_time * 0.7  # batched, not fully serialized

    # No corruption/interleaving: each file's content matches its own URL.
    catalogue_dir = tmp_path / "fetched_catalogues_data" / "PPE" / "cat"
    for _, _, resource_id in rows:
        expected_url = build_fetch_url(auth.base_url, "dataset", resource_id)
        assert (catalogue_dir / f"dataset_{resource_id}.jsonld").read_bytes() == (
            expected_url.encode()
        )


# --- Environment scoping (MQA_ENV) ---------------------------------------------


def test_ingestor_output_dir_is_scoped_by_env(tmp_path):
    ingestor = PPEIngestor(
        FakeAuth(), env="PROD", output_root=tmp_path / "fetched_catalogues_data"
    )
    try:
        assert ingestor.env == "PROD"
        assert ingestor.output_dir == tmp_path / "fetched_catalogues_data" / "PROD"
    finally:
        ingestor.shutdown()


def test_ingestor_invalid_env_raises_before_any_network_call(tmp_path):
    from mqa_advisor.auth import ConfigError

    auth = FakeAuth()
    with pytest.raises(ConfigError):
        PPEIngestor(auth, env="staging")
    assert auth.calls == []


def test_ppe_and_prod_runs_write_to_separate_folders(tmp_path):
    manifests_dir = tmp_path / "catalogues_to_fetch"
    _write_manifest(manifests_dir / "cat.xlsx", [("uri-1", "dataset", "1")])

    for env, host in (
        ("PPE", "https://ppe.data.europa.eu/"),
        ("PROD", "https://data.europa.eu/"),
    ):
        auth = FakeAuth(base_url=host)
        auth.queue(
            build_fetch_url(host, "dataset", "1"),
            FakeResponse(200, content=f"body-{env}".encode()),
        )
        ingestor = _make_ingestor(tmp_path, auth, env=env)
        ingestor.run_catalogue("cat")
        ingestor.shutdown()

    for env in ("PPE", "PROD"):
        folder = tmp_path / "fetched_catalogues_data" / env / "cat"
        assert (folder / "dataset_1.jsonld").read_bytes() == f"body-{env}".encode()
        assert (folder / "report.xlsx").exists()


def test_resumability_is_per_environment(tmp_path):
    """A file already fetched for PPE does not make the PROD run skip its own fetch."""
    manifests_dir = tmp_path / "catalogues_to_fetch"
    _write_manifest(manifests_dir / "cat.xlsx", [("uri-1", "dataset", "1")])
    ppe_dir = tmp_path / "fetched_catalogues_data" / "PPE" / "cat"
    ppe_dir.mkdir(parents=True)
    (ppe_dir / "dataset_1.jsonld").write_bytes(b"ppe-body")

    auth = FakeAuth(base_url="https://data.europa.eu/")
    auth.queue(
        build_fetch_url(auth.base_url, "dataset", "1"),
        FakeResponse(200, content=b"prod-body"),
    )
    ingestor = _make_ingestor(tmp_path, auth, env="PROD")
    ingestor.run_catalogue("cat")
    ingestor.shutdown()

    assert auth.calls == [build_fetch_url(auth.base_url, "dataset", "1")]
    prod_file = tmp_path / "fetched_catalogues_data" / "PROD" / "cat"
    assert (prod_file / "dataset_1.jsonld").read_bytes() == b"prod-body"
    assert (ppe_dir / "dataset_1.jsonld").read_bytes() == b"ppe-body"


def test_production_resource_missing_is_failed_without_affecting_other_rows(tmp_path):
    manifests_dir = tmp_path / "catalogues_to_fetch"
    _write_manifest(
        manifests_dir / "cat.xlsx",
        [("uri-ok", "dataset", "ok"), ("uri-gone", "dataset", "gone")],
    )
    auth = FakeAuth(base_url="https://data.europa.eu/")
    auth.queue(
        build_fetch_url(auth.base_url, "dataset", "ok"),
        FakeResponse(200, content=b"ok-body"),
    )
    auth.queue(
        build_fetch_url(auth.base_url, "dataset", "gone"),
        *[FakeResponse(404) for _ in range(MAX_FETCH_ATTEMPTS)],
    )
    ingestor = _make_ingestor(tmp_path, auth, env="PROD")
    report_path = ingestor.run_catalogue("cat")
    ingestor.shutdown()

    workbook = openpyxl.load_workbook(report_path)
    results = {
        r[2]: r[4] for r in workbook.active.iter_rows(min_row=2, values_only=True)
    }
    assert results == {"ok": "Success", "gone": "Failed"}
    folder = tmp_path / "fetched_catalogues_data" / "PROD" / "cat"
    assert (folder / "dataset_ok.jsonld").read_bytes() == b"ok-body"
    assert not (folder / "dataset_gone.jsonld").exists()


def test_ingestor_env_defaults_from_the_session_kind_not_from_mqa_env(
    tmp_path, monkeypatch
):
    from mqa_advisor.auth import Config, PPEAuth, PublicSession

    monkeypatch.setenv("MQA_ENV", "PROD")
    ppe = PPEIngestor(
        PPEAuth(Config("u", "p", "https://ppe.example/")),
        output_root=tmp_path / "fetched_catalogues_data",
    )
    monkeypatch.setenv("MQA_ENV", "PPE")
    prod = PPEIngestor(
        PublicSession(), output_root=tmp_path / "fetched_catalogues_data"
    )
    try:
        assert ppe.env == "PPE"
        assert ppe.output_dir == tmp_path / "fetched_catalogues_data" / "PPE"
        assert prod.env == "PROD"
        assert prod.output_dir == tmp_path / "fetched_catalogues_data" / "PROD"
    finally:
        ppe.shutdown()
        prod.shutdown()


@pytest.mark.parametrize("kind,env", [("public", "PPE"), ("ppe", "PROD")])
def test_ingestor_env_contradicting_its_session_raises(kind, env):
    from mqa_advisor.auth import Config, ConfigError, PPEAuth, PublicSession

    session = (
        PublicSession()
        if kind == "public"
        else PPEAuth(Config("u", "p", "https://ppe.example/"))
    )
    with pytest.raises(ConfigError):
        PPEIngestor(session, env=env)
