"""TK-75 acceptance criteria — Gmail OAuth2 credential lifecycle (Q-65, mirrors TK-71/Q-57).

CI tests are mocked, ZERO network:
  AC (readonly-scope-only, Q-65 ruling 1): ``test_gmail_scopes_constant_is_readonly_only``,
      ``test_fresh_host_runs_oauth_flow_with_readonly_scope_only``,
      ``test_scope_guard_rejects_compose_scoped_token``.
  AC (non-interactive refresh): ``test_expired_token_refreshes_without_prompting``.
  AC (separate keyring account, Q-65 ruling 2): ``test_default_token_store_uses_gmail_account``,
      ``test_gmail_keyring_account_is_distinct_from_gcal``.
  AC (reuses shared client id/secret, no new env vars, Q-65 ruling 2):
      ``test_construction_requires_client_id_and_secret``.

Plus exactly ONE live smoke test gated on ``WOMBAT_TEST_GMAIL_LIVE=1`` (mirrors TK-71's
``WOMBAT_TEST_GCAL_LIVE`` idiom) — it SKIPS loudly with no creds configured, refreshes against
the REAL Google token endpoint, and makes ZERO Gmail data-API calls (that is ``GmailPoller``'s
job).
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

import wombat.integrations.gmail.auth as auth_module
from wombat.config import ConfigurationError, WombatConfig, load_config
from wombat.integrations.gcal.token_store import WOMBAT_KEYRING_ACCOUNT as GCAL_KEYRING_ACCOUNT
from wombat.integrations.gmail.auth import (
    GMAIL_SCOPES,
    GmailAuth,
    ScopeViolationError,
    assert_gmail_readonly_scopes,
)
from wombat.integrations.gmail.token_store import GMAIL_KEYRING_ACCOUNT, KeyringTokenStore

_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"  # send-capable — never granted


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
    """The in-memory fake — unit tests never touch the real vault."""

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
    request in-process — proves the refresh works with ZERO network."""

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
            "scope": " ".join(GMAIL_SCOPES),
            "token_type": "Bearer",
        }
        return _FakeTokenResponse(200, json.dumps(payload).encode("utf-8"))

    return _request


# --------------------------------------------------------------------------------- construction


def test_construction_requires_client_id_and_secret() -> None:
    with pytest.raises(ConfigurationError, match="GOOGLE_OAUTH_CLIENT_ID"):
        GmailAuth(config=_make_config(client_id=None), token_store=_FakeTokenStore())
    with pytest.raises(ConfigurationError, match="GOOGLE_OAUTH_CLIENT_SECRET"):
        GmailAuth(config=_make_config(client_secret=None), token_store=_FakeTokenStore())


# ------------------------------------------------------------------------------- readonly scope


def test_gmail_scopes_constant_is_readonly_only() -> None:
    assert GMAIL_SCOPES == ("https://www.googleapis.com/auth/gmail.readonly",)


def test_fresh_host_runs_oauth_flow_with_readonly_scope_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    def fake_from_client_config(client_config: dict[str, object], scopes: list[str]) -> _FakeFlow:
        calls["scopes"] = scopes
        return _FakeFlow(client_config, scopes)

    monkeypatch.setattr(InstalledAppFlow, "from_client_config", fake_from_client_config)
    token_store = _FakeTokenStore(initial=None)  # fresh host: no stored credential
    auth = GmailAuth(config=_make_config(), token_store=token_store)

    creds = auth.get_credentials()

    # constructed with EXACTLY the module constant — never widened, never derived
    assert calls["scopes"] == list(GMAIL_SCOPES)
    assert creds.scopes == list(GMAIL_SCOPES)
    assert token_store.load() is not None


def test_scope_guard_rejects_compose_scoped_token() -> None:
    # a token that carries the send-capable compose scope MUST fail the guard (Q-65 ruling 1)
    with pytest.raises(ScopeViolationError):
        assert_gmail_readonly_scopes([*GMAIL_SCOPES, _COMPOSE_SCOPE])
    # any other extra scope also fails
    with pytest.raises(ScopeViolationError):
        assert_gmail_readonly_scopes([*GMAIL_SCOPES, "https://www.googleapis.com/auth/gmail.send"])
    # a correctly-scoped token passes (does not raise)
    assert_gmail_readonly_scopes(list(GMAIL_SCOPES))


# ---------------------------------------------------------------------------------- refresh


def test_expired_token_refreshes_without_prompting(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = {
        "token": "stale-access-token",
        "refresh_token": "refresh-token-xyz",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "scopes": list(GMAIL_SCOPES),
        "expiry": "2020-01-01T00:00:00Z",  # far in the past -> Credentials.expired is True
    }
    token_store = _FakeTokenStore(initial=json.dumps(stored))
    auth = GmailAuth(config=_make_config(), token_store=token_store)

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


# --------------------------------------------------------------------------- keyring account


def test_gmail_keyring_account_is_distinct_from_gcal() -> None:
    assert GMAIL_KEYRING_ACCOUNT == "gmail-oauth-token"
    assert GMAIL_KEYRING_ACCOUNT != GCAL_KEYRING_ACCOUNT


def test_default_token_store_uses_gmail_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """When no ``token_store`` is injected, ``GmailAuth`` builds a ``KeyringTokenStore`` scoped
    to the gmail account — never the gcal one (Q-65 ruling 2)."""
    captured: dict[str, str] = {}

    class _RecordingKeyringTokenStore(KeyringTokenStore):
        def __init__(self, *, service: str = "wombat", account: str = "") -> None:
            captured["service"] = service
            captured["account"] = account
            super().__init__(service=service, account=account)

        def load(self) -> str | None:
            return None  # fresh host

        def save(self, token: str) -> None:
            pass

    monkeypatch.setattr(auth_module, "KeyringTokenStore", _RecordingKeyringTokenStore)
    monkeypatch.setattr(
        InstalledAppFlow,
        "from_client_config",
        lambda *a, **kw: _FakeFlow({}, list(GMAIL_SCOPES)),
    )

    GmailAuth(config=_make_config()).get_credentials()

    assert captured["account"] == GMAIL_KEYRING_ACCOUNT


def test_keyring_store_never_writes_a_plaintext_file_and_never_logs_the_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Reuses TK-71's proven ``KeyringTokenStore`` — this re-proves it for the gmail account
    specifically, so a future refactor cannot quietly regress the no-plaintext guarantee."""
    vault: dict[tuple[str, str], str] = {}

    def fake_set(service: str, account: str, password: str) -> None:
        vault[(service, account)] = password

    def fake_get(service: str, account: str) -> str | None:
        return vault.get((service, account))

    monkeypatch.setattr(keyring, "set_password", fake_set)
    monkeypatch.setattr(keyring, "get_password", fake_get)
    monkeypatch.chdir(tmp_path)

    store = KeyringTokenStore(account=GMAIL_KEYRING_ACCOUNT)
    sentinel_secret = "sentinel-gmail-secret-1a2b3c4d5e"
    fake_token_json = json.dumps({"token": sentinel_secret, "refresh_token": "r-token"})

    with caplog.at_level(logging.DEBUG):
        store.save(fake_token_json)
        loaded = store.load()

    assert loaded == fake_token_json
    assert vault == {("wombat", GMAIL_KEYRING_ACCOUNT): fake_token_json}
    assert list(tmp_path.rglob("*")) == []  # NO file was ever written under tmp_path
    assert sentinel_secret not in caplog.text  # NO credential material in any log


def test_keyring_vault_failure_raises_configuration_error_not_a_plaintext_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(service: str, account: str, password: str) -> None:
        raise keyring.errors.PasswordSetError("backend unavailable")

    monkeypatch.setattr(keyring, "set_password", boom)

    with pytest.raises(ConfigurationError):
        KeyringTokenStore(account=GMAIL_KEYRING_ACCOUNT).save("irrelevant")


# ------------------------------------------------------------------------------------- live smoke

_LIVE_ENV = "WOMBAT_TEST_GMAIL_LIVE"

_requires_live_gmail = pytest.mark.skipif(
    not os.environ.get(_LIVE_ENV),
    reason=(
        f"{_LIVE_ENV} is not set — skipping the live Google token-endpoint smoke test. Run "
        "`python -m wombat.integrations.gmail.auth` once to grant consent (stores a token in "
        f"the OS keyring vault), then export {_LIVE_ENV}=1 to exercise a real refresh."
    ),
)


@_requires_live_gmail
def test_live_refresh_against_real_google_token_endpoint() -> None:
    """Loads the vault credential, refreshes against the REAL Google token endpoint, and
    asserts granted scopes are exactly readonly. ZERO Gmail data-API calls (GmailPoller's job)."""
    config = load_config()
    auth = GmailAuth(config=config)  # real KeyringTokenStore — reads the real OS vault
    creds = auth.get_credentials()
    assert_gmail_readonly_scopes(creds.scopes)
    assert set(creds.scopes or []) == set(GMAIL_SCOPES)
