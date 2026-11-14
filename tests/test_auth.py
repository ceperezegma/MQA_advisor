import pytest

from mqa_advisor.auth import (
    DEFAULT_PPE_BASE_URL,
    PROD_BASE_URL,
    REQUEST_TIMEOUT_SECONDS,
    AuthenticationError,
    Config,
    ConfigError,
    PPEAuth,
    PublicSession,
    SessionExpiredError,
)

MOCK_LOGIN_PAGE_HTML = """
<form action="/login?client_id=abc&amp;state=xyz" method="POST" name="cognitoSignInForm">
    <input type="hidden" name="_csrf" value="mock-csrf-token"/>
    <input id="signInFormUsername" name="username" type="text">
    <input id="signInFormPassword" name="password" type="password">
    <input name="signInSubmitButton" type="Submit" value="Sign in">
</form>
"""


class FakeResponse:
    def __init__(self, status_code: int, url: str, text: str = "") -> None:
        self.status_code = status_code
        self.url = url
        self.text = text


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ("PPE_USERNAME", "PPE_PASSWORD", "PPE_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("mqa_advisor.auth.load_dotenv", lambda *a, **k: None)


def test_config_from_env_success(monkeypatch):
    monkeypatch.setenv("PPE_USERNAME", "alice")
    monkeypatch.setenv("PPE_PASSWORD", "secret")

    config = Config.from_env()

    assert config.username == "alice"
    assert config.password == "secret"
    assert config.base_url == DEFAULT_PPE_BASE_URL


def test_config_from_env_custom_base_url(monkeypatch):
    monkeypatch.setenv("PPE_USERNAME", "alice")
    monkeypatch.setenv("PPE_PASSWORD", "secret")
    monkeypatch.setenv("PPE_BASE_URL", "https://example.test")

    config = Config.from_env()

    assert config.base_url == "https://example.test"


@pytest.mark.parametrize(
    "username,password",
    [("", "secret"), ("alice", ""), ("", "")],
)
def test_config_from_env_missing_credentials_raises(monkeypatch, username, password):
    monkeypatch.setenv("PPE_USERNAME", username)
    monkeypatch.setenv("PPE_PASSWORD", password)

    with pytest.raises(ConfigError) as exc_info:
        Config.from_env()

    message = str(exc_info.value)
    assert "secret" not in message
    assert username == "" or username not in message


def test_config_from_env_missing_credentials_message_names_the_key(monkeypatch):
    monkeypatch.setenv("PPE_USERNAME", "")
    monkeypatch.setenv("PPE_PASSWORD", "")

    with pytest.raises(ConfigError, match="PPE_USERNAME"):
        Config.from_env()


def _make_auth() -> PPEAuth:
    config = Config(username="alice", password="secret", base_url=DEFAULT_PPE_BASE_URL)
    return PPEAuth(config)


def test_get_returns_response_when_session_valid(monkeypatch):
    auth = _make_auth()
    ok_response = FakeResponse(
        status_code=200, url=f"{DEFAULT_PPE_BASE_URL}/api/hub/repo/datasets/x.jsonld"
    )
    monkeypatch.setattr(auth._session, "get", lambda *a, **k: ok_response)

    response = auth.get(f"{DEFAULT_PPE_BASE_URL}/api/hub/repo/datasets/x.jsonld")

    assert response is ok_response


def test_get_raises_on_401(monkeypatch):
    auth = _make_auth()
    unauthorized = FakeResponse(
        status_code=401, url=f"{DEFAULT_PPE_BASE_URL}/api/hub/repo/datasets/x.jsonld"
    )
    monkeypatch.setattr(auth._session, "get", lambda *a, **k: unauthorized)

    with pytest.raises(SessionExpiredError):
        auth.get(f"{DEFAULT_PPE_BASE_URL}/api/hub/repo/datasets/x.jsonld")


def test_get_raises_on_redirect_to_login(monkeypatch):
    auth = _make_auth()
    redirected = FakeResponse(status_code=200, url=f"{DEFAULT_PPE_BASE_URL}/login")
    monkeypatch.setattr(auth._session, "get", lambda *a, **k: redirected)

    with pytest.raises(SessionExpiredError):
        auth.get(f"{DEFAULT_PPE_BASE_URL}/api/hub/repo/datasets/x.jsonld")


def test_get_applies_a_default_timeout(monkeypatch):
    auth = _make_auth()
    seen = {}

    def fake_get(url, **kwargs):
        seen.update(kwargs)
        return FakeResponse(200, url)

    monkeypatch.setattr(auth._session, "get", fake_get)
    auth.get(f"{DEFAULT_PPE_BASE_URL}/api/hub/repo/datasets/x.jsonld")

    assert seen["timeout"] == REQUEST_TIMEOUT_SECONDS


def test_get_explicit_timeout_wins(monkeypatch):
    auth = _make_auth()
    seen = {}

    def fake_get(url, **kwargs):
        seen.update(kwargs)
        return FakeResponse(200, url)

    monkeypatch.setattr(auth._session, "get", fake_get)
    auth.get(f"{DEFAULT_PPE_BASE_URL}/api/hub/repo/datasets/x.jsonld", timeout=5)

    assert seen["timeout"] == 5


def test_login_success(monkeypatch):
    auth = _make_auth()
    login_page = FakeResponse(
        status_code=200,
        url=f"{DEFAULT_PPE_BASE_URL}/login?client_id=abc&state=xyz",
        text=MOCK_LOGIN_PAGE_HTML,
    )
    success = FakeResponse(status_code=200, url=DEFAULT_PPE_BASE_URL)
    monkeypatch.setattr(auth._session, "get", lambda *a, **k: login_page)

    captured = {}

    def fake_post(url, data=None, **kwargs):
        captured["url"] = url
        captured["data"] = data
        return success

    monkeypatch.setattr(auth._session, "post", fake_post)

    session = auth.login()

    assert session is auth._session
    assert captured["data"]["_csrf"] == "mock-csrf-token"
    assert captured["data"]["username"] == "alice"
    assert captured["data"]["password"] == "secret"


def test_login_invalid_credentials_raises_without_leaking_credentials(monkeypatch):
    config = Config(
        username="alice@example.com", password="hunter2", base_url=DEFAULT_PPE_BASE_URL
    )
    auth = PPEAuth(config)
    login_page = FakeResponse(
        status_code=200,
        url=f"{DEFAULT_PPE_BASE_URL}/login?client_id=abc&state=xyz",
        text=MOCK_LOGIN_PAGE_HTML,
    )
    rejected = FakeResponse(
        status_code=401, url=f"{DEFAULT_PPE_BASE_URL}/login?client_id=abc&state=xyz"
    )
    monkeypatch.setattr(auth._session, "get", lambda *a, **k: login_page)
    monkeypatch.setattr(auth._session, "post", lambda *a, **k: rejected)

    with pytest.raises(AuthenticationError) as exc_info:
        auth.login()

    message = str(exc_info.value)
    assert "hunter2" not in message
    assert "alice@example.com" not in message


def test_login_unexpected_page_raises(monkeypatch):
    auth = _make_auth()
    unexpected = FakeResponse(
        status_code=200, url=DEFAULT_PPE_BASE_URL, text="<html></html>"
    )
    monkeypatch.setattr(auth._session, "get", lambda *a, **k: unexpected)

    with pytest.raises(AuthenticationError):
        auth.login()


# --- PublicSession (production: public, no login) ---------------------------------


def test_public_session_base_url_is_production():
    assert PublicSession().base_url == PROD_BASE_URL == "https://data.europa.eu/"


def test_public_session_get_is_a_plain_unauthenticated_get(monkeypatch):
    session = PublicSession()
    calls = []
    sentinel = FakeResponse(200, "https://data.europa.eu/x")

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return sentinel

    monkeypatch.setattr(session._session, "get", fake_get)

    assert session.get("https://data.europa.eu/x", timeout=5) is sentinel
    assert calls == [("https://data.europa.eu/x", {"timeout": 5})]


def test_public_session_returns_error_responses_as_is(monkeypatch):
    """No session-expiry semantics: a 401/redirect isn't a stale login here, so the
    response is returned for the caller's own `raise_for_status()`."""
    session = PublicSession()
    unauthorized = FakeResponse(401, "https://data.europa.eu/x")
    monkeypatch.setattr(session._session, "get", lambda *a, **k: unauthorized)

    assert session.get("https://data.europa.eu/x") is unauthorized


def test_public_session_needs_no_credentials(monkeypatch):
    # clean_env (autouse) already removed PPE_USERNAME/PPE_PASSWORD; must not raise.
    session = PublicSession()
    assert session.base_url


def test_public_session_applies_a_default_timeout(monkeypatch):
    session = PublicSession()
    seen = {}

    def fake_get(url, **kwargs):
        seen.update(kwargs)
        return FakeResponse(200, url)

    monkeypatch.setattr(session._session, "get", fake_get)
    session.get("https://data.europa.eu/x")

    assert seen["timeout"] == REQUEST_TIMEOUT_SECONDS


def test_public_session_explicit_timeout_wins(monkeypatch):
    session = PublicSession()
    seen = {}

    def fake_get(url, **kwargs):
        seen.update(kwargs)
        return FakeResponse(200, url)

    monkeypatch.setattr(session._session, "get", fake_get)
    session.get("https://data.europa.eu/x", timeout=3)

    assert seen["timeout"] == 3
