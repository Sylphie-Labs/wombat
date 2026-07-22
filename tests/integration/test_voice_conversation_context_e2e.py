"""tests/integration/test_voice_conversation_context_e2e.py — TK-291 (DEC-64 proof ticket, EP-29).

Proves the whole DEC-64 walkie-talkie arc end-to-end through the REAL seams, fakes only at the
edges: a real ``ASRSource`` (fake ``Transcriber``, a ``tmp_path`` drop dir) driven by the real
``SourceRegistry`` onto ONE real ``WombatQueue`` (throwaway Postgres, ``WOMBAT_TEST_PG_DSN`` —
module-level skip, NEVER the live wombat DB); the real production ``Gate``
(``make_gate_evaluator``, mirroring ``test_drain_pathway_e2e.py``'s own ``_build_real_gate_stack``
verbatim); the real ``ComposeStage`` with a ``FakeModel`` spy (never a live model call — the
operator's own ear-check is out of scope per the ticket's non_goals); the TK-288
``LastSpokenRegister`` plus the TK-289/TK-290 combined ``context_hook`` closure, wired EXACTLY as
``bootstrap.assemble_runtime`` wires them (same merge order: ``replying_to`` first,
``build_voice_context`` merged on top).

AC1: with the register already carrying a fresh spoken utterance (``note_spoken`` called directly
— the "note_spoken registered" precondition) and the ``ExternalItemStore`` seeded with a
today-gcal row and a recent-gmail row, a dropped audio file drains through the real Gate to
``compose`` — the ``FakeModel``'s captured user message carries ``replying_to`` (the exact
registered text) AND both ``context_calendar_today``/``context_recent_email`` substrings
(``format_payload_fields`` renders sorted ``'; '``-joined ``key: value`` pairs on ONE line, so this
asserts substrings, not lines). The reply then flows the normal DEC-60 voice path:
``voice_turn=True`` rides the composed-output artifact, ``speech_shape`` makes a REAL
(non-quiet-pass-through) shaping call, ``speak`` actually calls the TTS adapter, and the chat
broker's future resolves with the full composed text (the pane echo).

AC2: the register's OWN clock (injected, independent of the engine's fixed clock) is advanced past
``LAST_SPOKEN_TTL_SECONDS`` (120.0s); a SECOND dropped file's captured prompt carries NO
``replying_to`` substring, while ``context_calendar_today``/``context_recent_email`` are still
present (the store stays seeded across both turns).

AC3 (byte-untouched neighbors): NOT edited by this module — the DEC-62/DEC-57/DEC-60 and
brief/draft/reflection pinned suites run unmodified alongside this module as part of the full
quality-bar pass (see the ticket's runnable bar).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.state import RunStatus
from cogworx.model.base import ModelResponse
from cogworx.model.registry import ModelRegistry
from cogworx.runtime.engine import Engine
from cogworx.testing.doubles import InMemoryEntityKG

from tests.support.stage_context_fake import FakeModel
from wombat.chat.surface import ChatReplyBroker
from wombat.compose.templates import TemplateComposer
from wombat.config import WombatConfig
from wombat.domain.daily_ledger import DailyLedger
from wombat.domain.daily_ledger import ensure_schema as ensure_daily_ledger_schema
from wombat.domain.item_identity import idempotency_key
from wombat.external_store import ExternalItem, ExternalItemStore
from wombat.external_store import ensure_schema as ensure_external_schema
from wombat.gate.ceiling import CeilingLedger, FlushDayLatch
from wombat.gate.decay import LedgerReset
from wombat.gate.models import ItemKind
from wombat.gate.pending_set import InMemoryPendingJournal, PendingSet
from wombat.gate.pipeline import Gate
from wombat.pathways.drain_pathway import build_drain_pathway
from wombat.queue import WombatQueue
from wombat.queue import ensure_schema as ensure_queue_schema
from wombat.sinks.speak import SpeakSink
from wombat.sources.asr import ASRSource
from wombat.sources.presence import PresenceSnapshot, PresenceState
from wombat.sources.registry import SourceRegistry
from wombat.stages.artifacts import (
    composed_output_from_artifact_data,
    composed_output_voice_turn_from_artifact_data,
    speech_output_from_artifact_data,
    spoken_output_from_artifact_data,
)
from wombat.stages.chat_reply import ChatReplyStage
from wombat.stages.compose import ComposeStage
from wombat.stages.compose_dispatch_router import ComposeDispatchRouter
from wombat.stages.drain_queue import DrainQueueStage
from wombat.stages.gate_stage import GateStage, make_gate_evaluator
from wombat.stages.review_or_speak import ReviewOrSpeakStage
from wombat.stages.speech_shape import SpeechShapeStage
from wombat.substrate import cold_boot_bundle
from wombat.user_model.user_model import UserModel
from wombat.voice.context_prefetch import build_voice_context
from wombat.voice.reply_context import LAST_SPOKEN_TTL_SECONDS, LastSpokenRegister

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

if not _DSN:
    pytest.skip(
        "WOMBAT_TEST_PG_DSN is not set — skipping the TK-291 walkie-talkie conversation e2e "
        "proof, which requires a real throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5511:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5511/postgres",
        allow_module_level=True,
    )

_FIXED_NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
_TZ = ZoneInfo("UTC")
_PATHWAY_ID = "drain"
_STALENESS_CEILING_S = 300.0
_CONFIDENCE_FLOOR = 0.5

# The REAL Gate variant (mirrors test_drain_pathway_e2e.py's own _build_real_gate_stack): under
# the default GENERIC RatingParams, an untimed/automated item never clears this bar.
_REAL_URGENCY_THRESHOLD = 0.75

_REAL_ACTIVE_PRESENCE = PresenceSnapshot(
    state=PresenceState.ACTIVE, confidence=1.0, idle_ms=0, taken_at=_FIXED_NOW.timestamp()
)


class _NoOpRollover:
    """A ``DayRolloverProtocol`` double that never fires — this module proves the conversation
    arc, not decay/rollover (mirrors ``test_drain_pathway_e2e.py``'s own double)."""

    def check(self) -> LedgerReset | None:
        return None


def _config() -> WombatConfig:
    return WombatConfig(deepseek_api_key="dummy-not-real-key", deepseek_base_url="https://x.test")


def _initial_artifact() -> Artifact:
    return Artifact(
        kind="drain-tick",
        produced_by="test",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data={},
    )


class _MutableClock:
    """A steppable epoch-seconds clock (the ``LastSpokenRegister`` Clock shape) — lets AC2 fast
    forward past ``LAST_SPOKEN_TTL_SECONDS`` without a real sleep. Independent of the engine's own
    fixed ``_FIXED_NOW`` clock and ``ASRSource``'s own ``captured_at`` clock."""

    def __init__(self, start: float) -> None:
        self._value = start

    def __call__(self) -> float:
        return self._value

    def advance(self, seconds: float) -> None:
        self._value += seconds


class _RecordingTTSAdapter:
    """A TTS adapter fake that records every ``speak()`` call verbatim."""

    def __init__(self) -> None:
        self.spoken: list[str] = []

    def speak(self, text: str) -> None:
        self.spoken.append(text)


class _KeyedTranscriber:
    """A fake ``Transcriber`` returning a per-filename canned transcript."""

    def __init__(self, texts: dict[str, str]) -> None:
        self._texts = texts

    def transcribe(self, path: Path) -> str:
        return self._texts[path.name]


@pytest.fixture
def clean_tables() -> None:
    """Ensures every schema this module touches exists and is empty before each test — mirrors
    ``test_drain_pathway_e2e.py``'s ``clean_table_and_ledger``, plus the TK-244 external-items
    table this module's ``context_hook`` closure reads."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_queue_schema(conn)
        ensure_daily_ledger_schema(conn)
        ensure_external_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE wombat_queue")
            cur.execute("TRUNCATE TABLE daily_ledger")
            cur.execute("TRUNCATE TABLE wombat_external_items")
        conn.commit()


async def _wait_until(
    predicate: Callable[[], bool], *, timeout: float = 5.0, interval: float = 0.01
) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(interval)


async def test_ac1_and_ac2_walkie_talkie_conversation_arc(
    clean_tables: None, tmp_path: Path
) -> None:
    assert _DSN is not None
    dsn = _DSN
    queue = WombatQueue(dsn, max_size=10)
    external_item_store = ExternalItemStore(dsn)
    daily_ledger = DailyLedger(dsn, tz=_TZ, clock=lambda: _FIXED_NOW)
    try:
        # --- DEC-64 gap B: seed the grounding bundle -- the store stays seeded across both turns
        today_start = datetime(2026, 7, 21, 9, 0, tzinfo=_TZ)
        external_item_store.upsert_many(
            "gcal",
            [
                ExternalItem(
                    item_key="evt-1",
                    payload={
                        "event_id": "evt-1",
                        "title": "Team sync",
                        "start": today_start.isoformat(),
                        "end": today_start.isoformat(),
                        "all_day": False,
                    },
                    occurs_at=today_start,
                )
            ],
            fetched_at=today_start,
        )
        external_item_store.upsert_many(
            "gmail",
            [
                ExternalItem(
                    item_key="msg-1",
                    payload={
                        "message_id": "msg-1",
                        "subject": "Weekly digest",
                        "sender": "digest@example.com",
                        "received_at": today_start.isoformat(),
                        "priority_band": "normal",
                    },
                    occurs_at=today_start,
                )
            ],
            fetched_at=today_start,
        )

        # --- TK-288: ONE shared register, its own steppable clock (independent of the engine's).
        register_clock = _MutableClock(start=1_000_000.0)
        last_spoken_register = LastSpokenRegister(clock=register_clock)
        register_seed_text = "Should I send the reply now?"
        last_spoken_register.note_spoken("earlier-item", register_seed_text)

        # --- TK-289/TK-290: the SAME combined context_hook closure bootstrap.assemble_runtime
        # wires -- replying_to first, build_voice_context merged on top (bootstrap.py's
        # asr_context_hook, verbatim).
        def asr_context_hook() -> dict[str, str]:
            text = last_spoken_register.current()
            extra: dict[str, str] = {} if text is None else {"replying_to": text}
            extra.update(
                build_voice_context(external_item_store, tz=_TZ, clock=lambda: _FIXED_NOW)
            )
            return extra

        chat_reply_broker = ChatReplyBroker()

        def asr_turn_hook(event_key: str, transcript: str, captured_at: str) -> None:
            item_id = idempotency_key("asr", event_key)
            chat_reply_broker.register_voice_turn(item_id, transcript, captured_at)

        drop_dir = tmp_path
        audio_1 = b"turn-one-audio-bytes"
        audio_2 = b"turn-two-audio-bytes"
        event_key_1 = hashlib.sha256(audio_1).hexdigest()
        event_key_2 = hashlib.sha256(audio_2).hexdigest()
        item_id_1 = idempotency_key("asr", event_key_1)
        item_id_2 = idempotency_key("asr", event_key_2)

        transcriber = _KeyedTranscriber(
            {"turn-1.wav": "yes, do that", "turn-2.wav": "what's next"}
        )
        source = ASRSource(
            drop_dir=drop_dir,
            transcriber=transcriber,
            poll_interval_seconds=0.01,
            clock=lambda: _FIXED_NOW,
            turn_hook=asr_turn_hook,
            context_hook=asr_context_hook,
        )
        registry = SourceRegistry(queue)
        registry.register(source)

        # --- the real production Gate (mirrors test_drain_pathway_e2e.py's _build_real_gate_stack)
        user_model = UserModel(entity_kg=InMemoryEntityKG(), user_id="demo-user")
        pending_set = PendingSet(journal=InMemoryPendingJournal(), max_pending=100)
        ceiling = CeilingLedger(daily_ledger=daily_ledger, per_class_daily_ceiling=3)
        flush_latch = FlushDayLatch(daily_ledger=daily_ledger)
        gate = Gate(
            user_model=user_model,
            pending_set=pending_set,
            ceiling=ceiling,
            urgency_threshold=_REAL_URGENCY_THRESHOLD,
            load_flush_threshold=10.0,
            flush_min_age_seconds=300.0,
            decay_ttl_seconds=float("inf"),
            day_rollover=_NoOpRollover(),
            clock=lambda: _FIXED_NOW.timestamp(),
            flush_latch=flush_latch,
        )

        drain_queue_stage = DrainQueueStage(queue, batch_size=1, poll_interval_seconds=5.0)
        gate_stage = GateStage(
            evaluate=make_gate_evaluator(
                gate=gate,
                staleness_ceiling_s=_STALENESS_CEILING_S,
                confidence_floor=_CONFIDENCE_FLOOR,
                clock=lambda: _FIXED_NOW.timestamp(),
            ),
            presence_provider=lambda: _REAL_ACTIVE_PRESENCE,
        )
        review_or_speak_stage = ReviewOrSpeakStage(queue=queue)
        compose_dispatch_router = ComposeDispatchRouter(
            composer_by_kind={ItemKind.GENERIC: "compose"}
        )

        shared_model = FakeModel(
            response=ModelResponse(
                text="You have a new alert.", model_id="fake", finish_reason="stop"
            )
        )
        model_factory = lambda guard: shared_model  # noqa: E731
        compose_stage = ComposeStage(config=_config(), template_composer=TemplateComposer())
        chat_reply_stage = ChatReplyStage(broker=chat_reply_broker)
        adapter = _RecordingTTSAdapter()
        speech_shape_stage = SpeechShapeStage(
            config=_config(), voice_enabled=True, adapter_present=True
        )
        speak_stage = SpeakSink(
            voice_enabled=True, adapter=adapter, on_spoken=last_spoken_register.note_spoken
        )

        graph = build_drain_pathway(
            drain_queue_stage,
            gate_stage,
            review_or_speak_stage,
            compose_dispatch_router,
            compose_stage,
            chat_reply_stage,
            speech_shape_stage,
            speak_stage,
        )
        bundle = cold_boot_bundle()
        bundle.pathways.register(_PATHWAY_ID, graph)
        models = ModelRegistry()
        models.register_factory("deepseek", model_factory)
        engine = Engine(
            models=models,
            journal=bundle.journal,
            graph_store=bundle.graph_store,
            latent=bundle.latent,
            pathways=bundle.pathways,
            model_profile="deepseek",
            clock=lambda: _FIXED_NOW,
        )

        await registry.start()
        try:
            # ==================================================================== AC1
            future_1 = chat_reply_broker.register(item_id_1)
            (drop_dir / "turn-1.wav").write_bytes(audio_1)
            await _wait_until(lambda: (drop_dir / "processed" / "turn-1.wav").exists())

            final_1 = await engine.run(
                run_id="run-turn-1",
                session_id="sess-turn-1",
                pathway_id=_PATHWAY_ID,
                initial=_initial_artifact(),
            )
            assert final_1.status is RunStatus.COMPLETED
            assert queue.drain() == []  # acked

            # compose + speech_shape each make ONE model call per turn.
            assert len(shared_model.calls) == 2
            user_message_1 = shared_model.calls[0][1].content
            assert f"replying_to: {register_seed_text}" in user_message_1
            assert "context_calendar_today:" in user_message_1
            assert "Team sync" in user_message_1
            assert "context_recent_email:" in user_message_1
            assert "Weekly digest" in user_message_1

            compose_steps_1 = [s for s in final_1.steps if s.stage_name == "compose"]
            assert len(compose_steps_1) == 1
            composed_artifact_1 = compose_steps_1[0].result.output
            assert composed_artifact_1 is not None
            text_1, _item_id_1, _item_kind_1, degraded_1 = composed_output_from_artifact_data(
                composed_artifact_1.data
            )
            assert degraded_1 is False
            assert composed_output_voice_turn_from_artifact_data(composed_artifact_1.data) is True

            # DEC-60 pane echo: the chat broker's future resolves with the full composed text.
            assert future_1.done()
            assert future_1.result() == text_1

            # The normal DEC-60 voice path: speech_shape makes a REAL shaping call (never the
            # quiet held-chat pass-through, since voice_turn=True), and speak actually speaks it.
            speech_steps_1 = [s for s in final_1.steps if s.stage_name == "speech_shape"]
            assert len(speech_steps_1) == 1
            speech_artifact_1 = speech_steps_1[0].result.output
            assert speech_artifact_1 is not None
            _sp_id_1, _sp_kind_1, speech_text_1, speech_degraded_1 = (
                speech_output_from_artifact_data(speech_artifact_1.data)
            )
            assert speech_text_1 is not None
            assert speech_degraded_1 is False

            speak_steps_1 = [s for s in final_1.steps if s.stage_name == "speak"]
            assert len(speak_steps_1) == 1
            spoken_artifact_1 = speak_steps_1[0].result.output
            assert spoken_artifact_1 is not None
            _sk_id_1, _sk_kind_1, spoken_1, spoken_degraded_1 = spoken_output_from_artifact_data(
                spoken_artifact_1.data
            )
            assert spoken_1 is True
            assert spoken_degraded_1 is False
            assert adapter.spoken == [speech_text_1]

            # ==================================================================== AC2
            register_clock.advance(LAST_SPOKEN_TTL_SECONDS + 60.0)  # well past the TTL
            assert last_spoken_register.current() is None

            future_2 = chat_reply_broker.register(item_id_2)
            (drop_dir / "turn-2.wav").write_bytes(audio_2)
            await _wait_until(lambda: (drop_dir / "processed" / "turn-2.wav").exists())

            final_2 = await engine.run(
                run_id="run-turn-2",
                session_id="sess-turn-2",
                pathway_id=_PATHWAY_ID,
                initial=_initial_artifact(),
            )
            assert final_2.status is RunStatus.COMPLETED
            assert queue.drain() == []  # acked

            assert len(shared_model.calls) == 4  # +1 compose, +1 speech_shape, from turn 2
            user_message_2 = shared_model.calls[2][1].content
            assert "replying_to:" not in user_message_2  # stale -- key absent
            assert "context_calendar_today:" in user_message_2  # store still seeded
            assert "Team sync" in user_message_2
            assert "context_recent_email:" in user_message_2
            assert "Weekly digest" in user_message_2

            assert future_2.done()
        finally:
            await registry.stop()
    finally:
        queue.close()
        daily_ledger.close()
        external_item_store.close()
