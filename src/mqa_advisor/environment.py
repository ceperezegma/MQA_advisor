"""The MQA environment (`PPE` or `PROD`) a Test Run targets — one value shared by metadata
ingestion, the MQA cache (Reference Result) fetch and every output folder, so the two data
sources can never come from different environments.

Selected with `MQA_ENV` in `.env` (default `PROD`), or an explicit `env=` argument, which
always wins. See ADR-0012 (MQA cache switch) and ADR-0013 (ingestion follows the same
selector).
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

from mqa_advisor.auth import (
    DEFAULT_PPE_BASE_URL,
    PROD_BASE_URL,
    Config,
    ConfigError,
    PPEAuth,
    PublicSession,
)

# Host per environment for the public MQA cache. Deliberately independent of `PPE_BASE_URL`,
# which keeps driving PPE login and PPE metadata ingestion (ADR-0011).
ENV_BASE_URLS = {
    "PPE": DEFAULT_PPE_BASE_URL,
    "PROD": PROD_BASE_URL,
}
DEFAULT_ENV = "PROD"


def resolve_env(explicit: str | None = None) -> str:
    """Resolve the MQA environment (`"PPE"` or `"PROD"`): an explicit value always wins;
    otherwise read `MQA_ENV` from the environment (via `.env`), defaulting to `"PROD"`.
    Case-insensitive. Raises `ConfigError` for any other value."""
    if explicit is not None:
        env = explicit
    else:
        load_dotenv()
        env = os.environ.get("MQA_ENV", "").strip() or DEFAULT_ENV

    env = env.strip().upper()
    if env not in ENV_BASE_URLS:
        raise ConfigError(
            f"MQA_ENV/env must be one of {tuple(ENV_BASE_URLS)!r}, got {env!r}"
        )
    return env


def session_env(session: object) -> str | None:
    """The environment a session belongs to: `"PROD"` for a `PublicSession`, `"PPE"` for a
    `PPEAuth`, `None` for anything else (e.g. a duck-typed test double)."""
    if isinstance(session, PublicSession):
        return "PROD"
    if isinstance(session, PPEAuth):
        return "PPE"
    return None


def ensure_session_matches_env(session: object, env: str) -> None:
    """Raise `ConfigError` if `session` belongs to a different environment than `env`, so
    data fetched from one environment is never stored under (or compared as) the other."""
    kind = session_env(session)
    if kind is not None and kind != env:
        raise ConfigError(
            f"A {type(session).__name__} fetches from {kind}, but the environment is "
            f"{env!r}; pass a matching session or omit env."
        )


def build_session(env: str) -> PPEAuth | PublicSession:
    """The session for `env` (already resolved): a `PublicSession` for `PROD` (no login, no
    credentials), a logged-in `PPEAuth` for `PPE`."""
    if env == "PROD":
        return PublicSession()
    auth = PPEAuth(Config.from_env())
    auth.login()
    return auth
