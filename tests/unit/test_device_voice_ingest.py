"""tests/unit/test_device_voice_ingest.py — TK-340 acceptance criteria — ``POST /v1/voice``
audio ingest (``devices.voice_ingest.VoiceIngestHandler``) into a REMOTE-origin drop directory a
SECOND ``ASRSource`` (``sources.bootstrap.RemoteASRSource``, id ``"asr_remote"``) watches.

ALL socket-level tests are pure-asyncio: a REAL ``DeviceSurface`` driven by a hand-rolled minimal
HTTP client (mirrors ``tests/unit/test_device_surface.py``'s own client) — no Postgres, no real
network beyond loopback, no faster-whisper (a fake ``Transcriber`` stands in, mirroring
``tests/sources/test_asr.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wombat.devices import surface as device_surface_module
from wombat.devices.credentials import DeviceCredentialStore
from wombat.devices.surface import STALE_AUDIO_WINDOW_SECONDS, DeviceSurface
from wombat.devices.voice_ingest import VoiceIngestHandler
from wombat.domain.item_identity import idempotency_key
from wombat.sources.bootstrap import RemoteASRSource

_NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


class _FakeDeviceVault:
    """Mirrors ``tests/unit/test_device_surface.py``'s own fake — unit tests never touch the
    real keyring."""

    def __init__(self) -> None:
        self._blob: str | None = None

    def load(self) -> str | None:
        return self._blob

    def save(self, blob: str) -> None:
        self._blob = blob

    def clear(self) -> None:
        self._blob = None


class _FakeTranscriber:
    def __init__(self, text: str = "hello wombat") -> None:
        self.text = text
        self.calls: list[Path] = []

    def transcribe(self, path: Path) -> str:
        self.calls.append(path)
        return self.text


def _paired_store() -> tuple[DeviceCredentialStore, str, str]:
    store = DeviceCredentialStore(vault=_FakeDeviceVault())
    device_id, token = store.mint("iphone")
    return store, device_id, token


def _wav_bytes(payload: bytes) -> bytes:
    """A minimal byte string that passes the RIFF/WAVE magic-byte sniff — the handler never
    parses beyond the first 12 bytes, so the rest is arbitrary filler."""
    return b"RIFF" + (36 + len(payload)).to_bytes(4, "little") + b"WAVEfmt " + payload


async def _http_request(
    host: str,
    port: int,
    *,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    """Mirrors ``tests/unit/test_device_surface.py``'s own hand-rolled HTTP/1.1 client."""
    reader, writer = await asyncio.open_connection(host, port)
    try:
        request_lines = [f"{method} {path} HTTP/1.1", f"Host: {host}:{port}"]
        for name, value in (headers or {}).items():
            request_lines.append(f"{name}: {value}")
        request_lines.append(f"Content-Length: {len(body)}")
        request_lines.append("Connection: close")
        writer.write(("\r\n".join(request_lines) + "\r\n\r\n").encode("latin-1") + body)
        await writer.drain()

        status_line = await reader.readline()
        status = int(status_line.decode("latin-1").split(" ")[1])

        response_headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            name, _, value = line.decode("latin-1").partition(":")
            response_headers[name.strip().lower()] = value.strip()

        content_length = int(response_headers.get("content-length", "0") or "0")
        response_body = await reader.readexactly(content_length) if content_length else b""
        return status, response_headers, response_body
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _send_declared_but_truncated(
    host: str, port: int, *, headers: dict[str, str], declared_length: int, sent_body: bytes
) -> int:
    """Declares ``declared_length`` in ``Content-Length`` but only ever SENDS ``sent_body`` —
    mirrors ``test_device_surface.py``'s own ``_send_raw_and_read_status`` over-cap technique, so
    an over-cap test never hangs waiting for bytes that are deliberately never sent."""
    reader, writer = await asyncio.open_connection(host, port)
    try:
        request_lines = ["POST /v1/voice HTTP/1.1", f"Host: {host}:{port}"]
        for name, value in headers.items():
            request_lines.append(f"{name}: {value}")
        request_lines.append(f"Content-Length: {declared_length}")
        request_lines.append("Connection: close")
        head = ("\r\n".join(request_lines) + "\r\n\r\n").encode("latin-1")
        writer.write(head + sent_body)
        await writer.drain()
        status_line = await reader.readline()
        return int(status_line.decode("latin-1").split(" ")[1])
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def _build_surface(handler: VoiceIngestHandler) -> tuple[DeviceSurface, str]:
    store, _device_id, token = _paired_store()
    surface = DeviceSurface(
        credential_store=store,
        host="127.0.0.1",
        port=0,
        remote_voice_enabled=True,
        biometrics_enabled=False,
        voice_ingest_handler=handler,
    )
    return surface, token


# --------------------------------------------------------------------------------------- AC1


async def test_valid_wav_lands_byte_identically_and_asr_source_yields_one_event(
    tmp_path: Path,
) -> None:
    drop_dir = tmp_path / "remote-drop"
    handler = VoiceIngestHandler(drop_dir=drop_dir, clock=lambda: _NOW)
    surface, token = _build_surface(handler)
    audio = _wav_bytes(b"utterance-one")

    try:
        await surface.start()
        host, port = surface.address
        status, _headers, body = await _http_request(
            host,
            port,
            method="POST",
            path="/v1/voice",
            headers={
                "X-Wombat-Device-Token": token,
                "Content-Type": "audio/wav",
                "X-Wombat-Captured-At": _NOW.isoformat(),
            },
            body=audio,
        )
    finally:
        await surface.stop()

    assert status == 202
    payload = json.loads(body)
    assert payload["v"] == 1
    assert payload["accepted"] is True
    assert isinstance(payload["utterance_id"], str) and payload["utterance_id"]

    digest = hashlib.sha256(audio).hexdigest()
    written = drop_dir / f"{digest}.wav"
    assert written.read_bytes() == audio

    source = RemoteASRSource(
        drop_dir=drop_dir, transcriber=_FakeTranscriber(), poll_interval_seconds=0.01
    )
    assert source.id == "asr_remote"
    events = await source.poll()
    assert len(events) == 1
    event = events[0]
    assert event.event_key == digest
    assert idempotency_key(source.id, event.event_key) == idempotency_key("asr_remote", digest)


# --------------------------------------------------------------------------------------- AC2


async def test_duplicate_post_returns_same_utterance_id_and_exactly_one_event_across_polls(
    tmp_path: Path,
) -> None:
    drop_dir = tmp_path / "remote-drop"
    handler = VoiceIngestHandler(drop_dir=drop_dir, clock=lambda: _NOW)
    surface, token = _build_surface(handler)
    audio = _wav_bytes(b"same-bytes-both-times")
    headers = {
        "X-Wombat-Device-Token": token,
        "Content-Type": "audio/wav",
        "X-Wombat-Captured-At": _NOW.isoformat(),
    }
    source = RemoteASRSource(
        drop_dir=drop_dir, transcriber=_FakeTranscriber(), poll_interval_seconds=0.01
    )

    try:
        await surface.start()
        host, port = surface.address

        status1, _h1, body1 = await _http_request(
            host, port, method="POST", path="/v1/voice", headers=headers, body=audio
        )
        events_after_first = await source.poll()  # may move the file to processed/ already

        status2, _h2, body2 = await _http_request(
            host, port, method="POST", path="/v1/voice", headers=headers, body=audio
        )
        events_after_second = await source.poll()
    finally:
        await surface.stop()

    assert status1 == 202
    assert status2 == 202
    utterance_id_1 = json.loads(body1)["utterance_id"]
    utterance_id_2 = json.loads(body2)["utterance_id"]
    assert utterance_id_1 == utterance_id_2
    assert len(events_after_first) + len(events_after_second) == 1


# --------------------------------------------------------------------------------------- AC3


async def test_stale_capture_stamp_answers_409_and_writes_nothing(tmp_path: Path) -> None:
    drop_dir = tmp_path / "remote-drop"
    handler = VoiceIngestHandler(drop_dir=drop_dir, clock=lambda: _NOW)
    surface, token = _build_surface(handler)
    stale_captured_at = _NOW - timedelta(seconds=STALE_AUDIO_WINDOW_SECONDS + 1)
    audio = _wav_bytes(b"too-old")

    try:
        await surface.start()
        host, port = surface.address
        status, _headers, body = await _http_request(
            host,
            port,
            method="POST",
            path="/v1/voice",
            headers={
                "X-Wombat-Device-Token": token,
                "Content-Type": "audio/wav",
                "X-Wombat-Captured-At": stale_captured_at.isoformat(),
            },
            body=audio,
        )
    finally:
        await surface.stop()

    assert status == 409
    payload = json.loads(body)
    assert payload["v"] == 1
    assert payload["error"] == "stale_audio"
    assert payload["stale_audio_window_seconds"] == STALE_AUDIO_WINDOW_SECONDS
    assert not drop_dir.exists() or not any(drop_dir.iterdir())

    source = RemoteASRSource(
        drop_dir=drop_dir, transcriber=_FakeTranscriber(), poll_interval_seconds=0.01
    )
    assert await source.poll() == []


# --------------------------------------------------------------------------------------- AC4


async def test_non_audio_content_type_is_rejected_and_writes_nothing(tmp_path: Path) -> None:
    drop_dir = tmp_path / "remote-drop"
    handler = VoiceIngestHandler(drop_dir=drop_dir, clock=lambda: _NOW)
    surface, token = _build_surface(handler)

    try:
        await surface.start()
        host, port = surface.address
        status, _headers, _body = await _http_request(
            host,
            port,
            method="POST",
            path="/v1/voice",
            headers={
                "X-Wombat-Device-Token": token,
                "Content-Type": "text/plain",
                "X-Wombat-Captured-At": _NOW.isoformat(),
            },
            body=b"just plain text, not audio at all",
        )
    finally:
        await surface.stop()

    assert 400 <= status < 500
    assert not drop_dir.exists() or not any(drop_dir.iterdir())


async def test_claimed_wav_with_bad_magic_bytes_is_rejected_and_writes_nothing(
    tmp_path: Path,
) -> None:
    drop_dir = tmp_path / "remote-drop"
    handler = VoiceIngestHandler(drop_dir=drop_dir, clock=lambda: _NOW)
    surface, token = _build_surface(handler)

    try:
        await surface.start()
        host, port = surface.address
        status, _headers, _body = await _http_request(
            host,
            port,
            method="POST",
            path="/v1/voice",
            headers={
                "X-Wombat-Device-Token": token,
                "Content-Type": "audio/wav",
                "X-Wombat-Captured-At": _NOW.isoformat(),
            },
            body=b"NOT-A-REAL-WAV-FILE-JUST-SOME-BYTES",
        )
    finally:
        await surface.stop()

    assert 400 <= status < 500
    assert not drop_dir.exists() or not any(drop_dir.iterdir())


async def test_over_cap_body_is_rejected_without_hanging_and_writes_nothing(
    tmp_path: Path,
) -> None:
    drop_dir = tmp_path / "remote-drop"
    # A small cap keeps this test fast/deterministic while proving the exact same code path a
    # production 10 MiB cap uses.
    handler = VoiceIngestHandler(drop_dir=drop_dir, clock=lambda: _NOW, max_body_bytes=64)
    surface, token = _build_surface(handler)
    sent_body = b"x" * 64

    try:
        await surface.start()
        host, port = surface.address
        status = await asyncio.wait_for(
            _send_declared_but_truncated(
                host,
                port,
                headers={
                    "X-Wombat-Device-Token": token,
                    "Content-Type": "audio/wav",
                    "X-Wombat-Captured-At": _NOW.isoformat(),
                },
                declared_length=128,
                sent_body=sent_body,
            ),
            timeout=5.0,
        )
    finally:
        await surface.stop()

    assert 400 <= status < 500
    assert not drop_dir.exists() or not any(drop_dir.iterdir())


async def test_missing_captured_at_is_rejected_and_writes_nothing(tmp_path: Path) -> None:
    drop_dir = tmp_path / "remote-drop"
    handler = VoiceIngestHandler(drop_dir=drop_dir, clock=lambda: _NOW)
    surface, token = _build_surface(handler)
    audio = _wav_bytes(b"no-timestamp-header")

    try:
        await surface.start()
        host, port = surface.address
        status, _headers, _body = await _http_request(
            host,
            port,
            method="POST",
            path="/v1/voice",
            headers={"X-Wombat-Device-Token": token, "Content-Type": "audio/wav"},
            body=audio,
        )
    finally:
        await surface.stop()

    assert 400 <= status < 500
    assert not drop_dir.exists() or not any(drop_dir.iterdir())


async def test_naive_captured_at_is_rejected_and_writes_nothing(tmp_path: Path) -> None:
    drop_dir = tmp_path / "remote-drop"
    handler = VoiceIngestHandler(drop_dir=drop_dir, clock=lambda: _NOW)
    surface, token = _build_surface(handler)
    audio = _wav_bytes(b"naive-timestamp")

    try:
        await surface.start()
        host, port = surface.address
        status, _headers, _body = await _http_request(
            host,
            port,
            method="POST",
            path="/v1/voice",
            headers={
                "X-Wombat-Device-Token": token,
                "Content-Type": "audio/wav",
                # naive — no explicit UTC offset or "Z" (§2 pins this as a 400).
                "X-Wombat-Captured-At": "2026-08-03T07:12:04",
            },
            body=audio,
        )
    finally:
        await surface.stop()

    assert 400 <= status < 500
    assert not drop_dir.exists() or not any(drop_dir.iterdir())


# ---------------------------------------------------------------------------------- extra: R1


async def test_larger_than_generic_cap_body_is_not_truncated_when_within_the_handler_cap(
    tmp_path: Path,
) -> None:
    """Proves DeviceSurface's per-route body-read cap (R1): a body BIGGER than the surface's own
    generic ``_MAX_BODY_BYTES`` (1 MiB) but within the voice handler's own larger cap must reach
    the handler whole, not truncated."""
    drop_dir = tmp_path / "remote-drop"
    oversized_but_within_route_cap = device_surface_module._MAX_BODY_BYTES + 1024
    handler = VoiceIngestHandler(
        drop_dir=drop_dir,
        clock=lambda: _NOW,
        max_body_bytes=oversized_but_within_route_cap + 4096,
    )
    surface, token = _build_surface(handler)
    audio = _wav_bytes(b"x" * oversized_but_within_route_cap)

    try:
        await surface.start()
        host, port = surface.address
        status, _headers, _body = await _http_request(
            host,
            port,
            method="POST",
            path="/v1/voice",
            headers={
                "X-Wombat-Device-Token": token,
                "Content-Type": "audio/wav",
                "X-Wombat-Captured-At": _NOW.isoformat(),
            },
            body=audio,
        )
    finally:
        await surface.stop()

    assert status == 202
    digest = hashlib.sha256(audio).hexdigest()
    assert (drop_dir / f"{digest}.wav").read_bytes() == audio
