"""``python -m wombat.settings_app`` — the loopback-only settings API process (TK-197, EP-32,
DEC-31/32).

Handshake (DEC-31): pre-bind a socket at ``(BIND_HOST, 0)`` so the OS picks an ephemeral port,
print EXACTLY ONE machine-readable JSON line ``{"port": ..., "token": ...}`` to stdout (flushed)
for the Electron main process (TK-199) to read, THEN serve on that pre-bound socket. Every
request must carry that per-launch token via the ``X-Wombat-Token`` header (``api.create_app``)
— anything else is a 401.

``WOMBAT_PG_DSN`` (TK-242, DEC-43) is resolved directly from the process environment, falling
back to a cwd-relative ``.env`` (mirroring ``WombatConfig``'s own ``env_file=".env"``) — NEVER via
``load_config()``, which would additionally require the DeepSeek env vars this app has no
business needing. Absent a usable DSN, the app is built in the read-only degrade posture
(``api.create_app`` with ``store=None``) rather than failing to boot — the TK-197
runs-while-``serve()``-is-down property now rides Postgres being a separate always-up service
rather than this process ever needing ``wombat.bootstrap``/``wombat.runtime``.

Imports NOTHING from ``wombat.bootstrap``/``wombat.runtime`` (this process runs while ``serve()``
is down) — ``wombat.config``, ``wombat.settings_store``, ``wombat.external_store`` (TK-246), and
``wombat.voice.key_store`` are the only wombat modules touched, none of which reaches the runtime.
"""

from __future__ import annotations

import json
import os
import secrets
import socket

import psycopg
import uvicorn
from dotenv import dotenv_values
from pydantic import SecretStr

from wombat.config import WombatConfig
from wombat.external_store import ExternalItemStore
from wombat.integrations.gcal.auth import CalendarAuth
from wombat.integrations.gcal.token_store import KeyringTokenStore
from wombat.integrations.gmail.auth import GmailAuth
from wombat.integrations.gmail.token_store import GMAIL_KEYRING_ACCOUNT
from wombat.settings_app.api import BIND_HOST, create_app
from wombat.settings_app.google_connect import GoogleConnectionManager, GoogleServiceConnection
from wombat.settings_store import SettingsStore, ensure_schema, import_legacy_settings_file
from wombat.voice.key_store import WOMBAT_KEYRING_SERVICE, KeyringVoiceKeyStore

# TK-201 (Q-111(d)): a test/ops override for the keyring service name, so the Playwright smoke
# (and any future throwaway run) can point at a disposable service instead of the real
# "wombat" vault entry. Unset/blank -> KeyringVoiceKeyStore's own default (byte-identical to
# pre-TK-201 behavior).
_KEYRING_SERVICE_ENV_VAR = "WOMBAT_KEYRING_SERVICE"

_PG_DSN_ENV_VAR = "WOMBAT_PG_DSN"

_GOOGLE_CLIENT_ID_ENV_VAR = "GOOGLE_OAUTH_CLIENT_ID"
_GOOGLE_CLIENT_SECRET_ENV_VAR = "GOOGLE_OAUTH_CLIENT_SECRET"

# TK-256 (DEC-50): settings_app never needs the DeepSeek egress, but WombatConfig requires
# ``deepseek_api_key``/``deepseek_base_url``. This sentinel constructs GmailAuth/CalendarAuth's
# degenerate WombatConfig below — it is never read, never logged, never sent anywhere.
_UNUSED_DEEPSEEK_SENTINEL = "settings-app-unused"


def _resolve_pg_dsn() -> str | None:
    """``WOMBAT_PG_DSN`` from the process environment, else the same var in a cwd-relative
    ``.env`` — deliberately NOT ``load_config()`` (TK-242: no DeepSeek vars needed here)."""
    from_env = os.environ.get(_PG_DSN_ENV_VAR)
    if from_env:
        return from_env
    return dotenv_values(".env").get(_PG_DSN_ENV_VAR) or None


def _env_or_dotenv(var: str, dotenv: dict[str, str | None]) -> str:
    """``var`` from the process environment if the key is PRESENT there at all (an explicit
    empty-string override wins, never falling through to ``.env`` — the precedent
    ``tests/conftest.py``'s hermetic Google-creds strip depends on, mirroring how
    pydantic-settings' env source outranks its dotenv source regardless of value), else the same
    var read from ``dotenv`` (a pre-loaded ``dotenv_values(".env")`` mapping)."""
    if var in os.environ:
        return os.environ[var].strip()
    return (dotenv.get(var) or "").strip()


def _resolve_google_oauth_credentials() -> tuple[str, str] | None:
    """``GOOGLE_OAUTH_CLIENT_ID``/``GOOGLE_OAUTH_CLIENT_SECRET`` from the process environment,
    else the same vars in a cwd-relative ``.env`` — mirrors ``_resolve_pg_dsn`` (TK-242), with
    env-presence (not truthiness) outranking dotenv per ``_env_or_dotenv`` above. Both must
    resolve non-blank; ``None`` means DEC-50's in-app Google connect stays in its
    not_configured degrade for every service."""
    dotenv = dotenv_values(".env")
    client_id = _env_or_dotenv(_GOOGLE_CLIENT_ID_ENV_VAR, dotenv)
    client_secret = _env_or_dotenv(_GOOGLE_CLIENT_SECRET_ENV_VAR, dotenv)
    if client_id and client_secret:
        return client_id, client_secret
    return None


def _build_google_connections() -> GoogleConnectionManager:
    """The DEC-50 in-app Google connection manager — GmailAuth/CalendarAuth are REUSED VERBATIM
    (never modified), constructed over a directly-built ``WombatConfig`` carrying only the
    resolved Google OAuth credentials (the DeepSeek fields take
    ``_UNUSED_DEEPSEEK_SENTINEL``, unused by either auth class). Each service's ``configured``
    flag is resolved once, here, from ``_resolve_google_oauth_credentials()``."""
    credentials = _resolve_google_oauth_credentials()
    config = WombatConfig(
        deepseek_api_key=SecretStr(_UNUSED_DEEPSEEK_SENTINEL),
        deepseek_base_url=_UNUSED_DEEPSEEK_SENTINEL,
        google_oauth_client_id=credentials[0] if credentials else None,
        google_oauth_client_secret=SecretStr(credentials[1]) if credentials else None,
    )
    gmail_token_store = KeyringTokenStore(account=GMAIL_KEYRING_ACCOUNT)
    gcal_token_store = KeyringTokenStore()
    return GoogleConnectionManager(
        {
            "gmail": GoogleServiceConnection(
                configured=credentials is not None,
                token_store=gmail_token_store,
                auth_factory=lambda: GmailAuth(config=config, token_store=gmail_token_store),
            ),
            "gcal": GoogleServiceConnection(
                configured=credentials is not None,
                token_store=gcal_token_store,
                auth_factory=lambda: CalendarAuth(config=config, token_store=gcal_token_store),
            ),
        }
    )


def main() -> None:
    token = secrets.token_urlsafe(32)
    service = os.environ.get(_KEYRING_SERVICE_ENV_VAR) or WOMBAT_KEYRING_SERVICE

    dsn = _resolve_pg_dsn()
    store: SettingsStore | None = None
    external_store: ExternalItemStore | None = None
    if dsn:
        conn = psycopg.connect(dsn)
        try:
            ensure_schema(conn)
        finally:
            conn.close()
        # TK-242, DEC-44: the SECOND and LAST production call site (the first is
        # ``wombat.runtime.serve()``) — invoked exactly once at startup, after schema is applied.
        import_legacy_settings_file(dsn)
        store = SettingsStore(dsn)
        # TK-246 (DEC-45(e)): the SAME resolved DSN, read-only over wombat_external_items — its
        # schema is ensured by the runtime side (schema_preflight.ensure_all_schemas); a table
        # that doesn't exist yet degrades a read the same as any other storage failure.
        external_store = ExternalItemStore(dsn)

    google_connections = _build_google_connections()
    app = create_app(
        store, KeyringVoiceKeyStore(service=service), token, external_store, google_connections
    )

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((BIND_HOST, 0))
    sock.listen(socket.SOMAXCONN)
    port = sock.getsockname()[1]

    # The ONE machine-readable line the Electron main process reads (TK-199) — nothing else may
    # precede it on stdout.
    print(json.dumps({"port": port, "token": token}), flush=True)

    config = uvicorn.Config(app, host=BIND_HOST, port=port, log_level="warning")
    server = uvicorn.Server(config)
    server.run(sockets=[sock])


if __name__ == "__main__":
    main()
