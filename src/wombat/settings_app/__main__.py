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

from wombat.external_store import ExternalItemStore
from wombat.settings_app.api import BIND_HOST, create_app
from wombat.settings_store import SettingsStore, ensure_schema, import_legacy_settings_file
from wombat.voice.key_store import WOMBAT_KEYRING_SERVICE, KeyringVoiceKeyStore

# TK-201 (Q-111(d)): a test/ops override for the keyring service name, so the Playwright smoke
# (and any future throwaway run) can point at a disposable service instead of the real
# "wombat" vault entry. Unset/blank -> KeyringVoiceKeyStore's own default (byte-identical to
# pre-TK-201 behavior).
_KEYRING_SERVICE_ENV_VAR = "WOMBAT_KEYRING_SERVICE"

_PG_DSN_ENV_VAR = "WOMBAT_PG_DSN"


def _resolve_pg_dsn() -> str | None:
    """``WOMBAT_PG_DSN`` from the process environment, else the same var in a cwd-relative
    ``.env`` — deliberately NOT ``load_config()`` (TK-242: no DeepSeek vars needed here)."""
    from_env = os.environ.get(_PG_DSN_ENV_VAR)
    if from_env:
        return from_env
    return dotenv_values(".env").get(_PG_DSN_ENV_VAR) or None


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

    app = create_app(store, KeyringVoiceKeyStore(service=service), token, external_store)

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
