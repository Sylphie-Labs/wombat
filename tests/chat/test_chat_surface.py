"""TK-222 acceptance criteria — the runtime chat seam (EP-32, Q-110(d)).

ALL tests here are pure-asyncio: a REAL ``ChatSurface`` (``asyncio.start_server`` on an ephemeral
loopback port) driven by a hand-rolled minimal HTTP client (stdlib-only, mirrors the surface's own
stdlib-only posture) — no Postgres, no real network beyond loopback, no fastapi/uvicorn/httpx.

  AC1 end-to-end (fakes): a tokened POST /chat pushes exactly one SourceEvent whose
      registry-derived idempotency key round-trips through ``gate_item_from_queue_item`` to the
      pre-computed item_id; resolving the broker with composed text answers the SAME connection.
  AC1 structural: wombat/chat/* + sources/chat_source.py import no model/compose/mouth module,
      and a spy proves the only egress from a message is ChatSource.push -> registry poll ->
      Enqueuer.
  AC2: tokenless/wrong-token -> 401; the socket is bound to 127.0.0.1 exclusively; exactly one
      parseable handshake JSON exists at the configured path per launch (via ``wombat.runtime``'s
      guarded start seam).
  AC3: a start/handshake failure logs ONE loud WARNING and never raises (CON-3); a broker.resolve
      failure inside chat_reply is covered by ``tests/stages/test_chat_reply.py`` — this module
      covers the TIMEOUT path -> {"status": "held"}.
  AC4 (regression) lives in tests/unit/test_compose_stage.py + the wider suite.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from wombat.chat.surface import ChatReplyBroker, ChatSurface
from wombat.domain.item_identity import idempotency_key as derive_key
from wombat.gate.gate import gate_item_from_queue_item
from wombat.gate.models import ItemKind
from wombat.queue import EnqueueResult, QueueItem
from wombat.runtime import _start_chat_surface, _stop_chat_surface
from wombat.sources.base import SourceEvent
from wombat.sources.chat_source import ChatSource
from wombat.sources.registry import SourceRegistry

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "wombat"
_CHAT_PACKAGE_DIR = _SRC_ROOT / "chat"
_CHAT_SOURCE_PATH = _SRC_ROOT / "sources" / "chat_source.py"

_FORBIDDEN_IMPORT_PREFIXES = (
    "openai",
    "httpx",
    "requests",
    "cogworx.model",
    "wombat.compose",
    "wombat.stages.compose",
)

_TOKEN = "test-chat-token-0123456789"


class _FakeEnqueuer:
    def __init__(self) -> None:
        self.items: list[QueueItem] = []

    def enqueue(self, item: QueueItem) -> EnqueueResult:
        self.items.append(item)
        return EnqueueResult.QUEUED


@dataclass
class _Stack:
    surface: ChatSurface
    source: ChatSource
    broker: ChatReplyBroker
    registry: SourceRegistry
    enqueuer: _FakeEnqueuer = field(default_factory=_FakeEnqueuer)


async def _start_stack(
    tmp_path: Path, *, reply_timeout_seconds: float = 5.0
) -> _Stack:
    source = ChatSource()
    broker = ChatReplyBroker()
    surface = ChatSurface(
        source=source,
        broker=broker,
        token=_TOKEN,
        handshake_path=tmp_path / "chat_handshake.json",
        reply_timeout_seconds=reply_timeout_seconds,
    )
    enqueuer = _FakeEnqueuer()
    registry = SourceRegistry(enqueuer)
    registry.register(source)

    await surface.start()
    await registry.start()
    return _Stack(
        surface=surface, source=source, broker=broker, registry=registry, enqueuer=enqueuer
    )


async def _stop_stack(stack: _Stack) -> None:
    await stack.registry.stop()
    await stack.surface.stop()


async def _wait_until(
    predicate: Callable[[], bool], *, timeout: float = 2.0, interval: float = 0.005
) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(interval)


async def _http_request(
    host: str,
    port: int,
    *,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    """A minimal HTTP/1.1 client, hand-rolled to mirror ``ChatSurface``'s own stdlib-only
    posture — no httpx/requests anywhere in this test module either."""
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
        response_body = (
            await reader.readexactly(content_length) if content_length else b""
        )
        return status, response_headers, response_body
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def _imported_module_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            names.add(node.module)
    return names


# --- AC1 structural: no model/compose/mouth import anywhere in wombat/chat/* or chat_source.py ---


def test_ac1_chat_package_imports_no_model_compose_or_mouth_module() -> None:
    paths = [*sorted(_CHAT_PACKAGE_DIR.glob("*.py")), _CHAT_SOURCE_PATH]
    assert len(paths) >= 2
    for path in paths:
        imported = _imported_module_names(path.read_text(encoding="utf-8"))
        offenders = [
            name
            for name in imported
            if any(
                name == prefix or name.startswith(prefix + ".")
                for prefix in _FORBIDDEN_IMPORT_PREFIXES
            )
        ]
        assert not offenders, f"{path} imports forbidden module(s): {offenders}"


# --- AC1 end-to-end: identity round trip + reply delivery ---------------------------------------


async def test_ac1_end_to_end_identity_round_trip_and_reply_delivery(tmp_path: Path) -> None:
    stack = await _start_stack(tmp_path)
    try:
        host, port = stack.surface.address

        post_task = asyncio.create_task(
            _http_request(
                host,
                port,
                method="POST",
                path="/chat",
                headers={
                    "X-Wombat-Chat-Token": _TOKEN,
                    "Content-Type": "application/json",
                },
                body=json.dumps({"text": "hello wombat"}).encode("utf-8"),
            )
        )

        await _wait_until(lambda: len(stack.enqueuer.items) >= 1)
        assert len(stack.enqueuer.items) == 1
        queue_item = stack.enqueuer.items[0]

        # The registry-derived key: sources.registry.SourceRegistry enqueues under
        # idempotency_key(source.id, event.event_key) — the SAME derivation the surface
        # pre-computed BEFORE pushing.
        assert queue_item.payload["item_kind"] == "chat"
        assert queue_item.payload["text"] == "hello wombat"
        assert "received_at" in queue_item.payload

        gate_item = gate_item_from_queue_item(queue_item)
        assert gate_item.item_id == queue_item.idempotency_key
        assert gate_item.item_kind is ItemKind.CHAT

        stack.broker.resolve(queue_item.idempotency_key, "composed reply text")

        status, headers, body = await asyncio.wait_for(post_task, timeout=5.0)
        assert status == 200
        assert json.loads(body) == {"status": "replied", "text": "composed reply text"}
        assert headers.get("access-control-allow-origin") == "*"
    finally:
        await _stop_stack(stack)


async def test_ac1_spy_only_egress_is_push_then_registry_poll_then_enqueuer(
    tmp_path: Path,
) -> None:
    stack = await _start_stack(tmp_path)
    push_calls: list[SourceEvent] = []
    original_push = stack.source.push

    def _spy_push(event: SourceEvent) -> None:
        push_calls.append(event)
        original_push(event)

    stack.source.push = _spy_push  # type: ignore[method-assign]

    try:
        host, port = stack.surface.address
        post_task = asyncio.create_task(
            _http_request(
                host,
                port,
                method="POST",
                path="/chat",
                headers={"X-Wombat-Chat-Token": _TOKEN},
                body=json.dumps({"text": "spy check"}).encode("utf-8"),
            )
        )

        await _wait_until(lambda: len(push_calls) >= 1)
        assert len(push_calls) == 1
        await _wait_until(lambda: len(stack.enqueuer.items) >= 1)
        assert len(stack.enqueuer.items) == 1
        assert stack.enqueuer.items[0].idempotency_key == derive_key(
            "chat", push_calls[0].event_key
        )

        stack.broker.resolve(stack.enqueuer.items[0].idempotency_key, "ok")
        status, _headers, _body = await asyncio.wait_for(post_task, timeout=5.0)
        assert status == 200
    finally:
        await _stop_stack(stack)


# --- AC2: auth --------------------------------------------------------------------------------


async def test_ac2_tokenless_post_is_401(tmp_path: Path) -> None:
    stack = await _start_stack(tmp_path)
    try:
        host, port = stack.surface.address
        status, _headers, body = await _http_request(
            host,
            port,
            method="POST",
            path="/chat",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"text": "no token"}).encode("utf-8"),
        )
        assert status == 401
        assert json.loads(body) == {"error": "unauthorized"}
        assert stack.enqueuer.items == []
    finally:
        await _stop_stack(stack)


async def test_ac2_wrong_token_post_is_401(tmp_path: Path) -> None:
    stack = await _start_stack(tmp_path)
    try:
        host, port = stack.surface.address
        status, _headers, _body = await _http_request(
            host,
            port,
            method="POST",
            path="/chat",
            headers={"X-Wombat-Chat-Token": "wrong-token"},
            body=json.dumps({"text": "no token"}).encode("utf-8"),
        )
        assert status == 401
        assert stack.enqueuer.items == []
    finally:
        await _stop_stack(stack)


async def test_ac2_options_preflight_is_answered_without_a_token(tmp_path: Path) -> None:
    stack = await _start_stack(tmp_path)
    try:
        host, port = stack.surface.address
        status, headers, _body = await _http_request(
            host, port, method="OPTIONS", path="/chat"
        )
        assert status == 204
        assert headers.get("access-control-allow-origin") == "*"
        assert "x-wombat-chat-token" in headers.get("access-control-allow-headers", "").lower()
    finally:
        await _stop_stack(stack)


# --- AC2: bind ----------------------------------------------------------------------------------


async def test_ac2_socket_is_bound_to_loopback_exclusively(tmp_path: Path) -> None:
    stack = await _start_stack(tmp_path)
    try:
        host, port = stack.surface.address
        assert host == "127.0.0.1"
        assert port != 0
        assert stack.surface.port == port
    finally:
        await _stop_stack(stack)


# --- AC2: handshake (via wombat.runtime's guarded start seam) -----------------------------------


async def test_ac2_exactly_one_parseable_handshake_json_per_launch(tmp_path: Path) -> None:
    handshake_path = tmp_path / "nested" / "chat_handshake.json"
    source = ChatSource()
    broker = ChatReplyBroker()
    surface = ChatSurface(
        source=source, broker=broker, token=_TOKEN, handshake_path=handshake_path
    )
    try:
        await _start_chat_surface(surface)

        assert handshake_path.exists()
        handshake = json.loads(handshake_path.read_text(encoding="utf-8"))
        assert handshake == {"port": surface.port, "token": _TOKEN}

        # A second launch OVERWRITES (still exactly one file, still parseable).
        await _start_chat_surface(surface)
        assert json.loads(handshake_path.read_text(encoding="utf-8")) == {
            "port": surface.port,
            "token": _TOKEN,
        }
    finally:
        await _stop_chat_surface(surface)


async def test_none_surface_is_a_silent_no_op_for_start_and_stop() -> None:
    await _start_chat_surface(None)  # never raises, nothing to log
    await _stop_chat_surface(None)


# --- AC3: degrade — start/handshake failure logs ONE loud WARNING, never raises -----------------


async def test_ac3_unwritable_handshake_path_degrades_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A handshake path that is a DIRECTORY (not writable as a file) is the injected failure —
    the surface still binds, but the handshake write raises; the whole call is guarded."""
    unwritable = tmp_path / "not_a_file"
    unwritable.mkdir()
    source = ChatSource()
    broker = ChatReplyBroker()
    surface = ChatSurface(source=source, broker=broker, token=_TOKEN, handshake_path=unwritable)

    try:
        with caplog.at_level(logging.WARNING, logger="wombat.runtime"):
            await _start_chat_surface(surface)  # never raises

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
    finally:
        await _stop_chat_surface(surface)


async def test_ac3_bind_failure_degrades_with_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _FailingSource(ChatSource):
        pass

    source = _FailingSource()
    broker = ChatReplyBroker()
    surface = ChatSurface(
        source=source,
        broker=broker,
        token=_TOKEN,
        handshake_path=Path("unused.json"),
        host="not-a-real-host.invalid",  # forces asyncio.start_server to raise on bind
    )

    with caplog.at_level(logging.WARNING, logger="wombat.runtime"):
        await _start_chat_surface(surface)  # never raises

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    # A failed bind never assigned a server -- stopping it afterward is still a safe no-op.
    await _stop_chat_surface(surface)


# --- AC3: reply-await timeout -> {"status": "held"} ----------------------------------------------


async def test_ac3_reply_timeout_answers_held_and_a_late_resolve_is_then_a_no_op(
    tmp_path: Path,
) -> None:
    stack = await _start_stack(tmp_path, reply_timeout_seconds=0.05)
    try:
        host, port = stack.surface.address
        status, _headers, body = await asyncio.wait_for(
            _http_request(
                host,
                port,
                method="POST",
                path="/chat",
                headers={"X-Wombat-Chat-Token": _TOKEN},
                body=json.dumps({"text": "slow one"}).encode("utf-8"),
            ),
            timeout=5.0,
        )
        assert status == 200
        assert json.loads(body) == {"status": "held"}

        # The registration was discarded on timeout — a LATE resolve for the same item_id is
        # the documented unknown-id no-op (never raises).
        await _wait_until(lambda: len(stack.enqueuer.items) >= 1)
        item_id = stack.enqueuer.items[0].idempotency_key
        stack.broker.resolve(item_id, "too late")  # must not raise
    finally:
        await _stop_stack(stack)


# --- malformed body -> 400 (basic hygiene, not a Q-110(d) AC but must never crash the surface) --


async def test_malformed_body_is_400_and_never_reaches_the_source(tmp_path: Path) -> None:
    stack = await _start_stack(tmp_path)
    try:
        host, port = stack.surface.address
        status, _headers, _body = await _http_request(
            host,
            port,
            method="POST",
            path="/chat",
            headers={"X-Wombat-Chat-Token": _TOKEN},
            body=b"not json",
        )
        assert status == 400
        assert stack.enqueuer.items == []
    finally:
        await _stop_stack(stack)
