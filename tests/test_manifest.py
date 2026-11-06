import json

import pytest
import requests

from mqa_advisor.auth import SessionExpiredError
from mqa_advisor.ingest import load_manifest
from mqa_advisor.manifest import (
    DCAT_DATA_SERVICE,
    DCAT_DATASET,
    ManifestGenerationError,
    _load_query,
    generate_manifest,
    manifest_resource_id,
    manifest_type,
)

DATASET_URI = "http://data.europa.eu/88u/dataset/annual-report-2004"
SERVICE_URI = "http://data.europa.eu/88u/service/my-service"


def _sparql_json(*pairs):
    return json.dumps(
        {
            "head": {"vars": ["resource", "type"]},
            "results": {
                "bindings": [
                    {
                        "resource": {"type": "uri", "value": resource},
                        "type": {"type": "uri", "value": type_uri},
                    }
                    for resource, type_uri in pairs
                ]
            },
        }
    ).encode()


class _Response:
    def __init__(self, content=b"", status_code=200):
        self.content = content
        self.status_code = status_code

    @property
    def ok(self):
        return self.status_code < 400


class _FakeSession:
    def __init__(self, base_url="https://data.europa.eu/", response=None, error=None):
        self.base_url = base_url
        self._response = response
        self._error = error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self._error is not None:
            raise self._error
        return self._response


def _session_with(*pairs, **kwargs):
    return _FakeSession(response=_Response(_sparql_json(*pairs)), **kwargs)


# --- the two former Excel formulas -----------------------------------------------------


def test_manifest_type_maps_the_two_dcat_types():
    assert manifest_type(DCAT_DATASET) == "dataset"
    assert manifest_type(DCAT_DATA_SERVICE) == "dataservice"


def test_manifest_type_is_empty_for_anything_else():
    assert manifest_type("http://www.w3.org/ns/dcat#Catalog") == ""
    assert manifest_type("") == ""


def test_resource_id_of_a_dataset_uri():
    assert manifest_resource_id(DATASET_URI) == "annual-report-2004"


def test_resource_id_of_a_service_uri():
    assert manifest_resource_id(SERVICE_URI) == "my-service"


def test_resource_id_keeps_the_tilde_suffix():
    uri = "http://data.europa.eu/88u/dataset/https-data-gov-lt-datasets-2539-~~1"
    assert manifest_resource_id(uri) == "https-data-gov-lt-datasets-2539-~~1"


def test_resource_id_keeps_tildes_in_the_middle():
    uri = "http://data.europa.eu/88u/dataset/some-id~~abc~def"
    assert manifest_resource_id(uri) == "some-id~~abc~def"


def test_resource_id_is_empty_without_a_known_prefix():
    assert manifest_resource_id("http://example.org/other/thing") == ""


# --- the packaged query ----------------------------------------------------------------


def test_query_has_the_catalogue_substituted_and_its_own_braces_intact():
    query = _load_query("nsip-lu")
    assert "<http://data.europa.eu/88u/catalogue/nsip-lu>" in query
    assert "{catalogue}" not in query
    assert "WHERE {" in query


@pytest.mark.parametrize(
    "name", ["", "-x", "a b", "a>b", "x} DROP {", "a/b", "../x", "a\nb"]
)
def test_invalid_catalogue_names_are_rejected_before_any_request(name, tmp_path):
    session = _session_with((DATASET_URI, DCAT_DATASET))
    with pytest.raises(ManifestGenerationError, match="not a valid catalogue name"):
        generate_manifest(name, env="PROD", session=session, manifests_dir=tmp_path)
    assert session.calls == []


@pytest.mark.parametrize("name", ["europol", "nsip-lu", "data-gov-lt", "berec_office"])
def test_real_catalogue_names_are_accepted(name):
    assert f"catalogue/{name}>" in _load_query(name)


# --- querying the endpoint -------------------------------------------------------------


def test_queries_the_sparql_endpoint_of_the_sessions_host(tmp_path):
    for env, base in (
        ("PROD", "https://data.europa.eu/"),
        ("PPE", "https://ppe.data.europa.eu/"),
    ):
        session = _session_with((DATASET_URI, DCAT_DATASET), base_url=base)

        generate_manifest("europol", env=env, session=session, manifests_dir=tmp_path)

        ((url, kwargs),) = session.calls
        assert url == f"{base}sparql"
        assert kwargs["headers"] == {"Accept": "application/sparql-results+json"}
        assert "catalogue/europol>" in kwargs["params"]["query"]


# --- writing the manifest --------------------------------------------------------------


def test_workbook_round_trips_through_load_manifest_in_query_order(tmp_path):
    session = _session_with(
        (DATASET_URI, DCAT_DATASET),
        ("http://data.europa.eu/88u/dataset/b~x", DCAT_DATASET),
        (SERVICE_URI, DCAT_DATA_SERVICE),
    )

    path = generate_manifest(
        "europol", env="PROD", session=session, manifests_dir=tmp_path
    )

    assert path == tmp_path / "europol.xlsx"
    rows = load_manifest("europol", tmp_path)
    assert [(r.type, r.resource_id) for r in rows] == [
        ("dataset", "annual-report-2004"),
        ("dataset", "b~x"),
        ("dataservice", "my-service"),
    ]
    assert rows[0].resource_uri == DATASET_URI


def test_workbook_holds_plain_values_and_all_four_columns(tmp_path):
    import openpyxl

    session = _session_with((DATASET_URI, DCAT_DATASET))
    generate_manifest("europol", env="PROD", session=session, manifests_dir=tmp_path)

    sheet = openpyxl.load_workbook(tmp_path / "europol.xlsx").active
    assert [c.value for c in sheet[1]] == [
        "resource_uri",
        "type_uri",
        "type",
        "resource_id",
    ]
    assert [c.value for c in sheet[2]] == [
        DATASET_URI,
        DCAT_DATASET,
        "dataset",
        "annual-report-2004",
    ]
    assert not any(
        str(c.value).startswith("=") for row in sheet.iter_rows() for c in row
    )


def test_an_existing_manifest_is_overwritten(tmp_path):
    (tmp_path / "europol.xlsx").write_bytes(b"old")
    session = _session_with((DATASET_URI, DCAT_DATASET))

    generate_manifest("europol", env="PROD", session=session, manifests_dir=tmp_path)

    assert [r.resource_id for r in load_manifest("europol", tmp_path)] == [
        "annual-report-2004"
    ]


def test_the_manifests_folder_is_created_when_missing(tmp_path):
    session = _session_with((DATASET_URI, DCAT_DATASET))
    folder = tmp_path / "new" / "folder"

    generate_manifest("europol", env="PROD", session=session, manifests_dir=folder)

    assert (folder / "europol.xlsx").exists()


def test_env_defaults_from_mqa_env_and_the_session_is_built_when_omitted(
    tmp_path, monkeypatch
):
    session = _session_with((DATASET_URI, DCAT_DATASET))
    built = []
    monkeypatch.setattr("mqa_advisor.environment.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("MQA_ENV", "ppe")
    monkeypatch.setattr(
        "mqa_advisor.manifest.build_session", lambda env: built.append(env) or session
    )

    generate_manifest("europol", manifests_dir=tmp_path)

    assert built == ["PPE"]


# --- failures --------------------------------------------------------------------------


def _failing_cases():
    return [
        (
            _FakeSession(response=_Response(status_code=503)),
            "HTTP 503",
        ),
        (
            _FakeSession(error=requests.ConnectionError("boom")),
            "could not be reached",
        ),
        (
            _FakeSession(error=SessionExpiredError("expired")),
            "login session was not accepted",
        ),
        (
            _FakeSession(response=_Response(b"<html>login</html>")),
            "could not be understood",
        ),
        (
            _FakeSession(response=_Response(b'{"results": {}}')),
            "could not be understood",
        ),
        (
            _FakeSession(response=_Response(_sparql_json())),
            "no resources were found",
        ),
    ]


@pytest.mark.parametrize("session, cause", _failing_cases())
def test_failures_name_catalogue_environment_endpoint_and_cause(
    session, cause, tmp_path
):
    with pytest.raises(ManifestGenerationError) as excinfo:
        generate_manifest("europol", env="PPE", session=session, manifests_dir=tmp_path)

    message = str(excinfo.value)
    assert "'europol'" in message
    assert "(PPE)" in message
    assert "https://data.europa.eu/sparql" in message
    assert cause in message


def test_zero_resources_suggests_checking_the_name_and_no_generate(tmp_path):
    session = _FakeSession(response=_Response(_sparql_json()))

    with pytest.raises(ManifestGenerationError) as excinfo:
        generate_manifest("typo", env="PROD", session=session, manifests_dir=tmp_path)

    assert "check the catalogue name" in str(excinfo.value)
    assert "--no-generate" in str(excinfo.value)


@pytest.mark.parametrize("session, cause", _failing_cases())
def test_a_failure_leaves_an_existing_manifest_unchanged_and_writes_nothing_else(
    session, cause, tmp_path
):
    existing = tmp_path / "europol.xlsx"
    existing.write_bytes(b"previous manifest")

    with pytest.raises(ManifestGenerationError):
        generate_manifest(
            "europol", env="PROD", session=session, manifests_dir=tmp_path
        )

    assert existing.read_bytes() == b"previous manifest"
    assert [p.name for p in tmp_path.iterdir()] == ["europol.xlsx"]


@pytest.mark.parametrize("session, cause", _failing_cases())
def test_a_failure_with_no_manifest_creates_no_file(session, cause, tmp_path):
    with pytest.raises(ManifestGenerationError):
        generate_manifest(
            "europol", env="PROD", session=session, manifests_dir=tmp_path
        )

    assert list(tmp_path.iterdir()) == []


def test_the_request_has_a_timeout(tmp_path):
    session = _session_with((DATASET_URI, DCAT_DATASET))

    generate_manifest("europol", env="PROD", session=session, manifests_dir=tmp_path)

    assert session.calls[0][1]["timeout"] > 0


def test_a_write_failure_is_a_clean_manifest_generation_error(tmp_path, monkeypatch):
    def _locked(self, path):
        raise PermissionError("[Errno 13] Permission denied")

    monkeypatch.setattr("openpyxl.Workbook.save", _locked)
    session = _session_with((DATASET_URI, DCAT_DATASET))

    with pytest.raises(ManifestGenerationError) as excinfo:
        generate_manifest(
            "europol", env="PROD", session=session, manifests_dir=tmp_path
        )

    assert "could not be written" in str(excinfo.value)
    assert "'europol'" in str(excinfo.value)
