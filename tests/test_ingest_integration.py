"""Opt-in integration tests against real PPE.

Excluded from the default `pytest` run (see `[tool.pytest.ini_options]` in
pyproject.toml). Run explicitly with:

    uv run pytest -m integration

Requires PPE_USERNAME/PPE_PASSWORD (real credentials, via .env). The Dataset
test additionally requires PPE_TEST_DATASET_ID; the standalone Data Service
test requires PPE_TEST_DATASERVICE_ID (both are ids known to exist on PPE).
Each test skips gracefully if its required id is missing. Builds a one-row
manifest on the fly in a temp directory so a real run doesn't touch
`catalogues_to_fetch/`/`fetched_catalogues_data/`.
"""

import json
import os

import openpyxl
import pytest

from mqa_advisor.auth import Config, ConfigError, PPEAuth, PublicSession
from mqa_advisor.ingest import PPEIngestor

pytestmark = pytest.mark.integration


def _real_config_or_skip() -> Config:
    try:
        return Config.from_env()
    except ConfigError:
        pytest.skip(
            "PPE_USERNAME/PPE_PASSWORD not set in .env — opt-in integration test"
        )


def _dataset_id_or_skip() -> str:
    dataset_id = os.environ.get("PPE_TEST_DATASET_ID", "").strip()
    if not dataset_id:
        pytest.skip("PPE_TEST_DATASET_ID not set — opt-in integration test")
    return dataset_id


def _dataservice_id_or_skip() -> str:
    dataservice_id = os.environ.get("PPE_TEST_DATASERVICE_ID", "").strip()
    if not dataservice_id:
        pytest.skip("PPE_TEST_DATASERVICE_ID not set — opt-in integration test")
    return dataservice_id


def test_fetch_real_catalogue_manifest_end_to_end(tmp_path):
    config = _real_config_or_skip()
    dataset_id = _dataset_id_or_skip()

    manifests_dir = tmp_path / "catalogues_to_fetch"
    manifests_dir.mkdir()
    workbook = openpyxl.Workbook()
    workbook.active.append(["resource_uri", "type", "resource_id"])
    workbook.active.append(
        [f"{config.base_url}datasets/{dataset_id}", "dataset", dataset_id]
    )
    workbook.save(manifests_dir / "integration-test.xlsx")

    auth = PPEAuth(config)
    auth.login()
    ingestor = PPEIngestor(
        auth,
        env="PPE",
        manifests_dir=manifests_dir,
        output_root=tmp_path / "fetched_catalogues_data",
    )

    report_path = ingestor.run_catalogue("integration-test")
    ingestor.shutdown()

    catalogue_dir = tmp_path / "fetched_catalogues_data" / "PPE" / "integration-test"
    fetched_file = catalogue_dir / f"dataset_{dataset_id}.jsonld"
    assert fetched_file.exists()

    # Manually confirm Distribution data is inline (this design's key assumption,
    # per design.md Risks — not otherwise checked anywhere at ingestion time).
    body = json.loads(fetched_file.read_text(encoding="utf-8"))
    assert "distribution" in json.dumps(body).lower()

    workbook = openpyxl.load_workbook(report_path)
    result_row = next(workbook.active.iter_rows(min_row=2, values_only=True))
    assert result_row[2] == dataset_id
    assert result_row[4] == "Success"


def test_fetch_real_standalone_dataservice_end_to_end(tmp_path):
    config = _real_config_or_skip()
    dataservice_id = _dataservice_id_or_skip()

    manifests_dir = tmp_path / "catalogues_to_fetch"
    manifests_dir.mkdir()
    workbook = openpyxl.Workbook()
    workbook.active.append(["resource_uri", "type", "resource_id"])
    workbook.active.append(
        [
            f"{config.base_url}dataservices/{dataservice_id}",
            "dataservice",
            dataservice_id,
        ]
    )
    workbook.save(manifests_dir / "integration-test-dataservice.xlsx")

    auth = PPEAuth(config)
    auth.login()
    ingestor = PPEIngestor(
        auth,
        env="PPE",
        manifests_dir=manifests_dir,
        output_root=tmp_path / "fetched_catalogues_data",
    )

    report_path = ingestor.run_catalogue("integration-test-dataservice")
    ingestor.shutdown()

    catalogue_dir = (
        tmp_path / "fetched_catalogues_data" / "PPE" / "integration-test-dataservice"
    )
    fetched_file = catalogue_dir / f"dataservice_{dataservice_id}.json"
    assert fetched_file.exists()

    # Confirms the Search/Hub API's response matches ADR-0010's documented
    # {"status": "success", "result": {...}} envelope shape, unlike the
    # Registry API's .jsonld path which 404s for this resource type. A
    # non-"success" status would have made run_catalogue retry and report
    # "Failed" instead (see _validate_response_envelope in ingest.py).
    body = json.loads(fetched_file.read_text(encoding="utf-8"))
    assert body.get("status") == "success"
    assert "result" in body

    workbook = openpyxl.load_workbook(report_path)
    result_row = next(workbook.active.iter_rows(min_row=2, values_only=True))
    assert result_row[2] == dataservice_id
    assert result_row[4] == "Success"


# --- Production (MQA_ENV=PROD): public endpoints, no credentials ---------------------

_PROD_DATASET_ID = (
    "https-opendata-umea-se-api-v2-catalog-datasets-"
    "luftkvalitetsmaetningar-vaestra-esplanaden-fr-o-m-2025"
)
_PROD_DATASERVICE_ID = "https-data-gov-lt-datasets-3499-"


def test_fetch_real_production_dataset_and_dataservice_without_credentials(
    tmp_path, monkeypatch
):
    """Production's metadata endpoints are public: no PPE login or credentials are
    involved."""
    monkeypatch.delenv("PPE_USERNAME", raising=False)
    monkeypatch.delenv("PPE_PASSWORD", raising=False)

    manifests_dir = tmp_path / "catalogues_to_fetch"
    manifests_dir.mkdir()
    workbook = openpyxl.Workbook()
    workbook.active.append(["resource_uri", "type", "resource_id"])
    workbook.active.append(["uri-1", "dataset", _PROD_DATASET_ID])
    workbook.active.append(["uri-2", "dataservice", _PROD_DATASERVICE_ID])
    workbook.save(manifests_dir / "prod-test.xlsx")

    ingestor = PPEIngestor(
        PublicSession(),
        env="PROD",
        manifests_dir=manifests_dir,
        output_root=tmp_path / "fetched_catalogues_data",
    )
    report_path = ingestor.run_catalogue("prod-test")
    ingestor.shutdown()

    catalogue_dir = tmp_path / "fetched_catalogues_data" / "PROD" / "prod-test"
    dataset_file = catalogue_dir / f"dataset_{_PROD_DATASET_ID}.jsonld"
    dataservice_file = catalogue_dir / f"dataservice_{_PROD_DATASERVICE_ID}.json"
    assert dataset_file.exists()
    assert dataservice_file.exists()

    workbook = openpyxl.load_workbook(report_path)
    results = {
        r[2]: r[4] for r in workbook.active.iter_rows(min_row=2, values_only=True)
    }
    assert results == {_PROD_DATASET_ID: "Success", _PROD_DATASERVICE_ID: "Success"}
