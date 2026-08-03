"""wombat.devices.surface — DeviceSurface: the consent-gated LAN listener (TK-339, DEC-78),
wombat's FIRST inbound socket off loopback.

Built on the ``chat.surface.ChatSurface`` precedent (a stdlib-only ``asyncio.start_server``
HTTP/1.1 transport, zero new runtime dependency) with three deliberate DIVERGENCES, each a
ruling (DEC-78(a)):

  1. The bind host is an EXPLICIT config field (``config.wombat_device_bind_host``) defaulting
     to ``127.0.0.1`` — an unconfigured wombat is byte-identical to today; reaching the LAN is an
     act, never a drift.
  2. The port is FIXED and configured (``config.wombat_device_port``), never the ephemeral
     port-0 both existing surfaces (chat, settings) use — a device paired yesterday must find
     wombat today.
  3. The token is PER-DEVICE (``devices.credentials.DeviceCredentialStore``, TK-338) rather than
     per-launch, because pairing must survive a restart.

Auth is the ``X-Wombat-Device-Token`` header, checked BEFORE any routing decision — DEC-78(b)'s
anti-enumeration rule is stronger here than ``ChatSurface``'s: 401 on every path, including an
unknown one, EVEN WITH a valid token that simply isn't followed by the one known route. A 404 is
never produced by this surface, ever.

This ticket ships EXACTLY ONE route — ``GET /v1/health`` — the authenticated liveness probe that
doubles as the DEC-83 payload-level FORMAT HANDSHAKE every device reads before anything else: the
audio sample rate/format (read from the ONE ``voice.stream_playback.STREAM_SAMPLE_RATE`` constant,
never re-declared), the staleness/TTL windows §2/§5 pin, and the two DEC-78(d) consent toggles AS
ACTUALLY CONSTRUCTED. TK-340/TK-341 add the two ingest POSTs; TK-343/TK-345 add the remaining GET
and the optional WebSocket — this module grows to hold them, per planning/design/wire-contract.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Sequence

from wombat.devices.credentials import DeviceCredentialStore
from wombat.voice.stream_playback import STREAM_SAMPLE_RATE

logger = logging.getLogger(__name__)

# DEC-83 §2/§5: devices read these windows off GET /v1/health rather than holding their own copy
# (the drift TK-359 exists to prevent). Pinned module constants — TK-340/TK-343 read the SAME
# names, never a second literal.
STALE_AUDIO_WINDOW_SECONDS = 120
UTTERANCE_TTL_SECONDS = 120

_AUTH_HEADER = "x-wombat-device-token"
_HEALTH_PATH = "/v1/health"

# Defensive bound on the hand-rolled HTTP parse below — this is wombat's first LAN-reachable
# listener (DEC-78), so a malformed/adversarial connection must never hang or exhaust memory even
# though this ticket's own route carries no body.
_MAX_HEADER_LINES = 64
_MAX_BODY_BYTES = 1_048_576  # 1 MiB — TK-340/TK-341 pin their own larger per-route caps.

_UNAUTHORIZED_BODY: dict[str, object] = {"error": "unauthorized"}
_BAD_REQUEST_BODY: dict[str, object] = {"error": "bad_request"}

_REASON_PHRASES: dict[int, str] = {200: "OK", 400: "Bad Request", 401: "Unauthorized"}


async def _read_request_headers(reader: asyncio.StreamReader) -> dict[str, str] | None:
    """Read HTTP header lines up to the blank-line terminator — mirrors
    ``chat.surface._read_request_headers`` verbatim. ``None`` on EOF before completion, a header
    line with no ``:``, or more than ``_MAX_HEADER_LINES`` lines."""
    headers: dict[str, str] = {}
    for _ in range(_MAX_HEADER_LINES):
        line = await reader.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            return headers
        name, sep, value = line.decode("latin-1").partition(":")
        if not sep:
            return None
        headers[name.strip().lower()] = value.strip()
    return None


class DeviceSurface:
    """The consent-gated, per-device-token-guarded LAN listener (DEC-78). Runs ON the caller's
    event loop — ``start()``/``stop()`` bracket an ``asyncio.start_server`` bound to a FIXED,
    configured ``(host, port)`` — never the ephemeral port-0 ``ChatSurface`` uses."""

    def __init__(
        self,
        *,
        credential_store: DeviceCredentialStore,
        host: str,
        port: int,
        remote_voice_enabled: bool,
        biometrics_enabled: bool,
    ) -> None:
        self._credential_store = credential_store
        self._host = host
        self._port = port
        self._remote_voice_enabled = remote_voice_enabled
        self._biometrics_enabled = biometrics_enabled
        self._server: asyncio.Server | None = None

    @property
    def address(self) -> tuple[str, int]:
        """The ACTUAL bound ``(host, port)`` read off the live socket — proves the bind is really
        what it claims rather than trusting the configured host string. Raises ``RuntimeError``
        before ``start()``."""
        if self._server is None or not self._server.sockets:
            msg = "DeviceSurface.address: the surface has not started yet"
            raise RuntimeError(msg)
        sockname = self._server.sockets[0].getsockname()
        return str(sockname[0]), int(sockname[1])

    async def start(self) -> None:
        """Bind and start serving on the FIXED, configured port (never port-0). Raises on a bind
        failure — the caller (``wombat.runtime``) guards this call with the CON-3 loud-WARN
        degrade, mirroring ``_start_chat_surface``'s posture exactly."""
        self._server = await asyncio.start_server(
            self._handle_connection, host=self._host, port=self._port
        )
        if self._host != "127.0.0.1":
            # DEC-78(a): a non-loopback bind must never be a silent state.
            logger.info(
                "device surface: bound to non-loopback host %s:%d — reachable from the LAN",
                self._host,
                self._port,
            )

    async def stop(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            parts = request_line.decode("latin-1").strip().split(" ")
            if len(parts) != 3:
                await self._respond(writer, 400, _BAD_REQUEST_BODY)
                return
            method, path, _version = parts

            headers = await _read_request_headers(reader)
            if headers is None:
                await self._respond(writer, 400, _BAD_REQUEST_BODY)
                return

            try:
                content_length = int(headers.get("content-length", "0") or "0")
            except ValueError:
                content_length = 0
            content_length = max(0, min(content_length, _MAX_BODY_BYTES))
            body = await reader.readexactly(content_length) if content_length else b""

            await self._dispatch(method, path, headers, body, writer)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass  # the client went away mid-request — nothing to answer
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("device surface: connection handling failed", exc_info=True)
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _dispatch(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,  # this ticket's one route carries no body; kept for the TK-340/TK-341
        # route handlers this dispatch grows to hold.
        writer: asyncio.StreamWriter,
    ) -> None:
        """DEC-78(b) anti-enumeration, STRONGER than ``ChatSurface``: the token is checked FIRST,
        before any routing decision, and an unrecognized method/path answers the SAME 401 as a
        missing/wrong token — never a 404, even with a token that verifies. Only exactly one
        (method, path) pair — ``(GET, /v1/health)`` — ever proceeds past this gate in this
        ticket."""
        device_id = self._credential_store.verify(headers.get(_AUTH_HEADER, ""))
        if device_id is None:
            await self._respond(writer, 401, _UNAUTHORIZED_BODY)
            return
        if method != "GET" or path != _HEALTH_PATH:
            await self._respond(writer, 401, _UNAUTHORIZED_BODY)
            return
        await self._handle_health(device_id, writer)

    async def _handle_health(self, device_id: str, writer: asyncio.StreamWriter) -> None:
        """``GET /v1/health`` (DEC-83 §4) — authenticated liveness AND the format handshake every
        device reads at pair time and on every reconnect."""
        await self._respond(
            writer,
            200,
            {
                "v": 1,
                "ok": True,
                "device_id": device_id,
                "audio": {
                    "sample_rate_hz": STREAM_SAMPLE_RATE,
                    "format": "pcm_s16le",
                    "channels": 1,
                },
                "stale_audio_window_seconds": STALE_AUDIO_WINDOW_SECONDS,
                "utterance_ttl_seconds": UTTERANCE_TTL_SECONDS,
                "capabilities": {
                    "remote_voice": self._remote_voice_enabled,
                    "biometrics": self._biometrics_enabled,
                    "stream": False,
                },
            },
        )

    async def _respond(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        payload: dict[str, object] | Sequence[object],
    ) -> None:
        reason = _REASON_PHRASES.get(status, "OK")
        body = json.dumps(payload).encode("utf-8")
        header_lines = [
            f"HTTP/1.1 {status} {reason}",
            "Content-Type: application/json",
            f"Content-Length: {len(body)}",
            "Connection: close",
            "",
            "",
        ]
        writer.write("\r\n".join(header_lines).encode("latin-1") + body)
        await writer.drain()


__all__ = [
    "STALE_AUDIO_WINDOW_SECONDS",
    "UTTERANCE_TTL_SECONDS",
    "DeviceSurface",
]
