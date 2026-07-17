"""TK-256 acceptance criteria — wombat.settings_app.google_connect (DEC-50).

AC1 (status probe, honest + non-crashing): ``test_not_configured_never_calls_auth_factory``,
    ``test_not_connected_never_calls_auth_factory``,
    ``test_stored_token_and_successful_get_credentials_is_connected``,
    ``test_stored_token_and_raising_get_credentials_is_expired``,
    ``test_manager_status_aggregates_both_services``.
AC2 (background consent trigger): ``test_connect_returns_immediately_and_reports_in_progress``,
    ``test_connect_flips_to_connected_after_the_fake_save``,
    ``test_second_connect_while_in_progress_raises``,
    ``test_raising_consent_runner_surfaces_error_and_stays_up``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import NoReturn

import pytest

from wombat.settings_app.google_connect import (
    ConsentInProgressError,
    GoogleConnectionManager,
    GoogleServiceConnection,
)


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition never became true within the timeout")


class _FakeTokenStore:
    def __init__(self, initial: str | None = None) -> None:
        self.token = initial

    def load(self) -> str | None:
        return self.token

    def save(self, value: str) -> None:
        self.token = value


class _StaticAuth:
    """A fake auth object whose ``get_credentials()`` either succeeds or raises once, fixed."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error

    def get_credentials(self) -> object:
        if self._error is not None:
            raise self._error
        return object()


def _forbidden_factory() -> NoReturn:
    raise AssertionError(
        "auth_factory must not be called — the load()-None case must never invoke "
        "get_credentials()"
    )


def test_not_configured_never_calls_auth_factory() -> None:
    conn = GoogleServiceConnection(
        configured=False, token_store=_FakeTokenStore(None), auth_factory=_forbidden_factory
    )
    assert conn.status() == {"status": "not_configured", "consent": "idle"}


def test_not_connected_never_calls_auth_factory() -> None:
    conn = GoogleServiceConnection(
        configured=True, token_store=_FakeTokenStore(None), auth_factory=_forbidden_factory
    )
    assert conn.status() == {"status": "not_connected", "consent": "idle"}


def test_stored_token_and_successful_get_credentials_is_connected() -> None:
    conn = GoogleServiceConnection(
        configured=True,
        token_store=_FakeTokenStore("stored-token"),
        auth_factory=lambda: _StaticAuth(),
    )
    assert conn.status() == {"status": "connected", "consent": "idle"}


def test_stored_token_and_raising_get_credentials_is_expired() -> None:
    from google.auth.exceptions import RefreshError

    error = RefreshError("token revoked")  # type: ignore[no-untyped-call]
    conn = GoogleServiceConnection(
        configured=True,
        token_store=_FakeTokenStore("stored-token"),
        auth_factory=lambda: _StaticAuth(error=error),
    )
    assert conn.status() == {"status": "expired", "consent": "idle"}


def test_manager_status_aggregates_both_services() -> None:
    manager = GoogleConnectionManager(
        {
            "gmail": GoogleServiceConnection(
                configured=False, token_store=_FakeTokenStore(), auth_factory=_forbidden_factory
            ),
            "gcal": GoogleServiceConnection(
                configured=True,
                token_store=_FakeTokenStore("tok"),
                auth_factory=lambda: _StaticAuth(),
            ),
        }
    )
    assert manager.status() == {
        "gmail": {"status": "not_configured", "consent": "idle"},
        "gcal": {"status": "connected", "consent": "idle"},
    }


class _BlockingSaveAuth:
    """Mimics ``GmailAuth.get_credentials()``'s real shape: blocks (the interactive consent
    flow), then saves a token to the store, then returns — used to prove ``connect()`` never
    blocks the caller and that status flips to connected only once the fake save lands."""

    def __init__(
        self, token_store: _FakeTokenStore, started: threading.Event, resume: threading.Event
    ) -> None:
        self._token_store = token_store
        self._started = started
        self._resume = resume

    def get_credentials(self) -> object:
        self._started.set()
        self._resume.wait(timeout=5)
        self._token_store.save("consented-token")
        return object()


def test_connect_returns_immediately_and_reports_in_progress() -> None:
    token_store = _FakeTokenStore(None)
    started = threading.Event()
    resume = threading.Event()
    conn = GoogleServiceConnection(
        configured=True,
        token_store=token_store,
        auth_factory=lambda: _BlockingSaveAuth(token_store, started, resume),
    )

    conn.connect()  # must return immediately — run_local_server blocks for minutes (CON-5)
    assert started.wait(timeout=5)
    assert conn.status()["consent"] == "in_progress"

    resume.set()  # let the fake consent flow finish so the thread doesn't leak past the test
    _wait_until(lambda: conn.status()["consent"] != "in_progress")


def test_connect_flips_to_connected_after_the_fake_save() -> None:
    token_store = _FakeTokenStore(None)
    started = threading.Event()
    resume = threading.Event()
    conn = GoogleServiceConnection(
        configured=True,
        token_store=token_store,
        auth_factory=lambda: _BlockingSaveAuth(token_store, started, resume),
    )

    conn.connect()
    assert started.wait(timeout=5)
    resume.set()
    _wait_until(lambda: token_store.load() is not None)
    _wait_until(lambda: conn.status() == {"status": "connected", "consent": "idle"})


def test_second_connect_while_in_progress_raises() -> None:
    token_store = _FakeTokenStore(None)
    started = threading.Event()
    resume = threading.Event()
    conn = GoogleServiceConnection(
        configured=True,
        token_store=token_store,
        auth_factory=lambda: _BlockingSaveAuth(token_store, started, resume),
    )

    conn.connect()
    assert started.wait(timeout=5)
    with pytest.raises(ConsentInProgressError):
        conn.connect()

    resume.set()  # release the background thread before the test ends
    _wait_until(lambda: conn.status()["consent"] != "in_progress")


class _RaisingAuth:
    def get_credentials(self) -> object:
        raise RuntimeError("consent flow failed: user closed the browser")


def test_raising_consent_runner_surfaces_error_and_stays_up() -> None:
    conn = GoogleServiceConnection(
        configured=True, token_store=_FakeTokenStore(None), auth_factory=lambda: _RaisingAuth()
    )

    conn.connect()
    _wait_until(lambda: conn.status()["consent"] == "error")
    status = conn.status()
    assert status["status"] == "not_connected"
    assert status["consent"] == "error"
    assert "consent flow failed" in str(status["error"])

    # the process stays up: a fresh connect() attempt is accepted (not permanently wedged).
    conn.connect()
    _wait_until(lambda: conn.status()["consent"] == "error")
