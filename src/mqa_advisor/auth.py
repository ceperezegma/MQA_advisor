"""Authenticate against PPE and reuse the resulting session for Registry API calls.

PPE's login is a Cognito Hosted UI form POST behind a CloudFront edge-authorizer
(PKCE handled server-side via cookies, not by this client) — see ADR-0007 for how
the flow was confirmed.

Usage:

    auth = PPEAuth(Config.from_env())
    auth.login()
    response = auth.get("https://ppe.data.europa.eu/api/hub/repo/datasets/{id}.jsonld")
    # response raises SessionExpiredError on subsequent calls if the session goes stale;
    # re-authenticate with auth.login() and retry.

Production's metadata endpoints are public, so `MQA_ENV=PROD` uses `PublicSession` instead:
the same `get`/`base_url` interface with no login and no credentials (ADR-0013).
"""

from __future__ import annotations

import os
import re
from html import unescape
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv

DEFAULT_PPE_BASE_URL = "https://ppe.data.europa.eu/"
PROD_BASE_URL = "https://data.europa.eu/"
# Default timeout of every `get` of `PPEAuth` and `PublicSession` (a caller's `timeout=` wins).
REQUEST_TIMEOUT_SECONDS = 30

# Cognito Hosted UI login form — see ADR-0007 for how this was confirmed.
_LOGIN_FORM_ACTION_RE = re.compile(
    r'<form[^>]*action="([^"]+)"[^>]*name="cognitoSignInForm"', re.DOTALL
)
_LOGIN_CSRF_INPUT_RE = re.compile(r'name="_csrf"\s+value="([^"]+)"')


class ConfigError(Exception):
    """Raised when required configuration is missing or malformed."""


class AuthenticationError(Exception):
    """Raised when the PPE login flow itself fails (bad credentials, unexpected page)."""


class SessionExpiredError(Exception):
    """Raised when a previously-authenticated PPE session is invalid or has expired."""


class Config:
    """PPE credentials and base URL, loaded from the environment."""

    def __init__(self, username: str, password: str, base_url: str) -> None:
        self.username = username
        self.password = password
        self.base_url = base_url

    @classmethod
    def from_env(cls) -> Config:
        load_dotenv()

        username = os.environ.get("PPE_USERNAME", "").strip()
        password = os.environ.get("PPE_PASSWORD", "").strip()
        base_url = os.environ.get("PPE_BASE_URL", "").strip() or DEFAULT_PPE_BASE_URL

        missing = [
            name
            for name, value in (("PPE_USERNAME", username), ("PPE_PASSWORD", password))
            if not value
        ]
        if missing:
            raise ConfigError(
                f"Missing required configuration: {', '.join(missing)}. "
                "Set them in .env (see .env.example)."
            )

        return cls(username=username, password=password, base_url=base_url)


class PPEAuth:
    """Owns an authenticated `requests.Session` against PPE."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._session = requests.Session()

    @property
    def base_url(self) -> str:
        return self._config.base_url

    def login(self) -> requests.Session:
        login_page = self._session.get(self._config.base_url)
        if "/login" not in login_page.url:
            raise AuthenticationError(
                "Expected to be redirected to the PPE/Cognito login page, but landed on "
                f"a different URL (status={login_page.status_code})"
            )

        action_match = _LOGIN_FORM_ACTION_RE.search(login_page.text)
        csrf_match = _LOGIN_CSRF_INPUT_RE.search(login_page.text)
        if not action_match or not csrf_match:
            raise AuthenticationError(
                "Could not locate the PPE/Cognito login form on the login page — "
                "the login page markup may have changed (see ADR-0007)"
            )

        post_url = urljoin(login_page.url, unescape(action_match.group(1)))
        payload = {
            "_csrf": csrf_match.group(1),
            "username": self._config.username,
            "password": self._config.password,
            "signInSubmitButton": "Sign in",
        }

        response = self._session.post(post_url, data=payload)
        if "/login" in response.url:
            raise AuthenticationError(
                f"PPE login failed (status={response.status_code}) — "
                "check PPE_USERNAME/PPE_PASSWORD"
            )

        return self._session

    def get(self, url: str, **kwargs) -> requests.Response:
        # `requests` has no timeout: a stalled connection would block a fetch worker forever.
        kwargs.setdefault("timeout", REQUEST_TIMEOUT_SECONDS)
        response = self._session.get(url, allow_redirects=True, **kwargs)
        if self._is_expired(response):
            raise SessionExpiredError(
                f"PPE session is invalid or expired (status={response.status_code}, "
                f"url={response.url})"
            )
        return response

    def _is_expired(self, response: requests.Response) -> bool:
        if response.status_code == 401:
            return True
        return (
            response.url.startswith(self._config.base_url)
            and "login" in response.url.lower()
        )


class PublicSession:
    """Unauthenticated session for production, whose metadata endpoints are public.

    Offers the same two members `ingest.PPEIngestor` uses from `PPEAuth` — `get(url)` and
    `base_url` — so the ingestor takes either unchanged. Performs no login and never reads
    `PPE_USERNAME`/`PPE_PASSWORD`. Every request gets a default timeout
    (`REQUEST_TIMEOUT_SECONDS`, overridable per call): `requests` has none, so a
    stalled connection would otherwise block an ingest worker thread forever.
    """

    def __init__(self, base_url: str = PROD_BASE_URL) -> None:
        self.base_url = base_url
        self._session = requests.Session()

    def get(self, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", REQUEST_TIMEOUT_SECONDS)
        return self._session.get(url, **kwargs)
