"""TK-71 acceptance criteria — Google Calendar OAuth2 credential lifecycle (Q-57).

CI tests are mocked, ZERO network (Q-57(c)):
  AC1 (readonly-scope-only): ``test_gcal_scopes_constant_is_readonly_only``,
      ``test_fresh_host_runs_oauth_flow_with_readonly_scope_only``,
      ``test_scope_guard_rejects_write_scoped_token``.
  AC2 (non-interactive refresh): ``test_expired_token_refreshes_without_prompting``.
  AC3 (encrypted at rest / no plaintext / no log leakage):
      ``test_keyring_store_never_writes_a_plaintext_file_and_never_logs_the_secret``.

Plus one construction-validation test for the Q-57(b) AC3-at-construction precedent, and
exactly ONE live smoke test gated on ``WOMBAT_TEST_GCAL_LIVE=1`` (Q-57(c), the
``WOMBAT_TEST_PG_DSN`` skip idiom from ``tests/unit/test_daily_ledger.py``) — it SKIPS loudly
with no creds configured, refreshes against the REAL Google token endpoint, and makes ZERO
Calendar data-API calls (that is TK-72).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import keyring
import keyring.errors
import pytest
from google_auth_oauthlib.flow import InstalledAppFlow
from pydantic import SecretStr

import wombat.integrations.gcal.auth as auth_module
from wombat.config import ConfigurationError, WombatConfig, load_config
from wombat.integrations.gcal.auth import (
    GCAL_SCOPES,
    CalendarAuth,
    ScopeViolationError,
    assert_readonly_scopes,
)
from wombat.integrations.gcal.token_store import KeyringTokenStore

_WRITE_SCOPE = "https://www.googleapis.com/auth/calendar"  # write/manage — never granted (DEC-16)


def _make_config(
    *, client_id: str | None = "test-client-id", client_secret: str | None = "test-client-secret"
) -> WombatConfig:
    return WombatConfig(
        deepseek_api_key=SecretStr("unused-in-this-test"),
        deepseek_base_url="https://unused.example",
        google_oauth_client_id=client_id,
        google_oauth_client_secret=SecretStr(client_secret) if client_secret is not None else None,
    )


class _FakeTokenStore:
    """The Q-57(a)-mandated in-memory fake — unit tests never touch the real vault."""

    def __init__(self, *, initial: str | None = None) -> None:
        self._value = initial

    def load(self) -> str | None:
        return self._value

    def save(self, token: str) -> None:
        self._value = token

    def clear(self) -> None:
        self._value = None


class _FakeCredentials:
    """Stands in for ``google.oauth2.credentials.Credentials`` from a fake interactive flow."""

    def __init__(self, scopes: list[str], token: str = "fresh-token") -> None:
        self.scopes = scopes
        self.token = token

    def to_json(self) -> str:
        return json.dumps({"token": self.token, "scopes": self.scopes})


class _FakeFlow:
    def __init__(self, client_config: dict[str, object], scopes: list[str]) -> None:
        self.client_config = client_config
        self.scopes = scopes

    def run_local_server(self, port: int = 0, **kwargs: object) -> _FakeCredentials:
        return _FakeCredentials(list(self.scopes))


class _FakeTokenResponse:
    """Mimics ``google.auth.transport.Response`` — the token-endpoint HTTP response shape."""

    def __init__(self, status: int, data: bytes) -> None:
        self.status = status
        self.data = data
        self.headers: dict[str, str] = {}


def _fake_token_transport(new_access_token: str) -> object:
    """A fake ``google.auth.transport.Request`` callable that answers the token endpoint
    request in-process — proves the refresh works with ZERO network (Q-57(c))."""

    def _request(
        method: str | None = None,
        url: str | None = None,
        headers: object = None,
        body: object = None,
        **kwargs: object,
    ) -> _FakeTokenResponse:
        payload = {
            "access_token": new_access_token,
            "expires_in": 3600,
            "scope": " ".join(GCAL_SCOPES),
            "token_type": "Bearer",
        }
        return _FakeTokenResponse(200, json.dumps(payload).encode("utf-8"))

    return _request


# --------------------------------------------------------------------------------- construction


def test_construction_requires_client_id_and_secret() -> None:
    with pytest.raises(ConfigurationError, match="GOOGLE_OAUTH_CLIENT_ID"):
        CalendarAuth(config=_make_config(client_id=None), token_store=_FakeTokenStore())
    with pytest.raises(ConfigurationError, match="GOOGLE_OAUTH_CLIENT_SECRET"):
        CalendarAuth(config=_make_config(client_secret=None), token_store=_FakeTokenStore())


# ------------------------------------------------------------------------------------------ AC1


def test_gcal_scopes_constant_is_readonly_only() -> None:
    assert GCAL_SCOPES == ("https://www.googleapis.com/auth/calendar.readonly",)


def test_fresh_host_runs_oauth_flow_with_readonly_scope_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    def fake_from_client_config(client_config: dict[str, object], scopes: list[str]) -> _FakeFlow:
        calls["scopes"] = scopes
        return _FakeFlow(client_config, scopes)

    monkeypatch.setattr(InstalledAppFlow, "from_client_config", fake_from_client_config)
    token_store = _FakeTokenStore(initial=None)  # fresh host: no stored credential
    auth = CalendarAuth(config=_make_config(), token_store=token_store)

    creds = auth.get_credentials()

    # constructed with EXACTLY the module constant — never widened, never derived
    assert calls["scopes"] == list(GCAL_SCOPES)
    assert creds.scopes == list(GCAL_SCOPES)
    # persisted to the (fake) vault — never returned as a bare plaintext value
    assert token_store.load() is not None


def test_scope_guard_rejects_write_scoped_token() -> None:
    # a token that (incorrectly) carries a write/manage scope MUST fail the guard
    with pytest.raises(ScopeViolationError):
        assert_readonly_scopes([*GCAL_SCOPES, _WRITE_SCOPE])
    # a correctly-scoped token passes (does not raise)
    assert_readonly_scopes(list(GCAL_SCOPES))


def test_stored_token_with_broader_scope_is_rejected_before_credential_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TK-168 (CR-3): ``Credentials.from_authorized_user_info(..., scopes=list(GCAL_SCOPES))``
    overwrites ``creds.scopes`` with the passed constant, so a post-construction check of
    ``creds.scopes`` against ``GCAL_SCOPES`` checks the constant against itself and can never
    catch a vaulted token that actually granted a broader scope (older consent, manual edit,
    future scope change without re-consent). A stored token carrying the write/manage scope
    MUST be rejected before its credential is ever constructed/refreshed/returned."""
    stored = {
        "token": "stale-access-token",
        "refresh_token": "refresh-token-xyz",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "scopes": [*GCAL_SCOPES, _WRITE_SCOPE],
        "expiry": "2020-01-01T00:00:00Z",
    }
    token_store = _FakeTokenStore(initial=json.dumps(stored))
    auth = CalendarAuth(config=_make_config(), token_store=token_store)

    # neither the refresh transport nor the interactive flow may be reached
    refresh_calls: list[object] = []
    monkeypatch.setattr(
        auth_module, "GoogleAuthRequest", lambda: refresh_calls.append(None)
    )
    interactive_calls: list[object] = []
    monkeypatch.setattr(
        InstalledAppFlow,
        "from_client_config",
        lambda *a, **kw: interactive_calls.append((a, kw)),
    )

    with pytest.raises(ScopeViolationError):
        auth.get_credentials()

    assert refresh_calls == []
    assert interactive_calls == []
    # the vaulted token is untouched — no overwrite was attempted
    assert token_store.load() == json.dumps(stored)


# ------------------------------------------------------------------------------------------ AC2


def test_expired_token_refreshes_without_prompting(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = {
        "token": "stale-access-token",
        "refresh_token": "refresh-token-xyz",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "scopes": list(GCAL_SCOPES),
        "expiry": "2020-01-01T00:00:00Z",  # far in the past -> Credentials.expired is True
    }
    token_store = _FakeTokenStore(initial=json.dumps(stored))
    auth = CalendarAuth(config=_make_config(), token_store=token_store)

    # the token-endpoint transport is faked (zero network); the interactive flow is a Mock so
    # we can assert it is NEVER called on this path.
    monkeypatch.setattr(
        auth_module, "GoogleAuthRequest", lambda: _fake_token_transport("fresh-access-token")
    )
    interactive_calls: list[object] = []
    monkeypatch.setattr(
        InstalledAppFlow,
        "from_client_config",
        lambda *a, **kw: interactive_calls.append((a, kw)),
    )

    creds = auth.get_credentials()

    assert creds.token == "fresh-access-token"
    assert interactive_calls == []  # the interactive/consent flow was NEVER invoked
    assert token_store.load() is not None
    assert "fresh-access-token" in (token_store.load() or "")


# ------------------------------------------------------------------------------------------ AC3


def test_keyring_store_never_writes_a_plaintext_file_and_never_logs_the_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    vault: dict[tuple[str, str], str] = {}

    def fake_set(service: str, account: str, password: str) -> None:
        vault[(service, account)] = password

    def fake_get(service: str, account: str) -> str | None:
        return vault.get((service, account))

    monkeypatch.setattr(keyring, "set_password", fake_set)
    monkeypatch.setattr(keyring, "get_password", fake_get)
    monkeypatch.chdir(tmp_path)

    store = KeyringTokenStore()
    sentinel_secret = "sentinel-super-secret-9f8e7d6c5b4a"  # a known fake secret, never real
    fake_token_json = json.dumps({"token": sentinel_secret, "refresh_token": "r-token"})

    with caplog.at_level(logging.DEBUG):
        store.save(fake_token_json)
        loaded = store.load()

    assert loaded == fake_token_json
    assert list(tmp_path.rglob("*")) == []  # NO file was ever written under tmp_path
    assert sentinel_secret not in caplog.text  # NO credential material in any log


def test_keyring_vault_failure_raises_configuration_error_not_a_plaintext_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(service: str, account: str, password: str) -> None:
        raise keyring.errors.PasswordSetError("backend unavailable")

    monkeypatch.setattr(keyring, "set_password", boom)

    with pytest.raises(ConfigurationError):
        KeyringTokenStore().save("irrelevant")


# ------------------------------------------------------------------------------------- live smoke

_LIVE_ENV = "WOMBAT_TEST_GCAL_LIVE"

_requires_live_gcal = pytest.mark.skipif(
    not os.environ.get(_LIVE_ENV),
    reason=(
        f"{_LIVE_ENV} is not set — skipping the live Google token-endpoint smoke test. Run "
        "`python -m wombat.integrations.gcal.auth` once to grant consent (stores a token in "
        f"the OS keyring vault), then export {_LIVE_ENV}=1 to exercise a real refresh."
    ),
)


@_requires_live_gcal
def test_live_refresh_against_real_google_token_endpoint() -> None:
    """Loads the vault credential, refreshes against the REAL Google token endpoint, and
    asserts granted scopes are exactly readonly. ZERO Calendar data-API calls (TK-72's job)."""
    config = load_config()
    auth = CalendarAuth(config=config)  # real KeyringTokenStore — reads the real OS vault
    creds = auth.get_credentials()
    assert_readonly_scopes(creds.scopes)
    assert set(creds.scopes or []) == set(GCAL_SCOPES)
