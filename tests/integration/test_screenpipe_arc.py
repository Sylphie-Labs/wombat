"""tests/integration/test_screenpipe_arc.py — TK-325: the DEC-70 screenpipe arc closer, e2e.

Stands up ONE fake screenpipe HTTP server (stdlib ``http.server``, loopback-only, ephemeral port)
per test and drives the REAL ``ScreenpipeClient``/``ScreenpipeEventSource``/``DreamScreenpipeStage``
classes against it — no live screenpipe, ever, in CI. Model calls are ALWAYS a ``FakeModel`` (never
network) — this module never arms the live persona eval harness.

Requires a throwaway Postgres on ``WOMBAT_TEST_PG_DSN`` (never the live wombat DB, ASMP-2):

    docker run --rm -d -p 5442:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5442/postgres

Three legs, mirroring the briefing's own numbering:

(a)/(b) ``test_default_hold_then_forced_surface_through_real_gate`` — a ``ScreenpipeEventSource``
    derives ONE ``context_switch`` event from the fake server's data; enqueued as-is onto the REAL
    ``assemble_runtime``-composed ``Gate`` (throwaway pg), it HOLDS with ZERO model calls (the
    MAY-not-speak pin, end-to-end); the SAME event, payload-boosted to clear the real
    ``urgency_threshold``, SURFACES and composes one line through the existing generic mouth.

(c) ``test_dream_screenpipe_distills_fake_content_into_next_known_user_context`` — a
    ``DreamScreenpipeStage`` (built directly, over the REAL fake-server-backed ``ScreenpipeClient``
    and a ``FakeModel`` — ``dream_substrate.model`` has no client-injection seam through
    ``assemble_runtime``'s public surface, so this is the one leg that cannot be driven through the
    composed dream pathway without a live network client) distills the fake timeline into a
    clamped ``source='behavior'`` fact; the SAME throwaway pg then shows that fact rendered inside
    ``known_user_context`` on the NEXT chat turn's compose call, through the REAL
    ``assemble_runtime`` bundle.

(3) ``test_all_four_observe_toggles_off_constructs_no_screenpipe_client_anywhere`` — the structural
    inertness pin: an ``assemble_runtime`` bundle built with every ``wombat_observe_*`` toggle
    false contains NO ``ScreenpipeClient`` instance anywhere in its object graph, proven by a
    bounded, cycle-safe walk (not just the handful of attributes existing suites already name).

ISS-37 m3 (the level-1 rider): every dataset here stays comfortably under the client's
``_MAX_RESULTS=50`` cap, so the run-continuity-breaking truncation path is never even triggered —
the e2e "holds against" that landed behavior by construction, never tripping it.
"""

from __future__ import annotations

import http.server
import json
import os
import threading
import time as _time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, cast
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Transition
from cogworx.loop.state import RunStatus
from cogworx.model.base import ModelResponse
from cogworx.model.registry import ModelRegistry
from cogworx.runtime.engine import Engine
from cogworx.testing.doubles import InMemoryGraphStore, InMemoryJournal, InMemoryLatentStore

from tests.support.stage_context_fake import FakeModel, StageContextFake
from wombat import bootstrap
from wombat.behavior.stages.dream_screenpipe import DreamScreenpipeStage
from wombat.config import WombatConfig
from wombat.domain.item_identity import idempotency_key as derive_key
from wombat.integrations.screenpipe.client import ScreenpipeClient
from wombat.params import load_operating_params
from wombat.queue import QueueItem
from wombat.schema_preflight import ensure_all_schemas
from wombat.sources.presence import PresenceSnapshot, PresenceState
from wombat.sources.screenpipe_source import ScreenpipeEventSource
from wombat.stages.gate_stage import GateStage
from wombat.user_facts import UserFactsStore

_PG_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

if not _PG_DSN:
    pytest.skip(
        "WOMBAT_TEST_PG_DSN is not set — skipping the TK-325 screenpipe-arc e2e, which requires "
        "a real throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5442:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5442/postgres",
        allow_module_level=True,
    )

_TZ = ZoneInfo("UTC")


def _config(**overrides: Any) -> WombatConfig:
    # wombat_voice_enabled is explicitly pinned False (never left to whatever the operator's own
    # .env happens to carry) — this module asserts on the generic mouth's ONE compose call, and a
    # live voice config would add speech_shape's own second model call on top of it.
    return WombatConfig(
        deepseek_api_key="sk-test",
        deepseek_base_url="https://api.deepseek.com",
        wombat_voice_enabled=False,
        **overrides,
    )


def _drain_tick() -> Artifact:
    return Artifact(
        kind="drain-tick",
        produced_by="test",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=datetime.now(UTC)),
        data={},
    )


def _build_engine(bundle: bootstrap.RuntimeBundle, *, model: FakeModel) -> Engine:
    """Mirrors ``test_bootstrap.py``'s own TK-296 precedent: a FRESH ``Engine`` over the REAL
    ``assemble_runtime``-composed ``bundle.pathways``, with the "deepseek" profile swapped to a
    ``FakeModel`` factory — never a live network call, this module's own hard rule."""
    models = ModelRegistry()
    models.register_factory("deepseek", lambda guard: model)
    return Engine(
        models=models,
        journal=InMemoryJournal(),
        graph_store=InMemoryGraphStore(),
        latent=InMemoryLatentStore(),
        pathways=bundle.pathways,
        model_profile="deepseek",
        clock=lambda: datetime.now(UTC),
    )


def _raw_screenpipe_item(
    app: str, title: str, captured_at: datetime, *, frame_id: str = "frame-1"
) -> dict[str, Any]:
    """The raw ``{"type": "OCR", "content": {...}}`` shape ``ScreenpipeClient._parse_item``
    expects off ``GET /search``'s ``data`` array."""
    return {
        "type": "OCR",
        "content": {
            "app_name": app,
            "window_name": title,
            "text": "raw ocr body — never read by any consumer under test (DEC-70f)",
            "timestamp": captured_at.isoformat(),
            "frame_id": frame_id,
        },
    }


class _FakeScreenpipeRequestHandler(http.server.BaseHTTPRequestHandler):
    """Set per-instance by ``_fake_screenpipe_server`` below (closure-captured ``items``) — see
    that function for why this is built fresh per test rather than a shared module-level class."""

    items: ClassVar[list[dict[str, Any]]] = []

    def log_message(self, format: str, *args: Any) -> None:
        pass  # silence the default stderr access log

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._reply(200, {"status": "ok"})
            return
        if parsed.path == "/search":
            qs = parse_qs(parsed.query)
            start = datetime.fromisoformat(qs["start_time"][0])
            end = datetime.fromisoformat(qs["end_time"][0])
            matched = [
                item
                for item in self.items
                if start <= datetime.fromisoformat(item["content"]["timestamp"]) < end
            ]
            self._reply(200, {"data": matched})
            return
        self._reply(404, {"error": "not found"})

    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def _fake_screenpipe_server(items: list[dict[str, Any]]) -> Iterator[str]:
    """Stand up ONE loopback-only screenpipe fake (``GET /health`` + ``GET /search``) on an
    ephemeral port, serving ``items`` filtered by ``[start_time, end_time)`` on
    ``content.timestamp`` — mirrors ``tests/pathways/test_dream_screenpipe_stage.py``'s own
    ``_FakeClient`` windowing semantics, just over a real HTTP transport instead of a scripted
    double. Yields the ``base_url``; always shuts the server down on exit."""
    handler = type("_Handler", (_FakeScreenpipeRequestHandler,), {"items": items})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def _reset_tables(dsn: str, tables: tuple[str, ...]) -> None:
    import psycopg

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            for table in tables:
                cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        conn.commit()


# =================================================================================================
# (a)/(b): ScreenpipeEventSource derives one event from the fake server -> the REAL gate holds by
# default with zero model calls -> the same event, payload-forced, surfaces and composes one line.
# =================================================================================================


async def test_default_hold_then_forced_surface_through_real_gate(tmp_path: Any) -> None:
    assert _PG_DSN is not None
    _reset_tables(
        _PG_DSN,
        ("wombat_queue", "daily_ledger", "pending_journal", "wombat_seen_events"),
    )
    ensure_all_schemas(_PG_DSN)

    # A two-sample same-app timeline a day apart — comfortably under the client's 50-result cap
    # (ISS-37 m3), and far enough apart that dwell clears _MIN_DWELL_S=120s on the second sample.
    items = [
        _raw_screenpipe_item(
            "VSCode", "main.py - myproject", datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
        ),
        _raw_screenpipe_item(
            "VSCode", "main.py - myproject", datetime(2026, 7, 2, 10, 0, tzinfo=UTC)
        ),
    ]

    with _fake_screenpipe_server(items) as base_url:
        client = ScreenpipeClient(base_url)
        assert client.health() is True  # exercises GET /health explicitly

        construction_time = datetime(2026, 6, 30, tzinfo=UTC)
        poll_time = datetime(2026, 7, 3, tzinfo=UTC)
        clock_calls = iter([construction_time, poll_time])
        source = ScreenpipeEventSource(
            client=client, poll_interval_seconds=30.0, clock=lambda: next(clock_calls)
        )
        events = await source.poll()

    assert len(events) == 1
    event = events[0]
    assert event.payload["event"] == "context_switch"
    assert event.payload["app"] == "VSCode"
    assert event.payload["event_class"] == "screen_activity"

    op = load_operating_params()
    tz = _TZ
    bundle = bootstrap.assemble_runtime(config=_config(), dsn=_PG_DSN, params=op, tz=tz)

    # Force a fresh, ACTIVE presence read at CALL time (real epoch seconds) — decoupled from
    # whatever the test machine's actual idle state happens to be, so the HOLD below is
    # unambiguously the item's own real score, never a presence-luck accident.
    gate_stage = cast(GateStage, bundle.pathways.get(bundle.drain_pathway_id).get("gate"))
    gate_stage._presence_provider = lambda: PresenceSnapshot(
        state=PresenceState.ACTIVE, confidence=1.0, idle_ms=0, taken_at=_time.time()
    )

    shared_model = FakeModel(
        response=ModelResponse(
            text="VSCode has your attention.", model_id="fake", finish_reason="stop"
        )
    )
    engine = _build_engine(bundle, model=shared_model)

    # --- (a) default: the untimed/automated screen_activity item never clears the real 0.75
    # urgency_threshold under the neutral default RatingParams -> HOLD, zero model calls.
    default_key = derive_key("screenpipe", event.event_key)
    bundle.queue.enqueue(QueueItem(idempotency_key=default_key, payload=dict(event.payload)))
    run1 = await engine.run(
        run_id="tk325-default-hold",
        session_id="tk325-default-hold",
        pathway_id=bundle.drain_pathway_id,
        initial=_drain_tick(),
    )
    assert run1.status is RunStatus.COMPLETED
    assert shared_model.calls == []

    # --- (b) forced-surface: the SAME event's payload, boosted to clear the real threshold
    # (a timed VIP item — mirrors test_drain_pathway_e2e.py's own real-gate forcing precedent)
    # -> SURFACE_IMMEDIATE -> composes one line through the existing generic mouth.
    forced_payload = dict(event.payload)
    forced_payload.update({"is_timed": True, "seconds_to_event": 0, "sender_class": "vip"})
    forced_key = derive_key("screenpipe", event.event_key + ":forced")
    bundle.queue.enqueue(QueueItem(idempotency_key=forced_key, payload=forced_payload))
    run2 = await engine.run(
        run_id="tk325-forced-surface",
        session_id="tk325-forced-surface",
        pathway_id=bundle.drain_pathway_id,
        initial=_drain_tick(),
    )
    assert run2.status is RunStatus.COMPLETED
    assert len(shared_model.calls) == 1
    composed_user_message = shared_model.calls[0][1].content
    assert "item_kind: generic" in composed_user_message
    assert "app: VSCode" in composed_user_message


# =================================================================================================
# (c): the dream graph distills fake content into a clamped source='behavior' fact, which renders
# inside known_user_context on the NEXT chat payload stamp.
# =================================================================================================


async def test_dream_screenpipe_distills_fake_content_into_next_known_user_context(
    tmp_path: Any,
) -> None:
    assert _PG_DSN is not None
    _reset_tables(
        _PG_DSN,
        (
            "wombat_queue",
            "daily_ledger",
            "pending_journal",
            "wombat_user_facts",
            "wombat_seen_events",
        ),
    )
    ensure_all_schemas(_PG_DSN)

    now = datetime(2026, 7, 30, 3, 0, 0, tzinfo=UTC)
    today_local = now.astimezone(_TZ).date()
    # A recurring VSCode morning session over four distinct days — well under the 21-day/50-result
    # ceilings — clears _MIN_RECURRING_TITLE_COUNT=2 and _MIN_DAYPART_COUNT=3/_MIN_DAYPART_SHARE=0.4
    # so the fold yields projection lines and the ONE DEC-23-admitted model call fires.
    items = [
        _raw_screenpipe_item(
            "VSCode",
            "main.py - myproject",
            datetime.combine(
                today_local - timedelta(days=day_offset), datetime.min.time(), tzinfo=_TZ
            )
            + timedelta(hours=10),
        )
        for day_offset in range(4)
    ]

    with _fake_screenpipe_server(items) as base_url:
        dream_client = ScreenpipeClient(base_url)
        user_facts_for_stage = UserFactsStore(_PG_DSN)
        try:
            raw_fact_text = "The user usually has VSCode open in the mornings.\n"
            fake_dream_model = FakeModel(
                response=ModelResponse(text=raw_fact_text, model_id="fake", finish_reason="stop")
            )
            stage = DreamScreenpipeStage(
                client=dream_client, model=fake_dream_model, user_facts=user_facts_for_stage, tz=_TZ
            )
            result = await stage.run(StageContextFake(now_fn=lambda: now))
        finally:
            user_facts_for_stage.close()

    assert isinstance(result, Transition)
    assert result.to == "dream_behavior_log"
    assert result.output.data["new_facts"] == 1
    assert len(fake_dream_model.calls) == 1

    # The fact landed durably on the throwaway pg — read it back via a SEPARATE UserFactsStore
    # instance, proving durability, not in-process memoization.
    verify_store = UserFactsStore(_PG_DSN)
    try:
        rows = verify_store.list_facts(10)
    finally:
        verify_store.close()
    assert any(
        row["source"] == "behavior" and row["fact"] == raw_fact_text.strip() for row in rows
    )

    # Now prove it renders inside known_user_context on the NEXT chat payload — through the REAL
    # assemble_runtime bundle (a fresh UserFactsStore instance, over the SAME dsn).
    op = load_operating_params()
    config = _config(wombat_chat_handshake_file=str(tmp_path / "chat_handshake.json"))
    bundle = bootstrap.assemble_runtime(config=config, dsn=_PG_DSN, params=op, tz=_TZ)
    assert bundle.chat_source is not None
    chat_source = bundle.chat_source
    assert chat_source.context_hook is not None

    shared_model = FakeModel(
        response=ModelResponse(text="Sure thing.", model_id="fake", finish_reason="stop")
    )
    engine = _build_engine(bundle, model=shared_model)

    chat_extra = chat_source.context_hook()
    assert "known_user_context" in chat_extra
    assert raw_fact_text.strip() in chat_extra["known_user_context"]
    chat_payload = {
        **chat_extra,
        "item_kind": "chat",
        "text": "what do you know about me",
        "received_at": datetime.now(UTC).isoformat(),
    }
    bundle.source_registry._enqueue.enqueue(
        QueueItem(
            idempotency_key=derive_key(chat_source.id, "tk325-chat-1"), payload=chat_payload
        )
    )
    run = await engine.run(
        run_id="tk325-chat-known-user-context",
        session_id="tk325-chat-known-user-context",
        pathway_id=bundle.drain_pathway_id,
        initial=_drain_tick(),
    )
    assert run.status is RunStatus.COMPLETED
    compose_calls = [
        call for call in shared_model.calls if "known_user_context:" in call[1].content
    ]
    assert len(compose_calls) == 1
    assert raw_fact_text.strip() in compose_calls[0][1].content


# =================================================================================================
# (3): structural inertness — all four observe toggles false constructs NO ScreenpipeClient
# instance anywhere in the returned bundle's object graph.
# =================================================================================================


def _all_slot_names(cls: type) -> list[str]:
    names: list[str] = []
    for klass in cls.__mro__:
        slots = klass.__dict__.get("__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        names.extend(slots)
    return names


def _find_screenpipe_client(root: object) -> ScreenpipeClient | None:
    """A bounded, cycle-safe walk of the wombat/cogworx-owned object graph rooted at ``root``.

    DEC-70a custody: ``ScreenpipeClient`` is the ONE class that ever talks to screenpipe, and it
    is referenced ONLY from wombat/cogworx-authored objects (never a third-party SDK/stdlib
    object) — so this walk descends into builtin containers plus any object whose type lives in
    the ``wombat``/``cogworx`` package tree, and treats everything else (psycopg connections,
    locks, provider clients, zoneinfo, ...) as an opaque leaf. That keeps the walk both SAFE (no
    sockets/locks/huge module ``__globals__`` ever get descended into) and properly bounded (a
    visited-id set guards cycles; a depth cap is a pure safety net, never expected to bite).
    """
    visited: set[int] = set()
    stack: list[tuple[object, int]] = [(root, 0)]
    max_depth = 60

    while stack:
        obj, depth = stack.pop()
        if depth > max_depth:
            continue
        obj_id = id(obj)
        if obj_id in visited:
            continue
        visited.add(obj_id)

        if isinstance(obj, ScreenpipeClient):
            return obj

        if isinstance(obj, (list, tuple, set, frozenset)):
            stack.extend((item, depth + 1) for item in obj)
            continue
        if isinstance(obj, dict):
            for key, value in obj.items():
                stack.append((key, depth + 1))
                stack.append((value, depth + 1))
            continue

        module_name = type(obj).__module__ or ""
        if not (module_name == "wombat" or module_name.startswith("wombat.")) and not (
            module_name == "cogworx" or module_name.startswith("cogworx.")
        ):
            continue  # a third-party/stdlib leaf — outside DEC-70a's custody boundary

        instance_dict = getattr(obj, "__dict__", None)
        if instance_dict:
            stack.extend((value, depth + 1) for value in instance_dict.values())
        for slot in _all_slot_names(type(obj)):
            try:
                value = getattr(obj, slot)
            except Exception:
                continue
            stack.append((value, depth + 1))

    return None


def test_all_four_observe_toggles_off_constructs_no_screenpipe_client_anywhere() -> None:
    assert _PG_DSN is not None
    config = _config()
    assert config.wombat_observe_screen is False
    assert config.wombat_observe_webcam is False
    assert config.wombat_observe_mic is False
    assert config.wombat_observe_screenpipe is False

    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(
        config=config, dsn=_PG_DSN, params=op, tz=_TZ, replay_pending=False
    )

    found = _find_screenpipe_client(bundle)
    assert found is None, f"a ScreenpipeClient instance was reachable in the graph: {found!r}"
