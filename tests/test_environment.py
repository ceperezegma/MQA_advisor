import pytest

from mqa_advisor.auth import ConfigError
from mqa_advisor.environment import DEFAULT_ENV, ENV_BASE_URLS, resolve_env


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("MQA_ENV", raising=False)
    monkeypatch.setattr("mqa_advisor.environment.load_dotenv", lambda *a, **k: None)


def test_hosts_per_environment():
    assert ENV_BASE_URLS == {
        "PPE": "https://ppe.data.europa.eu/",
        "PROD": "https://data.europa.eu/",
    }


def test_default_is_prod_when_unset():
    assert DEFAULT_ENV == "PROD"
    assert resolve_env() == "PROD"


def test_blank_env_var_defaults_to_prod(monkeypatch):
    monkeypatch.setenv("MQA_ENV", "  ")
    assert resolve_env() == "PROD"


def test_env_var_is_read_case_insensitively(monkeypatch):
    monkeypatch.setenv("MQA_ENV", "ppe")
    assert resolve_env() == "PPE"


def test_explicit_overrides_env_var_both_directions(monkeypatch):
    monkeypatch.setenv("MQA_ENV", "PROD")
    assert resolve_env("PPE") == "PPE"
    monkeypatch.setenv("MQA_ENV", "PPE")
    assert resolve_env("prod") == "PROD"


@pytest.mark.parametrize("bad", ["staging", "", "PPE2"])
def test_invalid_explicit_value_raises(bad):
    with pytest.raises(ConfigError):
        resolve_env(bad)


def test_invalid_env_var_raises(monkeypatch):
    monkeypatch.setenv("MQA_ENV", "staging")
    with pytest.raises(ConfigError):
        resolve_env()


def test_session_env_by_session_kind():
    from mqa_advisor.auth import Config, PPEAuth, PublicSession
    from mqa_advisor.environment import session_env

    assert session_env(PublicSession()) == "PROD"
    assert session_env(PPEAuth(Config("u", "p", "https://ppe.example/"))) == "PPE"
    assert session_env(object()) is None


@pytest.mark.parametrize(
    "kind,env,ok",
    [
        ("public", "PROD", True),
        ("public", "PPE", False),
        ("ppe", "PPE", True),
        ("ppe", "PROD", False),
    ],
)
def test_ensure_session_matches_env(kind, env, ok):
    from mqa_advisor.auth import Config, PPEAuth, PublicSession
    from mqa_advisor.environment import ensure_session_matches_env

    session = (
        PublicSession()
        if kind == "public"
        else PPEAuth(Config("u", "p", "https://ppe.example/"))
    )
    if ok:
        ensure_session_matches_env(session, env)
    else:
        with pytest.raises(ConfigError):
            ensure_session_matches_env(session, env)


def test_ensure_session_matches_env_ignores_untyped_test_doubles():
    from mqa_advisor.environment import ensure_session_matches_env

    ensure_session_matches_env(object(), "PPE")
    ensure_session_matches_env(object(), "PROD")


def test_build_session_for_prod_is_public_and_needs_no_credentials(monkeypatch):
    from mqa_advisor.auth import PublicSession
    from mqa_advisor.environment import build_session

    monkeypatch.delenv("PPE_USERNAME", raising=False)
    monkeypatch.delenv("PPE_PASSWORD", raising=False)

    assert isinstance(build_session("PROD"), PublicSession)


def test_build_session_for_ppe_logs_in(monkeypatch):
    from mqa_advisor import environment

    events = []

    class FakeAuth:
        def __init__(self, config):
            events.append(("init", config))

        def login(self):
            events.append("login")

    monkeypatch.setattr(environment, "PPEAuth", FakeAuth)
    monkeypatch.setattr(environment.Config, "from_env", classmethod(lambda cls: "cfg"))

    session = environment.build_session("PPE")

    assert isinstance(session, FakeAuth)
    assert events == [("init", "cfg"), "login"]
