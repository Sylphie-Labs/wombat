"""tests/conftest.py — root test hermeticity (TK-254, ISS-10(a)).

TWO autouse fixtures guard every test in this suite against ever reaching the operator's real
Google OAuth credentials or real OS-keyring vault:

1. ``_strip_google_env`` — forces ``GOOGLE_OAUTH_CLIENT_ID``/``GOOGLE_OAUTH_CLIENT_SECRET`` to
   the empty string for the duration of each test. This repo's ``.env`` re-supplies real values
   for both vars (``WombatConfig``'s dotenv settings source), so a plain ``monkeypatch.delenv``
   would NOT be hermetic — dotenv would just re-fill the gap. ``setenv("", ...)`` outranks the
   dotenv source in pydantic-settings precedence AND the empty string fails
   ``_has_google_client_credentials``'s ``.strip()`` truthiness check, so every Google-gated boot
   path (``src/wombat/bootstrap.py``'s outbound gmail wiring, ``src/wombat/sources/bootstrap.py``'s
   ``_build_gcal_poller``/``_build_gmail_poller``) takes its existing loud-skip, Google-less
   branch by default. Tests that want to exercise the Google-wired path opt in explicitly with a
   test-level ``monkeypatch.setenv(...)`` (which runs AFTER this autouse strip) plus an injected
   fake token store — never the real keyring.

2. ``_keyring_tripwire`` — autouse-patches ``keyring.get_password``/``set_password``/
   ``delete_password`` to raise a loud ``AssertionError`` naming the offending call, so any code
   path that reaches the real OS keyring during a test fails immediately and unambiguously
   instead of silently reading/writing the operator's real vault. Tests that legitimately need to
   exercise keyring-adapter behavior (e.g. ``tests/voice/test_key_store.py``,
   ``tests/integrations/gcal/test_auth.py``, ``tests/integrations/gmail/test_auth.py``) already
   patch ``keyring.get_password``/``set_password``/``delete_password`` themselves at the test
   level — those local ``monkeypatch.setattr`` calls run after this autouse fixture and simply
   override it for that test, so they stay green.
"""

from __future__ import annotations

from collections.abc import Iterator

import keyring
import pytest


@pytest.fixture(autouse=True)
def _strip_google_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force both Google OAuth client-credential env vars empty for every test (TK-254).

    ``setenv`` to the empty string, NOT ``delenv`` — this repo's ``.env`` re-supplies both vars
    via ``WombatConfig``'s dotenv settings source, so deleting the env var would just let dotenv
    silently refill it. An explicit empty-string env override outranks dotenv in pydantic-
    settings precedence, and ``_has_google_client_credentials`` treats a blank/whitespace value
    as absent, so this is a real (not cosmetic) strip.
    """
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "")


def _tripwire(name: str) -> object:
    def _raise(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            f"tests/conftest.py hermeticity tripwire: unpatched test tried to call "
            f"keyring.{name}(...) — this would touch the operator's real OS-keyring vault. "
            f"Inject a fake token store, or patch keyring.{name} locally in the test."
        )

    return _raise


@pytest.fixture(autouse=True)
def _keyring_tripwire(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Autouse-patch the real ``keyring`` module's read/write/delete entry points to raise
    LOUD (naming the offending access) rather than ever touching the operator's real OS-keyring
    vault (TK-254, ISS-10(a)). Tests that legitimately exercise keyring-adapter behavior patch
    ``keyring.get_password``/``set_password``/``delete_password`` themselves at the test level —
    those local ``monkeypatch.setattr`` calls run after this fixture and simply override it.
    """
    monkeypatch.setattr(keyring, "get_password", _tripwire("get_password"))
    monkeypatch.setattr(keyring, "set_password", _tripwire("set_password"))
    monkeypatch.setattr(keyring, "delete_password", _tripwire("delete_password"))
    yield
