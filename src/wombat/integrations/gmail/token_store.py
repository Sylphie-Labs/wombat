"""wombat.integrations.gmail.token_store — the gmail-account OS keyring vault (TK-75, Q-65
ruling 2, extends Q-57(a)).

Q-65 ruling 2 BINDING: gmail gets its OWN keyring account (``gmail-oauth-token``, distinct from
gcal's ``gcal-oauth-token``) under the SAME ``WOMBAT_KEYRING_SERVICE = "wombat"``. TK-71's
``KeyringTokenStore`` (``wombat.integrations.gcal.token_store``) is already account-parameterized
(``__init__(*, service=..., account=...)``), so this module REUSES it rather than duplicating the
keyring error-handling logic — it only defines the gmail-specific account constant and re-exports
the shared ``TokenStore`` Protocol + ``KeyringTokenStore`` adapter for import convenience/symmetry
with ``wombat.integrations.gcal.token_store``.
"""

from __future__ import annotations

from wombat.integrations.gcal.token_store import (
    WOMBAT_KEYRING_SERVICE,
    KeyringTokenStore,
    TokenStore,
)

# The gmail-specific keyring account (Q-65 ruling 2) — same service, separate account from gcal's
# WOMBAT_KEYRING_ACCOUNT ("gcal-oauth-token"), so the two providers' tokens never collide.
GMAIL_KEYRING_ACCOUNT = "gmail-oauth-token"


__all__ = [
    "GMAIL_KEYRING_ACCOUNT",
    "WOMBAT_KEYRING_SERVICE",
    "KeyringTokenStore",
    "TokenStore",
]
