"""wombat.chat.surface — ChatReplyBroker + ChatSurface: the loopback chat transport (TK-222,
EP-32, Q-110(d)).

Q-110(d) RULING (binding — build EXACTLY this shape):

``ChatReplyBroker`` is the reply-correlation seam: a plain ``dict[str, asyncio.Future[str]]``
keyed by the pre-computed item id. ``register(item_id)`` MUST be called BEFORE the corresponding
message is pushed onto the ``ChatSource`` (else a same-tick resolve could race the registration
and be silently dropped); ``resolve(item_id, text)`` on an unknown/already-discarded id is a
NO-OP — chat_reply's own guarded-never-raise call is never a way to crash the drain spine, and a
non-chat item routed through ``chat_reply`` (every ``compose``-composed item now hops through it)
harmlessly resolves nothing.

``ChatSurface`` is a STDLIB-ONLY minimal HTTP/1.1 transport over ``asyncio.start_server`` — ZERO
new runtime dependency (no fastapi/uvicorn; those are the settings_app's, TK-197, which runs as a
SEPARATE process while ``serve()`` is down). It runs ON the runtime's own event loop, bound to
``127.0.0.1`` at an OS-assigned ephemeral port (CON-7: the residency guard's structural
on-host boundary — chat never listens beyond loopback). Auth is the ``X-Wombat-Chat-Token``
header, checked against a per-launch ``secrets.token_urlsafe(32)`` token minted by the composition
root (``bootstrap.assemble_runtime``); missing/wrong is a 401, mirroring TK-197's settings-app
discipline. ``OPTIONS /chat`` is answered without an auth check (a CORS preflight never carries
the app's custom headers) with ``Access-Control-Allow-Origin: *`` + the ``Content-Type``/
``X-Wombat-Chat-Token`` headers admitted — every OTHER response also carries the ``Allow-Origin``
header so the renderer's actual fetch (not just its preflight) can read the body.

``POST /chat`` body is ``{"text": <str>}``. On a tokened, well-formed request: mint
``event_key = uuid4().hex``, build the ``SourceEvent`` payload ``{"item_kind": "chat", "text":
<message>, "received_at": <UTC ISO>}`` — NO correlation id ever enters the payload (CON-1: the
mouth never sees one) — PRE-COMPUTE the expected ``item_id`` via the SAME canonical
``idempotency_key(source.id, event_key)`` derivation ``sources.registry.SourceRegistry`` uses at
enqueue time, register a broker future under that id, THEN push the event onto the injected
``ChatSource``. This ordering — register before push — plus reading ``source.id`` off the live
instance (never a hardcoded ``"chat"`` literal) are what make the pre-computed id and the
registry-derived id agree.

The held connection then awaits that future up to ``CHAT_REPLY_TIMEOUT_SECONDS`` (30.0): a
resolve within the window answers ``{"status": "replied", "text": <composed text>}``; a timeout
answers ``{"status": "held"}`` — an HONEST gate-held/slow answer, never a hang (the gate's
hold-authority stands; chat never bypasses it) — and discards the now-abandoned registration so a
late resolve becomes the documented unknown-id no-op rather than leaking a completed future.

STRUCTURAL (CON-1): this module imports NOTHING model/compose/mouth-shaped — only stdlib,
``wombat.domain.item_identity``, and ``wombat.sources.{base,chat_source}``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from wombat.domain.item_identity import idempotency_key
from wombat.sources.base import SourceEvent
from wombat.sources.chat_source import ChatSource

logger = logging.getLogger(__name__)

# The reply-await bound (Q-110(d) ruling 4): a resolve within this window answers "replied"; a
# timeout answers "held" rather than hanging the connection.
CHAT_REPLY_TIMEOUT_SECONDS = 30.0

# Defensive bounds on the hand-rolled HTTP parse below — this is a loopback, token-gated surface
# (CON-7), but a malformed/adversarial connection must still never hang or exhaust memory.
_MAX_HEADER_LINES = 64
_MAX_BODY_BYTES = 1_048_576  # 1 MiB

_REASON_PHRASES: dict[int, str] = {
    200: "OK",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
}

_CORS_HEADERS = (
    "Access-Control-Allow-Origin: *",
    "Access-Control-Allow-Methods: POST, OPTIONS",
    "Access-Control-Allow-Headers: Content-Type, X-Wombat-Chat-Token",
)


class ChatReplyBroker:
    """Correlates a chat item's composed reply back to the HTTP connection that submitted it
    (Q-110(d) ruling 4). ``register`` MUST be called before the matching ``ChatSource.push`` —
    see the module docstring."""

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[str]] = {}

    def register(self, item_id: str) -> asyncio.Future[str]:
        """Create and store a fresh, unresolved future for ``item_id`` on the CURRENT running
        loop. Must be called from within the loop the surface serves on (the same single-loop
        discipline every ``PushSource`` push relies on)."""
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending[item_id] = future
        return future

    def resolve(self, item_id: str, text: str) -> None:
        """Deliver ``text`` to the connection waiting on ``item_id``. A NO-OP for an unknown id
        (never registered, already resolved, or already discarded after a timeout) — this is the
        guard that lets ``chat_reply`` call this unconditionally for every composed item, chat or
        not, and never raise for one it doesn't recognize."""
        future = self._pending.pop(item_id, None)
        if future is not None and not future.done():
            future.set_result(text)

    def discard(self, item_id: str) -> None:
        """Drop a pending registration without resolving it (the timeout path) — a LATE resolve
        for this id afterward is then the documented unknown-id no-op."""
        self._pending.pop(item_id, None)


def _parse_chat_text(body: bytes) -> str | None:
    """Decode+validate a ``POST /chat`` body: ``{"text": <non-blank str>}``. ``None`` on any
    malformed shape — never raises."""
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    text = parsed.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return text


async def _read_request_headers(reader: asyncio.StreamReader) -> dict[str, str] | None:
    """Read HTTP header lines up to the blank-line terminator. ``None`` on EOF before completion,
    a header line with no ``:``, or more than ``_MAX_HEADER_LINES`` lines (malformed/adversarial
    input) — the caller answers 400 rather than hanging or growing unbounded."""
    headers: dict[str, str] = {}
    for _ in range(_MAX_HEADER_LINES):
        line = await reader.readline()
        if not line:
            return None  # EOF before the blank-line terminator
        if line in (b"\r\n", b"\n"):
            return headers
        name, sep, value = line.decode("latin-1").partition(":")
        if not sep:
            return None
        headers[name.strip().lower()] = value.strip()
    return None


class ChatSurface:
    """The loopback-only, token-guarded chat HTTP transport (Q-110(d) ruling 4). Runs ON the
    caller's event loop — ``start()``/``stop()`` bracket an ``asyncio.start_server`` bound to
    ``127.0.0.1`` at an OS-assigned ephemeral port."""

    def __init__(
        self,
        *,
        source: ChatSource,
        broker: ChatReplyBroker,
        token: str,
        handshake_path: Path,
        host: str = "127.0.0.1",
        reply_timeout_seconds: float = CHAT_REPLY_TIMEOUT_SECONDS,
    ) -> None:
        self._source = source
        self._broker = broker
        self._token = token
        # Read by the caller (wombat.runtime) to write the per-launch handshake file — this
        # surface itself never touches the filesystem (TK-222 ruling 5: "the runtime writes").
        self.handshake_path = handshake_path
        self._host = host
        self._reply_timeout_seconds = reply_timeout_seconds
        self._server: asyncio.Server | None = None

    @property
    def token(self) -> str:
        return self._token

    @property
    def address(self) -> tuple[str, int]:
        """The ACTUAL bound ``(host, port)`` read off the live socket — proves the bind is really
        loopback rather than trusting the configured host string. Raises ``RuntimeError`` before
        ``start()``."""
        if self._server is None or not self._server.sockets:
            msg = "ChatSurface.address: the surface has not started yet"
            raise RuntimeError(msg)
        sockname = self._server.sockets[0].getsockname()
        return str(sockname[0]), int(sockname[1])

    @property
    def port(self) -> int:
        return self.address[1]

    async def start(self) -> None:
        """Bind and start serving. Raises on a bind failure — the caller (``wombat.runtime``)
        guards this call with the CON-3 loud-WARN degrade (TK-222 ruling 5)."""
        self._server = await asyncio.start_server(self._handle_connection, host=self._host, port=0)

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
                await self._respond(writer, 400, {"error": "bad_request"})
                return
            method, path, _version = parts

            headers = await _read_request_headers(reader)
            if headers is None:
                await self._respond(writer, 400, {"error": "bad_request"})
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
            logger.warning("chat surface: connection handling failed", exc_info=True)
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _dispatch(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        writer: asyncio.StreamWriter,
    ) -> None:
        if method == "OPTIONS" and path == "/chat":
            # A CORS preflight never carries the app's custom headers — answered token-free.
            await self._respond(writer, 204, None)
            return
        if method != "POST" or path != "/chat":
            await self._respond(writer, 404, {"error": "not_found"})
            return
        if headers.get("x-wombat-chat-token") != self._token:
            await self._respond(writer, 401, {"error": "unauthorized"})
            return

        text = _parse_chat_text(body)
        if text is None:
            await self._respond(writer, 400, {"error": "bad_request"})
            return

        await self._accept_message(text, writer)

    async def _accept_message(self, text: str, writer: asyncio.StreamWriter) -> None:
        """The ONE egress point for an accepted message: register a broker future, THEN push onto
        the ``ChatSource`` (register-before-push, Q-110(d) ruling 4) — no other queue/enqueue
        touch happens here or anywhere else in this module."""
        event_key = uuid4().hex
        payload = {
            "item_kind": "chat",
            "text": text,
            "received_at": datetime.now(UTC).isoformat(),
        }
        item_id = idempotency_key(self._source.id, event_key)

        future = self._broker.register(item_id)
        self._source.push(SourceEvent(event_key=event_key, payload=payload))

        try:
            reply_text = await asyncio.wait_for(future, timeout=self._reply_timeout_seconds)
        except TimeoutError:
            self._broker.discard(item_id)
            await self._respond(writer, 200, {"status": "held"})
            return

        await self._respond(writer, 200, {"status": "replied", "text": reply_text})

    async def _respond(
        self, writer: asyncio.StreamWriter, status: int, payload: dict[str, object] | None
    ) -> None:
        reason = _REASON_PHRASES.get(status, "OK")
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        header_lines = [
            f"HTTP/1.1 {status} {reason}",
            "Content-Type: application/json",
            f"Content-Length: {len(body)}",
            "Connection: close",
            *_CORS_HEADERS,
            "",
            "",
        ]
        writer.write("\r\n".join(header_lines).encode("latin-1") + body)
        await writer.drain()


__all__ = ["CHAT_REPLY_TIMEOUT_SECONDS", "ChatReplyBroker", "ChatSurface"]
