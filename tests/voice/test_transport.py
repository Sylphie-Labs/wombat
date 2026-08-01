"""TK-330 acceptance criteria — ``StreamingVoiceTransport`` additive protocol extension +
``HttpxVoiceTransport.stream`` (DEC-73b).

All tests ride a LOCAL scripted HTTP server (stdlib ``http.server.ThreadingHTTPServer``,
threaded, ``127.0.0.1``, ephemeral port) — never a live network call (DEF-7). Chunk emission is
lock-step-paced through a ``threading.Event`` the test signals AFTER each read, so ordering
assertions read a shared emission log rather than racing wall-clock sleeps.

AC1 (ordering, byte-identical, no sleeps): ``test_stream_yields_scripted_chunks_in_order``
(parametrized over slow-dribble/one-chunk/empty scripted sequences) asserts the FIRST chunk is
yielded to the caller before the server's emission log shows the LAST chunk emitted.
AC2 (failure semantics): ``test_stream_non2xx_raises_before_any_chunk_delivered`` (zero chunks
on a non-2xx status) and ``test_stream_mid_disconnect_raises_after_exactly_k_chunks`` (a
connection death partway raises after exactly the chunks already delivered).
AC3 (additive / byte-untouched): ``test_voice_transport_post_signature_unchanged_and_still_
satisfied_by_existing_fakes`` proves ``VoiceTransport``/``post`` are untouched and a
``post``-only fake is NOT a ``StreamingVoiceTransport`` (structural, ``runtime_checkable``);
existing ``post``-based suites elsewhere in the repo are left unmodified, per the ticket brief.
"""

from __future__ import annotations

import http.server
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from wombat.voice.transport import (
    HttpxVoiceTransport,
    StreamingVoiceTransport,
    VoiceTransport,
)

# ``VoiceTransportError`` is deliberately NOT imported at module level: alphabetically-earlier
# tests (test_stt_deepgram.py, test_stt_providers.py) reload ``wombat.voice.transport``,
# rebinding the exception class in the shared module dict. A module-level import here would
# freeze the pre-reload identity, which would no longer match what ``HttpxVoiceTransport.stream``
# actually raises post-reload. The two tests that need it re-import locally, at call time.

_ERROR_BODY = b"scripted non-2xx body"


class _ScriptedHandler(http.server.BaseHTTPRequestHandler):
    """Emits the chunk sequence configured on ``self.server`` (a ``_ScriptedServer``), pacing
    each write against a shared ``threading.Event`` so the test controls exactly when the next
    chunk lands on the wire — deterministic ordering, never a wall-clock sleep."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass  # silence the default stderr access log

    def do_POST(self) -> None:
        server: _ScriptedServer = self.server  # type: ignore[assignment]
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length:
            self.rfile.read(content_length)

        if not (200 <= server.status_code < 300):
            self.send_response(server.status_code)
            self.send_header("Content-Length", str(len(_ERROR_BODY)))
            self.end_headers()
            self.wfile.write(_ERROR_BODY)
            server.emission_log.append("error-sent")
            server.error_sent_event.set()
            return

        total_length = sum(len(chunk) for chunk in server.chunks)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(total_length))
        self.end_headers()

        for index, chunk in enumerate(server.chunks):
            if server.disconnect_after is not None and index == server.disconnect_after:
                server.emission_log.append(f"disconnect@{index}")
                self.close_connection = True
                return
            self.wfile.write(chunk)
            self.wfile.flush()
            server.emission_log.append(f"chunk@{index}")
            server.step_event.wait(timeout=5)  # safety valve only, not an assertion
            server.step_event.clear()


class _ScriptedServer(http.server.ThreadingHTTPServer):
    """A local scripted HTTP server bound to an ephemeral ``127.0.0.1`` port for exactly ONE
    test's stream. ``step_event`` gates chunk-by-chunk emission; ``emission_log`` is the
    ground truth the test asserts ordering against."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        *,
        status_code: int = 200,
        chunks: list[bytes] | None = None,
        disconnect_after: int | None = None,
    ) -> None:
        super().__init__(("127.0.0.1", 0), _ScriptedHandler)
        self.status_code = status_code
        self.chunks = chunks or []
        self.disconnect_after = disconnect_after
        self.emission_log: list[str] = []
        self.step_event = threading.Event()
        # TK-330 AC4 (ISS-39 f2): a dedicated event for the non-2xx-error path, set AFTER the
        # handler thread appends "error-sent" -- the client thread raises VoiceTransportError as
        # soon as it reads the response status line, which can otherwise race the handler's own
        # append to emission_log. The test waits on this event before asserting the log.
        self.error_sent_event = threading.Event()

    @property
    def url(self) -> str:
        port = int(self.server_address[1])
        return f"http://127.0.0.1:{port}/stream"


class _ScriptedServerContext:
    """Starts/stops a ``_ScriptedServer`` on a daemon thread for the duration of a ``with``
    block — the test's own private server, never touching any live process."""

    def __init__(self, **kwargs: Any) -> None:
        self._server = _ScriptedServer(**kwargs)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> _ScriptedServer:
        self._thread.start()
        return self._server

    def __exit__(self, *exc_info: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _drain_paced(server: _ScriptedServer, stream: Iterator[bytes]) -> list[bytes]:
    """Pull every chunk from ``stream`` one at a time, signalling ``server.step_event`` after
    each so the server proceeds to its next scripted write only once this test has already
    consumed the previous one (AC1's deterministic ordering)."""
    received: list[bytes] = []
    for chunk in stream:
        received.append(chunk)
        server.step_event.set()
    return received


@pytest.mark.parametrize(
    "chunks",
    [
        pytest.param([b"one"], id="one-chunk"),
        pytest.param([b"al", b"pha", b"beta", b"gamma"], id="slow-dribble"),
        pytest.param([], id="empty"),
    ],
)
def test_stream_yields_scripted_chunks_in_order(chunks: list[bytes]) -> None:
    with _ScriptedServerContext(chunks=chunks) as server:
        transport = HttpxVoiceTransport()
        stream = transport.stream(server.url, headers={"X-Test": "1"}, json={"ok": True})

        received: list[bytes] = []
        for index, chunk in enumerate(stream):
            if index == 0 and len(chunks) > 1:
                # AC1: the first chunk reached the caller before the server's log shows the
                # LAST scripted chunk emitted — an ordering assert against the shared log, no
                # sleeps involved. (Only meaningful when there IS a distinct last chunk.)
                assert f"chunk@{len(chunks) - 1}" not in server.emission_log
            received.append(chunk)
            server.step_event.set()

        assert received == chunks


def test_stream_non2xx_raises_before_any_chunk_delivered() -> None:
    # ``VoiceTransportError`` is re-imported here (not the module-level binding) because
    # alphabetically-earlier tests (test_stt_deepgram.py, test_stt_providers.py) reload
    # ``wombat.voice.transport``, rebinding the exception class in the shared module dict; a
    # frozen module-level reference would no longer match what ``HttpxVoiceTransport.stream``
    # actually raises post-reload. See tests/voice/test_stt_providers.py:41-47 for the same hazard.
    from wombat.voice.transport import VoiceTransportError

    with _ScriptedServerContext(status_code=500, chunks=[b"never", b"delivered"]) as server:
        transport = HttpxVoiceTransport()
        stream = transport.stream(server.url, headers={}, json=None)

        received: list[bytes] = []
        with pytest.raises(VoiceTransportError):
            for chunk in stream:
                received.append(chunk)

        # TK-330 AC4 (ISS-39 f2): wait for the handler thread's own append to actually land before
        # asserting the log -- the client can raise as soon as it reads the status line, which
        # otherwise races the handler thread's post-write emission_log.append("error-sent").
        assert server.error_sent_event.wait(timeout=5)
        assert received == []
        assert server.emission_log == ["error-sent"]


def test_stream_mid_disconnect_raises_after_exactly_k_chunks() -> None:
    # See comment in test_stream_non2xx_raises_before_any_chunk_delivered above: re-import at
    # call time to always compare against the CURRENT (possibly reloaded) exception class.
    from wombat.voice.transport import VoiceTransportError

    chunks = [b"first", b"second", b"third", b"fourth"]
    with _ScriptedServerContext(chunks=chunks, disconnect_after=2) as server:
        transport = HttpxVoiceTransport()
        stream = transport.stream(server.url, headers={}, json=None)

        received: list[bytes] = []
        with pytest.raises(VoiceTransportError):
            for chunk in stream:
                received.append(chunk)
                server.step_event.set()

        assert received == chunks[:2]


class _PostOnlyFakeTransport:
    """An existing-style fake implementing ONLY ``post`` — proves ``VoiceTransport`` stayed
    byte-untouched (AC3): this fake was never edited for TK-330 and still satisfies the
    original protocol, but structurally does NOT satisfy the new ``StreamingVoiceTransport``."""

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        content: bytes | None = None,
        json: dict[str, object] | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes]] | None = None,
    ) -> tuple[int, bytes]:
        return 200, b"unchanged"


def test_voice_transport_post_signature_unchanged_and_still_satisfied_by_existing_fakes() -> None:
    fake = _PostOnlyFakeTransport()
    assert isinstance(fake, VoiceTransport)
    assert not isinstance(fake, StreamingVoiceTransport)
    assert fake.post("https://example.invalid", headers={}) == (200, b"unchanged")


def test_httpx_voice_transport_satisfies_streaming_voice_transport_protocol() -> None:
    assert isinstance(HttpxVoiceTransport(), StreamingVoiceTransport)
