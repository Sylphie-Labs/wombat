"""TK-188 acceptance criteria — voice-provider key vault (DEC-32).

CI tests use fakes ONLY, ZERO live keyring dependence (DEF-7 pattern):
  AC1 (env override wins): ``test_env_override_wins_over_store_value``.
  AC2 (store fallback / absent-both): ``test_store_value_used_when_no_env_override``,
      ``test_none_when_neither_env_nor_store_has_a_value``.
  AC3 (read-path degrades loud, write-path raises loud):
      ``test_store_get_failure_logs_one_warning_and_returns_none``,
      ``test_keyring_backend_failure_on_set_raises_loud_error``.

Plus a couple of adapter-shape checks (account naming, idempotent delete) matching the gcal
``token_store`` precedent.
"""

from __future__ import annotations

import logging

import keyring
import keyring.errors
import pytest
from pydantic import SecretStr

from wombat.voice.key_store import (
    WOMBAT_KEYRING_SERVICE,
    KeyringVoiceKeyStore,
    VoiceKeyStoreError,
    resolve_provider_key,
)


class _FakeVoiceKeyStore:
    """The in-memory fake — unit tests never touch the real vault."""

    def __init__(self, *, initial: dict[str, str] | None = None) -> None:
        self._values = dict(initial or {})

    def get(self, provider: str) -> str | None:
        return self._values.get(provider)

    def set(self, provider: str, key: str) -> None:
        self._values[provider] = key

    def delete(self, provider: str) -> None:
        self._values.pop(provider, None)


class _RaisingGetStore:
    """A fake whose ``get`` always raises — proves resolve_provider_key degrades (AC3)."""

    def get(self, provider: str) -> str | None:
        raise RuntimeError("vault backend unreachable")

    def set(self, provider: str, key: str) -> None:
        raise AssertionError("not exercised")

    def delete(self, provider: str) -> None:
        raise AssertionError("not exercised")


def test_env_override_wins_over_store_value() -> None:
    # AC1: a non-blank env override wins even when the store holds a DIFFERENT value.
    store = _FakeVoiceKeyStore(initial={"elevenlabs": "store-value"})
    result = resolve_provider_key("elevenlabs", SecretStr("env-value"), store)
    assert result == "env-value"


def test_store_value_used_when_no_env_override() -> None:
    # AC2: no env override -> the store value is returned.
    store = _FakeVoiceKeyStore(initial={"elevenlabs": "store-value"})
    assert resolve_provider_key("elevenlabs", None, store) == "store-value"


def test_blank_env_override_falls_back_to_store() -> None:
    store = _FakeVoiceKeyStore(initial={"elevenlabs": "store-value"})
    assert resolve_provider_key("elevenlabs", SecretStr("   "), store) == "store-value"


def test_none_when_neither_env_nor_store_has_a_value() -> None:
    # AC2: absent both -> None.
    store = _FakeVoiceKeyStore()
    assert resolve_provider_key("elevenlabs", None, store) is None


def test_store_get_failure_logs_one_warning_and_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # AC3 (read path): a store whose get() raises -> exactly one loud WARNING, None returned, no
    # exception escapes (CON-3).
    store = _RaisingGetStore()
    with caplog.at_level(logging.WARNING, logger="wombat.voice.key_store"):
        result = resolve_provider_key("elevenlabs", None, store)
    assert result is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "elevenlabs" in warnings[0].getMessage()


def test_keyring_backend_failure_on_set_raises_loud_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # AC3 (write path): a fake keyring backend raising on set() -> the module error raises loud.
    def boom(service: str, account: str, key: str) -> None:
        raise keyring.errors.PasswordSetError("backend unavailable")

    monkeypatch.setattr(keyring, "set_password", boom)
    store = KeyringVoiceKeyStore()
    with pytest.raises(VoiceKeyStoreError):
        store.set("elevenlabs", "secret-key")


def test_keyring_backend_failure_on_delete_raises_loud_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(service: str, account: str) -> None:
        raise keyring.errors.KeyringError("backend unavailable")

    monkeypatch.setattr(keyring, "delete_password", boom)
    store = KeyringVoiceKeyStore()
    with pytest.raises(VoiceKeyStoreError):
        store.delete("elevenlabs")


def test_keyring_delete_of_missing_entry_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    # gcal parity: PasswordDeleteError on an absent entry is swallowed, not raised.
    def missing(service: str, account: str) -> None:
        raise keyring.errors.PasswordDeleteError("not found")

    monkeypatch.setattr(keyring, "delete_password", missing)
    store = KeyringVoiceKeyStore()
    store.delete("elevenlabs")  # must not raise


def test_keyring_store_uses_expected_account_naming(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_get(service: str, account: str) -> str | None:
        calls.append((service, account))
        return "the-key"

    monkeypatch.setattr(keyring, "get_password", fake_get)
    store = KeyringVoiceKeyStore()
    assert store.get("elevenlabs") == "the-key"
    assert calls == [(WOMBAT_KEYRING_SERVICE, "voice-elevenlabs-api-key")]
