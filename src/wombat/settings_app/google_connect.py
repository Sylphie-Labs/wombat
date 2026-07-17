"""wombat.settings_app.google_connect — the in-app Google OAuth connection manager (TK-256,
DEC-50).

DEC-50 BINDING: consumer-facing Google consent lives in the app; the CLI auth modules
(``wombat.integrations.gmail.auth.GmailAuth`` / ``wombat.integrations.gcal.auth.CalendarAuth``)
are REUSED VERBATIM here, never modified. This module owns exactly two things per service
(``'gmail'``/``'gcal'``):

  * ``GoogleServiceConnection.status()`` — the honest, non-crashing status PROBE (DEC-49
    posture). Unresolved client creds -> ``not_configured``. A ``None`` stored token ->
    ``not_connected``, and ``get_credentials()`` is NEVER invoked in that case — verified in
    source: ``GmailAuth.get_credentials`` (``wombat/integrations/gmail/auth.py`` 132-170) and
    ``CalendarAuth.get_credentials`` (``wombat/integrations/gcal/auth.py``, mirrors it) only run
    the interactive ``InstalledAppFlow.run_local_server(port=0)`` when the stored token is
    ``None``; a stored-but-expired token takes the non-interactive ``creds.refresh`` branch. So
    probing via ``get_credentials()`` is browser-safe EXACTLY when ``load()`` is not ``None``.
    Otherwise ``get_credentials()`` runs inside a broad ``try``: success -> ``connected``, any
    ``Exception`` -> ``expired``. This probe never raises.
  * ``GoogleServiceConnection.connect()`` — the background CONSENT TRIGGER. Runs the SAME
    ``get_credentials()`` on a daemon thread (``run_local_server`` blocks for minutes while the
    system browser pops for Jim — CON-5 — so it must never run on the request thread). Per-service
    consent state (``idle``/``in_progress``/``error``) lives in process memory only; a second
    ``connect()`` while ``in_progress`` raises ``ConsentInProgressError`` (the route's 409). The
    token lands in the shared OS keyring via the auth object's OWN ``token_store.save`` — custody
    is unchanged.

``token_store``/``auth_factory`` are injected per service so every test runs on fakes; production
wiring (``wombat.settings_app.__main__``) passes real ``GmailAuth``/``CalendarAuth`` instances
(via a factory, since ``run_local_server`` must be re-entered fresh each consent attempt) over the
real keyring token stores.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Literal, Protocol

logger = logging.getLogger(__name__)

ServiceName = Literal["gmail", "gcal"]

# The closed service vocabulary this module ever manages (DEC-50) — GET /google/status and the
# POST /google/{service}/connect route both key off exactly these two names.
GOOGLE_SERVICES: tuple[ServiceName, ...] = ("gmail", "gcal")

ConnectionStatus = Literal["not_configured", "not_connected", "expired", "connected"]
ConsentState = Literal["idle", "in_progress", "error"]


class GoogleAuthLike(Protocol):
    """The ONE seam this module consumes from ``GmailAuth``/``CalendarAuth`` — ``get_credentials``
    (browser-safe exactly when the injected token store already holds a token)."""

    def get_credentials(self) -> object: ...


class GoogleTokenStoreLike(Protocol):
    """The ONE seam this module reads directly from a token store — ``load`` — mirroring the
    ``TokenStore`` Protocol in ``wombat.integrations.gcal.token_store``."""

    def load(self) -> str | None: ...


class ConsentInProgressError(RuntimeError):
    """Raised by ``connect()`` when a consent flow is already running for this service — the
    route's 409 (a second POST must never start a second browser flow)."""


class GoogleServiceConnection:
    """One service's ('gmail' or 'gcal') OAuth status probe + background consent trigger
    (DEC-50).

    ``configured`` is resolved ONCE, at construction (whether
    ``GOOGLE_OAUTH_CLIENT_ID``/``GOOGLE_OAUTH_CLIENT_SECRET`` were present) — never re-checked
    per call. ``auth_factory()`` must return a fresh object exposing ``get_credentials()`` (a
    ``GmailAuth``/``CalendarAuth`` instance in production, reused VERBATIM); it is called both by
    the status probe (when a token is already stored) and by the consent trigger.
    """

    def __init__(
        self,
        *,
        configured: bool,
        token_store: GoogleTokenStoreLike,
        auth_factory: Callable[[], GoogleAuthLike],
    ) -> None:
        self._configured = configured
        self._token_store = token_store
        self._auth_factory = auth_factory
        self._lock = threading.Lock()
        self._consent_state: ConsentState = "idle"
        self._consent_error: str | None = None

    def status(self) -> dict[str, object]:
        """The honest, non-crashing status probe (DEC-49 posture) — see the module docstring.
        Never raises. The ``"error"`` key is present only while ``consent`` is ``"error"``."""
        with self._lock:
            consent_state: ConsentState = self._consent_state
            consent_error = self._consent_error
        payload: dict[str, object] = {
            "status": self._connection_status(),
            "consent": consent_state,
        }
        if consent_error is not None:
            payload["error"] = consent_error
        return payload

    def _connection_status(self) -> ConnectionStatus:
        if not self._configured:
            return "not_configured"
        if self._token_store.load() is None:
            # NOTHING further is probed — get_credentials() would start the interactive consent
            # flow here, which this probe must never trigger (DEC-49).
            return "not_connected"
        try:
            self._auth_factory().get_credentials()
        except Exception:
            return "expired"
        return "connected"

    def connect(self) -> None:
        """Trigger the (possibly interactive) consent flow on a background daemon thread.

        Returns immediately. Raises ``ConsentInProgressError`` if a consent flow is already
        running for this service — the caller (the route) turns that into a 409.
        """
        with self._lock:
            if self._consent_state == "in_progress":
                raise ConsentInProgressError(
                    "a Google consent flow is already running for this service"
                )
            self._consent_state = "in_progress"
            self._consent_error = None

        threading.Thread(target=self._run_consent, daemon=True).start()

    def _run_consent(self) -> None:
        try:
            self._auth_factory().get_credentials()
        except Exception as exc:
            logger.warning("google consent flow failed: %s", exc)
            with self._lock:
                self._consent_state = "error"
                self._consent_error = str(exc)
            return
        with self._lock:
            self._consent_state = "idle"
            self._consent_error = None


class GoogleConnectionManager:
    """Holds one ``GoogleServiceConnection`` per service ('gmail', 'gcal') — the object
    ``settings_app.api``'s ``GET /google/status``/``POST /google/{service}/connect`` routes
    drive."""

    def __init__(self, connections: dict[ServiceName, GoogleServiceConnection]) -> None:
        self._connections = dict(connections)

    def get(self, service: ServiceName) -> GoogleServiceConnection:
        return self._connections[service]

    def status(self) -> dict[ServiceName, dict[str, object]]:
        return {service: conn.status() for service, conn in self._connections.items()}


__all__ = [
    "GOOGLE_SERVICES",
    "ConnectionStatus",
    "ConsentInProgressError",
    "ConsentState",
    "GoogleAuthLike",
    "GoogleConnectionManager",
    "GoogleServiceConnection",
    "GoogleTokenStoreLike",
    "ServiceName",
]
