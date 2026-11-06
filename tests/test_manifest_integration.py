"""Opt-in integration tests for manifest generation against the real SPARQL endpoints.

Excluded from the default `pytest` run (see `[tool.pytest.ini_options]` in
pyproject.toml). Run explicitly with:

    uv run pytest -m integration

The production test needs only network access. The PPE test needs PPE_USERNAME/PPE_PASSWORD
(real credentials, via .env) and skips without them. Both write into a temp folder, never
`catalogues_to_fetch/`, and compare against `fixtures/manifest_generation/europol_manifest.json`,
a snapshot of the hand-made `europol` manifest. A catalogue can gain resources over time, so
the check is that every snapshot resource is still returned with the same type and id.
"""

import json
from pathlib import Path

import pytest

from mqa_advisor.auth import ConfigError
from mqa_advisor.environment import build_session
from mqa_advisor.ingest import load_manifest
from mqa_advisor.manifest import generate_manifest

pytestmark = pytest.mark.integration

EXPECTED = json.loads(
    (
        Path(__file__).parent
        / "fixtures"
        / "manifest_generation"
        / "europol_manifest.json"
    ).read_text(encoding="utf-8")
)


def _assert_generated_manifest_covers_the_snapshot(env, tmp_path):
    try:
        session = build_session(env)
    except ConfigError:
        pytest.skip(
            "PPE_USERNAME/PPE_PASSWORD not set in .env — opt-in integration test"
        )

    generate_manifest("europol", env=env, session=session, manifests_dir=tmp_path)

    generated = {
        r.resource_uri: (r.type, r.resource_id)
        for r in load_manifest("europol", tmp_path)
    }
    for expected in EXPECTED:
        assert generated.get(expected["resource_uri"]) == (
            expected["type"],
            expected["resource_id"],
        )


def test_production_manifest_covers_the_europol_snapshot(tmp_path):
    _assert_generated_manifest_covers_the_snapshot("PROD", tmp_path)


def test_ppe_manifest_covers_the_europol_snapshot(tmp_path):
    _assert_generated_manifest_covers_the_snapshot("PPE", tmp_path)
