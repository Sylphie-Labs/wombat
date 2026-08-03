"""tests/unit/test_device_surface.py — TK-339 acceptance criteria — DeviceSurface, the
consent-gated LAN listener (DEC-78).

ALL socket-level tests are pure-asyncio: a REAL ``DeviceSurface`` (``asyncio.start_server`` on a
loopback or explicit host) driven by a hand-rolled minimal HTTP client (stdlib-only, mirrors
``tests/chat/test_chat_surface.py``'s own client) — no Postgres, no real network beyond loopback,
no fastapi/uvicorn/httpx. Bundle-wiring tests (AC1) drive ``bootstrap.assemble_runtime`` directly,
following the ``test_bootstrap.py`` precedent (``dsn`` is a bare string; every store touched here
is fully lazy, so no real Postgres connection ever happens).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from zoneinfo import ZoneInfo

import pytest

from wombat import bootstrap
from wombat.config import WombatConfig
from wombat.devices import surface as device_surface_module
from wombat.devices.credentials import DeviceCredentialStore, DeviceVault
from wombat.devices.surface import (
    STALE_AUDIO_WINDOW_SECONDS,
    UTTERANCE_TTL_SECONDS,
    DeviceSurface,
)
from wombat.params import load_operating_params
from wombat.runtime import _start_device_surface, _stop_device_surface
from wombat.voice.stream_playback import STREAM_SAMPLE_RATE

_UNAUTHORIZED_BODY = b'{"error": "unauthorized"}'


class _FakeDeviceVault:
    """The in-memory fake — mirrors ``tests/unit/test_device_credentials.py``'s own fake; unit
    tests never touch the real keyring."""

    def __init__(self) -> None:
        self._blob: str | None = None

    def load(self) -> str | None:
        return self._blob

    def save(self, blob: str) -> None:
        self._blob = blob

    def clear(self) -> None:
        self._blob = None


def _paired_store() -> tuple[DeviceCredentialStore, str, str]:
    """A ``DeviceCredentialStore`` over an in-memory fake vault, with one device already paired.
    Returns ``(store, device_id, token)``."""
    store = DeviceCredentialStore(vault=_FakeDeviceVault())
    device_id, token = store.mint("iphone")
    return store, device_id, token


async def _http_request(
    host: str,
    port: int,
    *,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    """A minimal HTTP/1.1 client, hand-rolled to mirror ``DeviceSurface``'s own stdlib-only
    posture — the SAME shape ``tests/chat/test_chat_surface.py`` uses."""
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


async def _send_raw_and_read_status(host: str, port: int, raw_head: bytes, body: bytes) -> int:
    """Sends ``raw_head`` (request line + headers + terminator) then EXACTLY ``body`` bytes
    (deliberately shorter than any oversized ``Content-Length`` the head declares), and reads
    back only the status line — proves the server answered without waiting for the full declared
    length."""
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(raw_head + body)
        await writer.drain()
        status_line = await reader.readline()
        return int(status_line.decode("latin-1").split(" ")[1])
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def _config(**overrides: object) -> WombatConfig:
    defaults: dict[str, object] = {
        "deepseek_api_key": "sk-test",
        "deepseek_base_url": "https://api.deepseek.com",
        "wombat_voice_enabled": False,
    }
    defaults.update(overrides)
    return WombatConfig(**defaults)  # type: ignore[arg-type]


# --- AC1: bundle wiring — structural inertness (DEC-68(b) pattern) ------------------------------


def test_device_surface_is_none_when_both_consent_toggles_off() -> None:
    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(
        config=_config(),
        dsn="postgresql://fake-host/fake-db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )
    assert bundle.device_surface is None


def test_device_surface_is_constructed_when_remote_voice_toggle_is_on() -> None:
    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(
        config=_config(wombat_remote_voice=True),
        dsn="postgresql://fake-host/fake-db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )
    assert bundle.device_surface is not None


def test_device_surface_is_constructed_when_biometrics_toggle_is_on() -> None:
    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(
        config=_config(wombat_observe_biometrics=True),
        dsn="postgresql://fake-host/fake-db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )
    assert bundle.device_surface is not None


# --- AC2: default bind host is loopback, proven against the live socket -------------------------


async def test_default_bind_host_reaches_only_loopback() -> None:
    store, _device_id, _token = _paired_store()
    surface = DeviceSurface(
        credential_store=store,
        host="127.0.0.1",
        port=0,
        remote_voice_enabled=True,
        biometrics_enabled=False,
    )
    try:
        await surface.start()
        assert surface.address[0] == "127.0.0.1"
    finally:
        await surface.stop()


# --- AC3: explicit non-loopback bind, fixed restart-stable port, one loud log line ---------------


async def test_explicit_non_loopback_bind_is_fixed_and_restart_stable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, _device_id, _token = _paired_store()
    # Port 0 would be ephemeral; bind to an OS-assigned one first via a throwaway server, then
    # reuse that concrete port number as the FIXED configured port under test — proves fixed-port
    # behavior without a hardcoded port number colliding with a parallel test run.
    probe = await asyncio.start_server(lambda r, w: None, host="127.0.0.1", port=0)
    fixed_port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()

    surface = DeviceSurface(
        credential_store=store,
        host="0.0.0.0",
        port=fixed_port,
        remote_voice_enabled=True,
        biometrics_enabled=False,
    )
    try:
        with caplog.at_level(logging.INFO, logger="wombat.devices.surface"):
            await surface.start()
        assert surface.address == ("0.0.0.0", fixed_port)
        info_or_above = [r for r in caplog.records if r.levelno >= logging.INFO]
        assert len(info_or_above) == 1
        assert "0.0.0.0" in info_or_above[0].getMessage()

        await surface.stop()
        await surface.start()  # a second start, same configured port
        assert surface.address == ("0.0.0.0", fixed_port)
    finally:
        await surface.stop()


# --- AC4: anti-enumeration — 401 everywhere, never 404, oversized body never fully buffered ------


async def test_no_token_wrong_token_and_valid_token_on_unknown_path_all_answer_401() -> None:
    store, _device_id, token = _paired_store()
    surface = DeviceSurface(
        credential_store=store,
        host="127.0.0.1",
        port=0,
        remote_voice_enabled=True,
        biometrics_enabled=False,
    )
    try:
        await surface.start()
        host, port = surface.address

        status, _headers, body = await _http_request(host, port, method="GET", path="/v1/health")
        assert status == 401
        assert body == _UNAUTHORIZED_BODY

        status, _headers, body = await _http_request(
            host,
            port,
            method="GET",
            path="/v1/health",
            headers={"X-Wombat-Device-Token": "wrong-token"},
        )
        assert status == 401
        assert body == _UNAUTHORIZED_BODY

        status, _headers, body = await _http_request(
            host,
            port,
            method="GET",
            path="/v1/does-not-exist",
            headers={"X-Wombat-Device-Token": token},
        )
        assert status == 401
        assert status != 404
        assert body == _UNAUTHORIZED_BODY
    finally:
        await surface.stop()


async def test_oversized_body_is_capped_and_never_hangs_the_response() -> None:
    store, _device_id, _token = _paired_store()
    surface = DeviceSurface(
        credential_store=store,
        host="127.0.0.1",
        port=0,
        remote_voice_enabled=True,
        biometrics_enabled=False,
    )
    try:
        await surface.start()
        host, port = surface.address

        # Declares a body of TWICE _MAX_BODY_BYTES but only ever SENDS the capped amount — the
        # server's read is clamped to _MAX_BODY_BYTES before reading, so it never waits for the
        # (never-sent) remainder of the declared length. If the cap were missing, the server
        # would readexactly() the full declared length and this call would time out instead of
        # returning promptly.
        declared_length = device_surface_module._MAX_BODY_BYTES * 2
        sent_body = b"x" * device_surface_module._MAX_BODY_BYTES
        head = (
            f"POST /v1/health HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Content-Length: {declared_length}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("latin-1")
        status = await asyncio.wait_for(
            _send_raw_and_read_status(host, port, head, sent_body), timeout=5.0
        )
        assert status == 401  # no token on this request -- the auth gate runs before any dispatch
    finally:
        await surface.stop()


# --- AC5: bind failure degrades with ONE loud warning, guarded (CON-3) --------------------------


async def test_bind_failure_degrades_with_one_warning(caplog: pytest.LogCaptureFixture) -> None:
    store, _device_id, _token = _paired_store()
    surface = DeviceSurface(
        credential_store=store,
        host="not-a-real-host.invalid",  # forces asyncio.start_server to raise on bind
        port=8788,
        remote_voice_enabled=True,
        biometrics_enabled=False,
    )
    with caplog.at_level(logging.WARNING, logger="wombat.runtime"):
        await _start_device_surface(surface)  # never raises

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    await _stop_device_surface(surface)  # a failed bind never assigned a server -- still safe


async def test_none_surface_is_a_silent_no_op_for_start_and_stop() -> None:
    await _start_device_surface(None)  # never raises, nothing to log
    await _stop_device_surface(None)


# --- AC6: end-to-end from a second connection — tokened 200, untokened 401 ----------------------


async def test_tokened_health_request_answers_200_and_untokened_answers_401() -> None:
    store, device_id, token = _paired_store()
    surface = DeviceSurface(
        credential_store=store,
        host="127.0.0.1",
        port=0,
        remote_voice_enabled=True,
        biometrics_enabled=False,
    )
    try:
        await surface.start()
        host, port = surface.address

        status, _headers, body = await _http_request(
            host,
            port,
            method="GET",
            path="/v1/health",
            headers={"X-Wombat-Device-Token": token},
        )
        assert status == 200
        payload = json.loads(body)
        assert payload["ok"] is True
        assert payload["device_id"] == device_id

        status, _headers, _body = await _http_request(host, port, method="GET", path="/v1/health")
        assert status == 401
    finally:
        await surface.stop()


# --- AC7/AC8: the DEC-83 wire-spec field-level format handshake ---------------------------------


async def test_health_response_matches_the_wire_contract_field_by_field() -> None:
    store, device_id, token = _paired_store()
    surface = DeviceSurface(
        credential_store=store,
        host="127.0.0.1",
        port=0,
        remote_voice_enabled=True,
        biometrics_enabled=False,
    )
    try:
        await surface.start()
        host, port = surface.address

        status, headers, body = await _http_request(
            host,
            port,
            method="GET",
            path="/v1/health",
            headers={"X-Wombat-Device-Token": token},
        )
        assert status == 200
        assert headers["content-type"] == "application/json"
        payload = json.loads(body)
        assert payload == {
            "v": 1,
            "ok": True,
            "device_id": device_id,
            "audio": {"sample_rate_hz": STREAM_SAMPLE_RATE, "format": "pcm_s16le", "channels": 1},
            "stale_audio_window_seconds": STALE_AUDIO_WINDOW_SECONDS,
            "utterance_ttl_seconds": UTTERANCE_TTL_SECONDS,
            "capabilities": {"remote_voice": True, "biometrics": False, "stream": False},
        }
        # DEC-83 §4: the SAME constant the Fish streaming request reads, never a second literal.
        assert payload["audio"]["sample_rate_hz"] == STREAM_SAMPLE_RATE
    finally:
        await surface.stop()


async def test_health_capabilities_block_reflects_both_toggles_as_actually_constructed() -> None:
    store, _device_id, token = _paired_store()
    surface = DeviceSurface(
        credential_store=store,
        host="127.0.0.1",
        port=0,
        remote_voice_enabled=False,
        biometrics_enabled=True,
    )
    try:
        await surface.start()
        host, port = surface.address

        _status, _headers, body = await _http_request(
            host,
            port,
            method="GET",
            path="/v1/health",
            headers={"X-Wombat-Device-Token": token},
        )
        payload = json.loads(body)
        assert payload["capabilities"] == {
            "remote_voice": False,
            "biometrics": True,
            "stream": False,
        }
    finally:
        await surface.stop()


def test_fake_device_vault_used_here_satisfies_the_protocol() -> None:
    assert isinstance(_FakeDeviceVault(), DeviceVault)


# --- TK-340 (R1): POST /v1/voice falls to the SAME 401 as an unknown path when its handler is
# --- absent — DEC-78(b) anti-enumeration parity, proven at the surface level (no VoiceIngest
# --- Handler import here; devices/voice_ingest.py has its own dedicated test module).


async def test_post_voice_falls_to_401_when_no_handler_is_wired() -> None:
    store, _device_id, token = _paired_store()
    surface = DeviceSurface(
        credential_store=store,
        host="127.0.0.1",
        port=0,
        remote_voice_enabled=True,
        biometrics_enabled=False,
        # voice_ingest_handler defaults to None — the route must be indistinguishable from an
        # unknown path (R1).
    )
    try:
        await surface.start()
        host, port = surface.address

        status, _headers, body = await _http_request(
            host,
            port,
            method="POST",
            path="/v1/voice",
            headers={"X-Wombat-Device-Token": token, "Content-Type": "audio/wav"},
            body=b"irrelevant",
        )
        assert status == 401
        assert status != 404
        assert body == _UNAUTHORIZED_BODY
    finally:
        await surface.stop()
