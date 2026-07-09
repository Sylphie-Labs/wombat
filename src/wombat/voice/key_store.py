"""wombat.voice.key_store — the OS-keyring vault for voice-provider API keys (TK-188, DEC-32).

DEC-32 tier-1 BINDING: voice-provider secrets live in the OS keyring, are NEVER written to any
wombat-managed file, and the resolution order is explicit: an env/.env value wins, else the
keyring, else absent. This module rides the verified ``wombat.integrations.gcal.token_store``
pattern (Q-57(a)) — a small ``Protocol`` (``VoiceKeyStore``) so unit tests inject an in-memory
fake, plus a production ``KeyringVoiceKeyStore`` adapter over the ``keyring`` library (already a
runtime dep).

Design:
  * ``VoiceKeyStore`` is deliberately three methods — ``get``/``set``/``delete`` — keyed by
    provider name (e.g. ``"elevenlabs"``); unit tests never touch the real vault.
  * ``KeyringVoiceKeyStore`` maps each provider to its own keyring account,
    ``f"voice-{provider}-api-key"``, under the shared ``WOMBAT_KEYRING_SERVICE`` (``"wombat"``,
    the same service the gcal/gmail token stores use). A keyring failure on a WRITE
    (``set``/``delete``) raises a loud ``VoiceKeyStoreError`` — TK-197's settings API surfaces
    it to the operator. ``delete`` of an already-absent entry is a no-op
    (``PasswordDeleteError`` swallowed — gcal parity).
  * ``resolve_provider_key`` is the ONE read-path seam a caller uses: a non-blank env override
    always wins; otherwise it falls back to the vault. CON-3 BINDING: ANY exception raised while
    reading the vault is caught, logged LOUD (``logger.warning``), and resolves to ``None`` — a
    broken vault degrades gracefully, it never kills boot.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

import keyring
import keyring.errors
from pydantic import SecretStr

logger = logging.getLogger(__name__)

# Shared OS credential-vault service name (same service as the gcal/gmail token stores,
# Q-57(a)) — a descriptive module constant, NOT a TK-13 tunable.
WOMBAT_KEYRING_SERVICE = "wombat"


class VoiceKeyStoreError(RuntimeError):
    """Raised when a WRITE (``set``/``delete``) to the voice-provider key vault fails — a loud
    failure TK-197's settings API surfaces to the operator, never a silent fallback (DEC-32)."""


@runtime_checkable
class VoiceKeyStore(Protocol):
    """The wombat-owned voice-provider-key persistence seam — deliberately just three methods so
    a test double is trivial to write. ``get`` returns ``None`` when no key has been stored for
    ``provider`` yet; ``set`` persists the key; ``delete`` removes it (idempotent — deleting an
    already-absent key is not an error)."""

    def get(self, provider: str) -> str | None: ...

    def set(self, provider: str, key: str) -> None: ...

    def delete(self, provider: str) -> None: ...


def _account_for(provider: str) -> str:
    return f"voice-{provider}-api-key"


class KeyringVoiceKeyStore:
    """Production ``VoiceKeyStore`` adapter over the OS credential vault via ``keyring``.

    Never touch this from a unit test (Q-57(a) parity) — inject an in-memory fake instead. Any
    live-keyring smoke must loud-skip by default (DEF-7 pattern).
    """

    def __init__(self, *, service: str = WOMBAT_KEYRING_SERVICE) -> None:
        self._service = service

    def get(self, provider: str) -> str | None:
        return keyring.get_password(self._service, _account_for(provider))

    def set(self, provider: str, key: str) -> None:
        account = _account_for(provider)
        try:
            keyring.set_password(self._service, account, key)
        except keyring.errors.KeyringError as exc:
            # Covers backend-unavailable, write failure, and an oversized blob alike — all fail
            # LOUD here, never a silent plaintext-file fallback (DEC-32).
            raise VoiceKeyStoreError(
                f"voice key vault write failed ({self._service}/{account}): {exc}"
            ) from exc

    def delete(self, provider: str) -> None:
        account = _account_for(provider)
        try:
            keyring.delete_password(self._service, account)
        except keyring.errors.PasswordDeleteError:
            pass  # already absent — delete is idempotent
        except keyring.errors.KeyringError as exc:
            raise VoiceKeyStoreError(
                f"voice key vault delete failed ({self._service}/{account}): {exc}"
            ) from exc


def resolve_provider_key(
    provider: str, env_override: SecretStr | None, store: VoiceKeyStore
) -> str | None:
    """The ONE read-path seam (DEC-32): a non-blank ``env_override`` always wins; otherwise fall
    back to ``store.get(provider)``. CON-3 BINDING: ANY exception raised while reading the store
    is caught, logged LOUD, and resolves to ``None`` — a broken vault degrades gracefully, it
    never crashes boot."""
    if env_override is not None:
        value = env_override.get_secret_value()
        if value.strip():
            return value
    try:
        return store.get(provider)
    except Exception as exc:
        logger.warning("voice key vault read failed for provider %r: %s", provider, exc)
        return None


__all__ = [
    "WOMBAT_KEYRING_SERVICE",
    "KeyringVoiceKeyStore",
    "VoiceKeyStore",
    "VoiceKeyStoreError",
    "resolve_provider_key",
]
