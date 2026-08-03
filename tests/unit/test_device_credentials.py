"""tests/unit/test_device_credentials.py — TK-338 acceptance criteria — the per-device bearer-
token credential store (DEC-32 keyring tier).

All tests inject an in-memory fake ``DeviceVault`` (never the real vault) — the same shape as the
gcal ``TokenStore`` / voice ``VoiceKeyStore`` precedent, enforced repo-wide by conftest's
autouse keyring tripwire.
"""

from __future__ import annotations

import json
import logging

import pytest

from wombat.devices.credentials import DeviceCredentialStore, DeviceVault


class _FakeDeviceVault:
    """The in-memory fake — unit tests never touch the real vault."""

    def __init__(self, *, initial: str | None = None) -> None:
        self._blob = initial

    def load(self) -> str | None:
        return self._blob

    def save(self, blob: str) -> None:
        self._blob = blob

    def clear(self) -> None:
        self._blob = None


class _RaisingLoadVault:
    """A fake whose ``load`` always raises — proves ``verify`` fails closed (AC5)."""

    def load(self) -> str | None:
        raise RuntimeError("vault locked")

    def save(self, blob: str) -> None:
        raise AssertionError("not exercised")

    def clear(self) -> None:
        raise AssertionError("not exercised")


def test_fake_device_vault_satisfies_the_protocol() -> None:
    assert isinstance(_FakeDeviceVault(), DeviceVault)


def test_mint_returns_distinct_ids_and_tokens_and_persists_no_plaintext() -> None:
    vault = _FakeDeviceVault()
    store = DeviceCredentialStore(vault=vault)

    iphone_id, iphone_token = store.mint("iphone")
    watch_id, watch_token = store.mint("watch")

    assert iphone_id != watch_id
    assert iphone_token != watch_token

    blob = vault.load()
    assert blob is not None
    assert iphone_token not in blob
    assert watch_token not in blob


def test_verify_returns_device_id_for_valid_tokens_and_none_for_everything_else() -> None:
    vault = _FakeDeviceVault()
    store = DeviceCredentialStore(vault=vault)
    iphone_id, iphone_token = store.mint("iphone")
    watch_id, watch_token = store.mint("watch")

    assert store.verify(iphone_token) == iphone_id
    assert store.verify(watch_token) == watch_id
    assert store.verify(iphone_token[:-1]) is None
    assert store.verify("not-a-real-token") is None
    assert store.verify("") is None


def test_list_returns_names_and_timestamps_with_no_token_or_hash() -> None:
    vault = _FakeDeviceVault()
    store = DeviceCredentialStore(vault=vault)
    iphone_id, iphone_token = store.mint("iphone")
    watch_id, watch_token = store.mint("watch")

    devices = store.list()
    serialized = json.dumps(devices)

    assert {d["device_id"] for d in devices} == {iphone_id, watch_id}
    names_by_id = {d["device_id"]: d["name"] for d in devices}
    assert names_by_id == {iphone_id: "iphone", watch_id: "watch"}
    assert all("paired_at" in d for d in devices)
    assert iphone_token not in serialized
    assert watch_token not in serialized
    assert all("hash" not in d for d in devices)


def test_revoke_is_per_device_and_never_collateral() -> None:
    vault = _FakeDeviceVault()
    store = DeviceCredentialStore(vault=vault)
    iphone_id, iphone_token = store.mint("iphone")
    watch_id, watch_token = store.mint("watch")

    store.revoke(watch_id)

    assert store.verify(watch_token) is None
    assert store.verify(iphone_token) == iphone_id


def test_revoke_of_unknown_device_id_is_a_no_op() -> None:
    vault = _FakeDeviceVault()
    store = DeviceCredentialStore(vault=vault)
    store.revoke("does-not-exist")  # must not raise


def test_verify_against_a_locked_vault_fails_closed_with_one_loud_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = DeviceCredentialStore(vault=_RaisingLoadVault())
    with caplog.at_level(logging.WARNING, logger="wombat.devices.credentials"):
        result = store.verify("any-token")

    assert result is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
