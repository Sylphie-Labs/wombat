"""wombat.integrations.gcal.token_store — the OS keyring vault for the Google OAuth token
(TK-71, Q-57(a)).

Q-57(a) BINDING: the token store is the OS credential vault ONLY (Windows Credential
Manager/DPAPI via the ``keyring`` library on this laptop). The full serialized Google
authorized-user token JSON is stored directly in the vault — NO token bytes are EVER written
to a wombat-managed file, so AC3 (encrypted at rest, no plaintext) holds in the STRONG form:
no plaintext file exists at all; at-rest encryption is the platform vault's, not wombat's.

``TokenStore`` is a small ``@runtime_checkable`` Protocol (``load``/``save``/``clear``) so unit
tests inject an IN-MEMORY fake — tests must never touch the real vault. ``KeyringTokenStore`` is
the production adapter. If the vault backend is unavailable, a write/read/clear fails, or the
blob exceeds the vault's size limit, ``keyring`` raises and this module converts that into a
LOUD ``ConfigurationError`` — NEVER a silent plaintext-file fallback (NG-3).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import keyring
import keyring.errors

from wombat.config import ConfigurationError

# Service/account names for the OS credential vault entry (Q-57(a)). Descriptive module
# constants, NOT TK-13 tunables — there is nothing here for an operator to retune.
WOMBAT_KEYRING_SERVICE = "wombat"
WOMBAT_KEYRING_ACCOUNT = "gcal-oauth-token"


@runtime_checkable
class TokenStore(Protocol):
    """The wombat-owned token-persistence seam (Q-57(a)) — deliberately just three methods
    so a test double is trivial to write. ``load`` returns ``None`` when no token has been
    stored yet (the fresh-host case, AC1); ``save`` persists the full serialized token JSON;
    ``clear`` removes it (idempotent — clearing an already-absent token is not an error)."""

    def load(self) -> str | None: ...

    def save(self, token: str) -> None: ...

    def clear(self) -> None: ...


class KeyringTokenStore:
    """Production ``TokenStore`` adapter over the OS credential vault via ``keyring``.

    Never touch this from a unit test (Q-57(a)) — inject an in-memory fake instead. The one
    exception is the live smoke test (gated on ``WOMBAT_TEST_GCAL_LIVE``), which deliberately
    exercises the real vault as part of proving the end-to-end bring-up path.
    """

    def __init__(
        self, *, service: str = WOMBAT_KEYRING_SERVICE, account: str = WOMBAT_KEYRING_ACCOUNT
    ) -> None:
        self._service = service
        self._account = account

    def load(self) -> str | None:
        try:
            return keyring.get_password(self._service, self._account)
        except keyring.errors.KeyringError as exc:
            raise ConfigurationError(
                f"gcal token vault read failed ({self._service}/{self._account}): {exc}"
            ) from exc

    def save(self, token: str) -> None:
        try:
            keyring.set_password(self._service, self._account, token)
        except keyring.errors.KeyringError as exc:
            # Covers backend-unavailable, write failure, and an oversized blob alike — all
            # fail LOUD here, never a silent plaintext-file fallback (Q-57(a), NG-3).
            raise ConfigurationError(
                f"gcal token vault write failed ({self._service}/{self._account}): {exc}"
            ) from exc

    def clear(self) -> None:
        try:
            keyring.delete_password(self._service, self._account)
        except keyring.errors.PasswordDeleteError:
            pass  # already absent — clear is idempotent
        except keyring.errors.KeyringError as exc:
            raise ConfigurationError(
                f"gcal token vault clear failed ({self._service}/{self._account}): {exc}"
            ) from exc


__all__ = [
    "WOMBAT_KEYRING_ACCOUNT",
    "WOMBAT_KEYRING_SERVICE",
    "KeyringTokenStore",
    "TokenStore",
]
