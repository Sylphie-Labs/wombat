"""``python -m wombat.settings_app`` — the loopback-only settings API process (TK-197, EP-32,
DEC-31/32).

Handshake (DEC-31): pre-bind a socket at ``(BIND_HOST, 0)`` so the OS picks an ephemeral port,
print EXACTLY ONE machine-readable JSON line ``{"port": ..., "token": ...}`` to stdout (flushed)
for the Electron main process (TK-199) to read, THEN serve on that pre-bound socket. Every
request must carry that per-launch token via the ``X-Wombat-Token`` header (``api.create_app``)
— anything else is a 401.

Imports NOTHING from ``wombat.bootstrap``/``wombat.runtime`` (this process runs while ``serve()``
is down) — ``wombat.config`` and ``wombat.voice.key_store`` are the only wombat modules touched,
neither of which reaches the runtime.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
from pathlib import Path

import uvicorn

from wombat.config import WOMBAT_SETTINGS_FILE
from wombat.settings_app.api import BIND_HOST, create_app
from wombat.voice.key_store import WOMBAT_KEYRING_SERVICE, KeyringVoiceKeyStore

# TK-201 (Q-111(d)): a test/ops override for the keyring service name, so the Playwright smoke
# (and any future throwaway run) can point at a disposable service instead of the real
# "wombat" vault entry. Unset/blank -> KeyringVoiceKeyStore's own default (byte-identical to
# pre-TK-201 behavior).
_KEYRING_SERVICE_ENV_VAR = "WOMBAT_KEYRING_SERVICE"


def main() -> None:
    token = secrets.token_urlsafe(32)
    service = os.environ.get(_KEYRING_SERVICE_ENV_VAR) or WOMBAT_KEYRING_SERVICE
    app = create_app(Path(WOMBAT_SETTINGS_FILE), KeyringVoiceKeyStore(service=service), token)

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
