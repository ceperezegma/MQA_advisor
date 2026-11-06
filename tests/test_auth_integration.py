"""Opt-in integration tests against real PPE.

Excluded from the default `pytest` run (see `[tool.pytest.ini_options]` in
pyproject.toml). Run explicitly with:

    uv run pytest -m integration

Requires PPE_USERNAME/PPE_PASSWORD (real credentials, via .env) and
PPE_TEST_DATASET_ID (an id known to exist on PPE, e.g. from a dataset's
Registry API URL: /api/hub/repo/datasets/{id}.jsonld). Skips gracefully if
either is missing.
"""

import os

import pytest

from mqa_advisor.auth import Config, ConfigError, PPEAuth

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


def test_login_and_fetch_real_dataset():
    config = _real_config_or_skip()
    dataset_id = _dataset_id_or_skip()

    auth = PPEAuth(config)
    auth.login()
    response = auth.get(f"{config.base_url}/api/hub/repo/datasets/{dataset_id}.jsonld")

    assert response.status_code == 200
    assert response.json()


def test_session_reused_across_multiple_fetches():
    config = _real_config_or_skip()
    dataset_id = _dataset_id_or_skip()

    auth = PPEAuth(config)

    login_post_calls = {"count": 0}
    original_post = auth._session.post

    def counting_post(*args, **kwargs):
        login_post_calls["count"] += 1
        return original_post(*args, **kwargs)

    auth._session.post = counting_post

    auth.login()
    for _ in range(3):
        response = auth.get(
            f"{config.base_url}/api/hub/repo/datasets/{dataset_id}.jsonld"
        )
        assert response.status_code == 200

    assert login_post_calls["count"] == 1
