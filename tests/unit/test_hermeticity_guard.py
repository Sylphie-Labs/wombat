"""tests/unit/test_hermeticity_guard.py — canary for the root conftest keyring tripwire
(TK-254, ISS-10(a), AC2).

Constructs a default keyring-backed token store and calls ``load()`` with NO local patch of
``keyring`` — the root ``tests/conftest.py`` autouse ``_keyring_tripwire`` fixture must intercept
the underlying ``keyring.get_password`` call and raise, proving the tripwire is armed for every
test in the suite rather than merely present in the file.
"""

from __future__ import annotations

import pytest

from wombat.integrations.gcal.token_store import KeyringTokenStore


def test_unpatched_keyring_access_raises_tripwire() -> None:
    store = KeyringTokenStore()
    with pytest.raises(AssertionError, match=r"keyring\.get_password"):
        store.load()
