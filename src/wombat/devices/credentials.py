"""wombat.devices.credentials — the per-device bearer-token credential store (TK-338, DEC-32
keyring tier), the seam TK-339's DeviceSurface (auth) and TK-342's pairing UX (mint/list/revoke)
both read.

A paired device (iPhone, Watch) gets its own high-entropy bearer token at pairing time. The
runtime process (which must verify tokens on every inbound request) and the settings-app process
(which must list/revoke devices) are separate processes with no shared in-process object, so the
record has to live somewhere both can reach it — the OS credential vault, DEC-32's existing
keyring tier (Q-57(a)/TK-71/TK-188 precedent), under ONE service/account holding a single
JSON blob of all paired devices.

Only the sha256 hex digest of each token is ever persisted — the plaintext token is returned to
the caller exactly once, at ``mint`` time, and never stored or logged anywhere. ``verify`` hashes
the presented token and looks up a match; a broken/locked vault on the read path degrades to
``None`` (fails CLOSED — nothing authenticates) with one loud warning, rather than raising and
taking the process down (CON-3).

``DeviceVault`` is a small ``Protocol`` (``load``/``save``/``clear`` over the one serialized blob)
mirroring the gcal ``TokenStore`` shape so unit tests inject an in-memory fake and never touch the
real vault. ``KeyringDeviceVault`` is the production adapter.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

import keyring
import keyring.errors

logger = logging.getLogger(__name__)

# Service/account for the OS credential vault entry — a descriptive module constant, NOT a
# TK-13 tunable. Shares the "wombat" service the gcal/gmail/voice vault entries already use.
WOMBAT_KEYRING_SERVICE = "wombat"
WOMBAT_DEVICES_KEYRING_ACCOUNT = "device-credentials"

_TOKEN_ENTROPY_BYTES = 32


class DeviceCredentialError(RuntimeError):
    """Raised when a WRITE to the device credential vault fails — a loud failure, never a silent
    fallback (DEC-32)."""


@runtime_checkable
class DeviceVault(Protocol):
    """The wombat-owned device-credential persistence seam — load/save/clear over ONE serialized
    blob, mirroring the gcal ``TokenStore`` pattern (Q-57(a)) so unit tests inject an in-memory
    fake and never touch the real vault. ``load`` returns ``None`` when nothing has been paired
    yet."""

    def load(self) -> str | None: ...

    def save(self, blob: str) -> None: ...

    def clear(self) -> None: ...


class KeyringDeviceVault:
    """Production ``DeviceVault`` adapter over the OS credential vault via ``keyring``.

    Never touch this from a unit test — inject an in-memory fake instead (``DeviceVault``).
    """

    def __init__(
        self,
        *,
        service: str = WOMBAT_KEYRING_SERVICE,
        account: str = WOMBAT_DEVICES_KEYRING_ACCOUNT,
    ) -> None:
        self._service = service
        self._account = account

    def load(self) -> str | None:
        return keyring.get_password(self._service, self._account)

    def save(self, blob: str) -> None:
        try:
            keyring.set_password(self._service, self._account, blob)
        except keyring.errors.KeyringError as exc:
            raise DeviceCredentialError(
                f"device credential vault write failed ({self._service}/{self._account}): {exc}"
            ) from exc

    def clear(self) -> None:
        try:
            keyring.delete_password(self._service, self._account)
        except keyring.errors.PasswordDeleteError:
            pass  # already absent — clear is idempotent
        except keyring.errors.KeyringError as exc:
            raise DeviceCredentialError(
                f"device credential vault clear failed ({self._service}/{self._account}): {exc}"
            ) from exc


_DeviceRecord = dict[str, str]
_Blob = dict[str, dict[str, _DeviceRecord]]


def _empty_blob() -> _Blob:
    return {"devices": {}}


def _load_devices(vault: DeviceVault) -> _Blob:
    raw = vault.load()
    if raw is None:
        return _empty_blob()
    blob: _Blob = json.loads(raw)
    return blob


def _save_devices(vault: DeviceVault, blob: _Blob) -> None:
    vault.save(json.dumps(blob))


class DeviceCredentialStore:
    """mint/verify/list/revoke a bearer token per paired device (TK-338)."""

    def __init__(self, *, vault: DeviceVault) -> None:
        self._vault = vault

    def mint(self, name: str) -> tuple[str, str]:
        """Pair a new device named ``name``. Returns ``(device_id, token)`` — the ONLY time the
        plaintext token is ever available; only its sha256 hex digest is persisted."""
        blob = _load_devices(self._vault)
        device_id = secrets.token_hex(8)
        token = secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)
        blob["devices"][device_id] = {
            "name": name,
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "paired_at": datetime.now(UTC).isoformat(),
        }
        _save_devices(self._vault, blob)
        return device_id, token

    def verify(self, token: str) -> str | None:
        """Return the ``device_id`` owning ``token``, or ``None`` if it verifies no device. A
        broken/locked vault fails CLOSED — logged as one loud warning, never raised (CON-3)."""
        try:
            blob = _load_devices(self._vault)
        except Exception as exc:
            logger.warning("device credential vault read failed during verify: %s", exc)
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        for device_id, record in blob["devices"].items():
            if record["token_hash"] == token_hash:
                return device_id
        return None

    def list(self) -> list[dict[str, str]]:
        """Return every paired device's name and pairing timestamp — never a token or hash."""
        blob = _load_devices(self._vault)
        return [
            {"device_id": device_id, "name": record["name"], "paired_at": record["paired_at"]}
            for device_id, record in blob["devices"].items()
        ]

    def revoke(self, device_id: str) -> None:
        """Remove ``device_id`` so its token never verifies again. Idempotent — revoking an
        already-absent or unknown device_id is not an error."""
        blob = _load_devices(self._vault)
        blob["devices"].pop(device_id, None)
        _save_devices(self._vault, blob)


__all__ = [
    "WOMBAT_DEVICES_KEYRING_ACCOUNT",
    "WOMBAT_KEYRING_SERVICE",
    "DeviceCredentialError",
    "DeviceCredentialStore",
    "DeviceVault",
    "KeyringDeviceVault",
]
