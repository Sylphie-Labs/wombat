"""tests/integration/test_companion_chat_e2e.py — TK-298 (DEC-65(h) proof ticket, EP-29/EP-13).

Proves the DEC-65 companion-arc "getting to know you" loop end-to-end through the REAL seams,
mirroring the ``tests/integration/test_voice_conversation_context_e2e.py`` (TK-291) module shape:
a real ``ChatSource``/``ASRSource`` driven by the real ``SourceRegistry`` (wired with the real
``build_chat_turn_sink`` composed chat-turn sink, TK-295/TK-308) onto ONE real ``WombatQueue``
(throwaway Postgres, ``WOMBAT_TEST_PG_DSN`` — module-level skip, NEVER the live wombat DB); the
real production ``Gate`` (``make_gate_evaluator``); the real ``ComposeStage`` with a ``FakeModel``
spy under a ``LivePersona`` with ``user_name="Jim"``; the real ``DreamFactsStage`` (TK-297).

A typed chat turn is pushed onto the real ``ChatSource`` with its payload built EXACTLY as
``wombat.chat.surface.ChatSurface._accept_message`` builds it (the context_hook mapping merged
UNDER the ``item_kind``/``text``/``received_at`` built-ins) — that merge order, and the surface's
own loopback HTTP transport, are already proven end-to-end by ``tests/chat/test_chat_surface.py``
(TK-296); this module's job starts one seam downstream of that: what a turn does through
Gate/Compose/the durable getting-to-know stores.

AC(a): a typed chat turn composes under the Mouth.CHAT companion instruction (the companion base
role substring present, "Jim" present, the instruction ends with ``CAPABILITY_CHARTER``) with
``known_user_context`` facts (a seeded "told" fact) in the user message, and carries NO
``replying_to`` (the last-spoken register is still empty at this point).

AC(b): a voice turn (real ``ASRSource``, a dropped audio file) gets the SAME companion
instruction/known_user_context, PLUS ``replying_to`` — the last-spoken register was freshly
seeded (inside its TTL) just before this turn.

AC(c): a gmail-kind (``ItemKind.GENERIC``) surfaced item — timed + VIP sender, clearing the real
Gate's urgency bar exactly like ``test_drain_pathway_e2e.py``'s own real-gate test — composes
under the UNCHANGED ``Mouth.COMPOSE`` instruction with ZERO DEC-65 grounding keys in the prompt
(``compose.py``'s ``_GROUNDING_ONLY_KEYS`` is never stamped outside a chat context_hook).

AC(d): both the typed and the voice turn (never the gmail item — the chat-turn sink is an
explicit ``item_kind == "chat"`` whitelist) land in ``wombat_chat_turns``, one ``voice=False``
and one ``voice=True``.

AC(e): a ``DreamFactsStage`` run (TK-297) over those two chat turns, with a fake extraction model,
writes new deduped facts into ``UserFactsStore`` — and the VERY NEXT chat turn's prompt then
carries one of those newly-extracted facts in its ``known_user_context``, closing the
getting-to-know loop in this one module.

AC(3) (byte-untouched neighbors): NOT edited by this module — the DEC-57/DEC-60/DEC-63/DEC-64
pinned suites and the brief/draft/reflection instruction pins run unmodified alongside this
module as part of the ticket's full quality-bar pass.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Transition
from cogworx.loop.state import RunStatus
from cogworx.model.base import ModelResponse
from cogworx.model.registry import ModelRegistry
from cogworx.runtime.engine import Engine
from cogworx.testing.doubles import InMemoryEntityKG

from tests.support.stage_context_fake import FakeModel, StageContextFake
from wombat.behavior.stages.dream_facts import DreamFactsStage
from wombat.chat.surface import ChatReplyBroker
from wombat.chat_turns import ChatTurnStore
from wombat.chat_turns import ensure_schema as ensure_chat_turns_schema
from wombat.compose.templates import TemplateComposer
from wombat.config import WombatConfig
from wombat.domain.daily_ledger import DailyLedger
from wombat.domain.daily_ledger import ensure_schema as ensure_daily_ledger_schema
from wombat.domain.item_identity import idempotency_key
from wombat.gate.ceiling import CeilingLedger, FlushDayLatch
from wombat.gate.decay import LedgerReset
from wombat.gate.models import ItemKind
from wombat.gate.pending_set import InMemoryPendingJournal, PendingSet
from wombat.gate.pipeline import Gate
from wombat.pathways.drain_pathway import build_drain_pathway
from wombat.persona.capabilities import CAPABILITY_CHARTER
from wombat.persona.live import LivePersona
from wombat.persona.matrix import DEFAULT_MATRIX
from wombat.queue import QueueItem, WombatQueue
from wombat.queue import ensure_schema as ensure_queue_schema
from wombat.sinks.speak import SpeakSink
from wombat.sources.asr import ASRSource
from wombat.sources.base import SourceEvent
from wombat.sources.bootstrap import build_chat_turn_sink
from wombat.sources.chat_source import ChatSource
from wombat.sources.presence import PresenceSnapshot, PresenceState
from wombat.sources.registry import SourceRegistry
from wombat.stages.artifacts import composed_output_from_artifact_data
from wombat.stages.chat_reply import ChatReplyStage
from wombat.stages.compose import ComposeStage
from wombat.stages.compose_dispatch_router import ComposeDispatchRouter
from wombat.stages.drain_queue import DrainQueueStage
from wombat.stages.gate_stage import GateStage, make_gate_evaluator
from wombat.stages.review_or_speak import ReviewOrSpeakStage
from wombat.stages.speech_shape import SpeechShapeStage
from wombat.substrate import cold_boot_bundle
from wombat.user_facts import UserFactsStore
from wombat.user_facts import ensure_schema as ensure_user_facts_schema
from wombat.user_model.user_model import UserModel
from wombat.voice.context_prefetch import build_user_facts_context
from wombat.voice.reply_context import LastSpokenRegister

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

if not _DSN:
    pytest.skip(
        "WOMBAT_TEST_PG_DSN is not set — skipping the TK-298 companion-chat e2e proof, which "
        "requires a real throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5511:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5511/postgres",
        allow_module_level=True,
    )

_FIXED_NOW = datetime(2026, 7, 30, 9, 0, 0, tzinfo=UTC)
_TZ = ZoneInfo("UTC")
_PATHWAY_ID = "drain"
_STALENESS_CEILING_S = 300.0
_CONFIDENCE_FLOOR = 0.5

# The REAL Gate variant (mirrors test_drain_pathway_e2e.py's/TK-291's own real-gate stack): under
# the default GENERIC RatingParams, an untimed/automated item never clears this bar — only the
# timed+VIP gmail-kind item below is built to clear it.
_REAL_URGENCY_THRESHOLD = 0.75

_REAL_ACTIVE_PRESENCE = PresenceSnapshot(
    state=PresenceState.ACTIVE, confidence=1.0, idle_ms=0, taken_at=_FIXED_NOW.timestamp()
)


class _NoOpRollover:
    """A ``DayRolloverProtocol`` double that never fires — this module proves the companion-chat
    arc, not decay/rollover (mirrors TK-291's own double)."""

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


class _KeyedTranscriber:
    """A fake ``Transcriber`` returning a per-filename canned transcript."""

    def __init__(self, texts: dict[str, str]) -> None:
        self._texts = texts

    def transcribe(self, path: Path) -> str:
        return self._texts[path.name]


@pytest.fixture
def clean_tables() -> None:
    """Ensures every schema this module touches exists and is empty before each test."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_queue_schema(conn)
        ensure_daily_ledger_schema(conn)
        ensure_chat_turns_schema(conn)
        ensure_user_facts_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE wombat_queue")
            cur.execute("TRUNCATE TABLE daily_ledger")
            cur.execute("TRUNCATE TABLE wombat_chat_turns")
            cur.execute("TRUNCATE TABLE wombat_user_facts")
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


def _push_typed_chat(
    chat_source: ChatSource,
    broker: ChatReplyBroker,
    *,
    text: str,
    context_hook: Callable[[], dict[str, str]],
    received_at: datetime,
) -> str:
    """Push one typed chat turn onto the real ``ChatSource``, with its payload built EXACTLY as
    ``wombat.chat.surface.ChatSurface._accept_message`` builds it (``context_hook``'s mapping
    merged UNDER the ``item_kind``/``text``/``received_at`` built-ins, register-before-push) —
    without standing up the real loopback HTTP transport itself, since that transport's own
    merge-order behavior is already proven end-to-end by ``tests/chat/test_chat_surface.py``
    (TK-296). Returns the registry-derived item id."""
    event_key = uuid4().hex
    payload: dict[str, object] = {
        **context_hook(),
        "item_kind": "chat",
        "text": text,
        "received_at": received_at.isoformat(),
    }
    item_id = idempotency_key(chat_source.id, event_key)
    broker.register(item_id)
    chat_source.push(SourceEvent(event_key=event_key, payload=payload))
    return item_id


async def test_ac_a_through_e_companion_chat_getting_to_know_arc(
    clean_tables: None, tmp_path: Path
) -> None:
    assert _DSN is not None
    dsn = _DSN
    queue = WombatQueue(dsn, max_size=10)
    daily_ledger = DailyLedger(dsn, tz=_TZ, clock=lambda: _FIXED_NOW)
    chat_turn_store = ChatTurnStore(dsn)
    user_facts_store = UserFactsStore(dsn)
    try:
        # --- DEC-65d/DEC-66: a pre-existing "told" fact, seeded directly — (a)/(b) both see it in
        # known_user_context.
        user_facts_store.upsert_fact(
            "seed-dog-fact", "The user has a dog named Biscuit.", source="told"
        )

        # --- TK-288 (DEC-64): the last-spoken register — its own clock, independent of the
        # engine's fixed clock.
        register_clock_value = {"t": 1_000_000.0}
        last_spoken_register = LastSpokenRegister(clock=lambda: register_clock_value["t"])

        # --- TK-289/TK-296 (DEC-64/DEC-65f): replying_to first, known_user_context merged on top
        # — the SAME shape bootstrap.assemble_runtime's asr_context_hook closure wires (this
        # module omits the TK-290 calendar/gmail half — already proven end-to-end by TK-291 —
        # since no AC below reads it).
        def context_hook() -> dict[str, str]:
            text = last_spoken_register.current()
            extra: dict[str, str] = {} if text is None else {"replying_to": text}
            extra.update(build_user_facts_context(user_facts_store))
            return extra

        chat_reply_broker = ChatReplyBroker()
        chat_source = ChatSource()

        def asr_turn_hook(event_key: str, transcript: str, captured_at: str) -> None:
            item_id = idempotency_key("asr", event_key)
            chat_reply_broker.register_voice_turn(item_id, transcript, captured_at)

        drop_dir = tmp_path
        voice_text = "I go for a run every Sunday morning"
        transcriber = _KeyedTranscriber({"turn-voice.wav": voice_text})
        asr_source = ASRSource(
            drop_dir=drop_dir,
            transcriber=transcriber,
            poll_interval_seconds=0.01,
            clock=lambda: _FIXED_NOW,
            turn_hook=asr_turn_hook,
            context_hook=context_hook,
        )

        # TK-295/TK-308 (DEC-65e): the real composed chat-turn sink, hardened against a raising
        # per-event field projection — covers BOTH sources with one tap.
        chat_turn_sink = build_chat_turn_sink(chat_turn_store, clock=lambda: _FIXED_NOW)
        registry = SourceRegistry(queue, sink=chat_turn_sink)
        registry.register(chat_source)
        registry.register(asr_source)

        # --- the real production Gate (mirrors TK-291/test_drain_pathway_e2e.py's own
        # _build_real_gate_stack) --------------------------------------------------------------
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
            response=ModelResponse(text="Sure thing!", model_id="fake", finish_reason="stop")
        )
        model_factory = lambda guard: shared_model  # noqa: E731

        # TK-293/DEC-65b: a LivePersona (not the frozen fallback) so Mouth.CHAT renders with the
        # real user_name — mirrors test_compose_stage.py's own store-less usage.
        live_persona = LivePersona(DEFAULT_MATRIX, "Steward", user_name="Jim")
        compose_stage = ComposeStage(
            config=_config(), template_composer=TemplateComposer(), live_persona=live_persona
        )
        chat_reply_stage = ChatReplyStage(broker=chat_reply_broker)
        # Voice OFF (no adapter): speech_shape/speak stay quiet pass-throughs for every item here
        # — this module's assertions are about ComposeStage's own prompt/mouth selection and the
        # getting-to-know stores, not the DEC-55/DEC-60 spoken-summary path (already proven by
        # TK-291). This also keeps shared_model.calls indexed 1:1 with compose calls.
        speech_shape_stage = SpeechShapeStage(
            config=_config(), voice_enabled=False, adapter_present=False
        )
        speak_stage = SpeakSink(voice_enabled=False, adapter=None)

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
            # ================================================================== AC(a): typed chat
            typed_item_id = _push_typed_chat(
                chat_source,
                chat_reply_broker,
                text="What's my dog's name again?",
                context_hook=context_hook,
                received_at=_FIXED_NOW,
            )
            await _wait_until(lambda: queue.pending_count() >= 1)

            final_a = await engine.run(
                run_id="run-a-typed",
                session_id="sess-a",
                pathway_id=_PATHWAY_ID,
                initial=_initial_artifact(),
            )
            assert final_a.status is RunStatus.COMPLETED
            assert queue.drain() == []  # acked

            system_msg_a, user_msg_a = shared_model.calls[0]
            assert "personal assistant and companion, chatting with Jim" in system_msg_a.content
            assert "Jim" in system_msg_a.content
            assert system_msg_a.content.endswith(CAPABILITY_CHARTER)
            assert "known_user_context: The user has a dog named Biscuit." in user_msg_a.content
            assert "replying_to:" not in user_msg_a.content  # register is still empty

            compose_steps_a = [s for s in final_a.steps if s.stage_name == "compose"]
            assert len(compose_steps_a) == 1
            composed_a = compose_steps_a[0].result.output
            assert composed_a is not None
            _text_a, item_id_a, item_kind_a, degraded_a = composed_output_from_artifact_data(
                composed_a.data
            )
            assert item_id_a == typed_item_id
            assert item_kind_a is ItemKind.CHAT
            assert degraded_a is False

            # ================================================================== AC(b): voice turn
            last_spoken_register.note_spoken(typed_item_id, "It's Biscuit!")
            audio = b"turn-voice-audio-bytes"
            (drop_dir / "turn-voice.wav").write_bytes(audio)
            await _wait_until(lambda: (drop_dir / "processed" / "turn-voice.wav").exists())
            await _wait_until(lambda: queue.pending_count() >= 1)

            final_b = await engine.run(
                run_id="run-b-voice",
                session_id="sess-b",
                pathway_id=_PATHWAY_ID,
                initial=_initial_artifact(),
            )
            assert final_b.status is RunStatus.COMPLETED
            assert queue.drain() == []  # acked

            system_msg_b, user_msg_b = shared_model.calls[1]
            assert "personal assistant and companion, chatting with Jim" in system_msg_b.content
            assert "Jim" in system_msg_b.content
            assert system_msg_b.content.endswith(CAPABILITY_CHARTER)
            assert "known_user_context: The user has a dog named Biscuit." in user_msg_b.content
            assert "replying_to: It's Biscuit!" in user_msg_b.content

            compose_steps_b = [s for s in final_b.steps if s.stage_name == "compose"]
            assert len(compose_steps_b) == 1
            composed_b = compose_steps_b[0].result.output
            assert composed_b is not None
            _text_b, _item_id_b, item_kind_b, degraded_b = composed_output_from_artifact_data(
                composed_b.data
            )
            assert item_kind_b is ItemKind.CHAT
            assert degraded_b is False

            # =========================================================== AC(c): gmail-kind item
            queue.enqueue(
                QueueItem(
                    idempotency_key="gmail-item-1",
                    payload={
                        "item_kind": "generic",
                        "subject": "Invoice due",
                        "is_timed": True,
                        "seconds_to_event": 0.0,
                        "sender_class": "vip",
                    },
                )
            )
            final_c = await engine.run(
                run_id="run-c-gmail",
                session_id="sess-c",
                pathway_id=_PATHWAY_ID,
                initial=_initial_artifact(),
            )
            assert final_c.status is RunStatus.COMPLETED
            assert queue.drain() == []  # acked

            system_msg_c, user_msg_c = shared_model.calls[2]
            assert "You are Steward, a quiet steward." in system_msg_c.content
            assert system_msg_c.content.endswith(CAPABILITY_CHARTER)
            for grounding_key in (
                "replying_to:",
                "known_user_context:",
                "context_calendar_today:",
                "context_recent_email:",
            ):
                assert grounding_key not in user_msg_c.content

            compose_steps_c = [s for s in final_c.steps if s.stage_name == "compose"]
            assert len(compose_steps_c) == 1
            composed_c = compose_steps_c[0].result.output
            assert composed_c is not None
            _text_c, _item_id_c, item_kind_c, degraded_c = composed_output_from_artifact_data(
                composed_c.data
            )
            assert item_kind_c is ItemKind.GENERIC
            assert degraded_c is False

            # ==================================== AC(d): turns landed in wombat_chat_turns
            landed = chat_turn_store.turns_since(_FIXED_NOW - timedelta(hours=1))
            landed_by_text = {row["text"]: row["voice"] for row in landed}
            assert landed_by_text == {
                "What's my dog's name again?": False,
                voice_text: True,
            }

            # ====================================== AC(e): DreamFactsStage closes the loop
            dream_model = FakeModel(
                response=ModelResponse(
                    text=(
                        "The user goes for a run every Sunday morning.\n"
                        "The user's favorite coffee order is a flat white."
                    ),
                    model_id="fake",
                    finish_reason="stop",
                )
            )
            dream_stage = DreamFactsStage(
                model=dream_model, chat_turns=chat_turn_store, user_facts=user_facts_store
            )
            dream_ctx = StageContextFake(now_fn=lambda: _FIXED_NOW)
            dream_result = await dream_stage.run(dream_ctx)
            assert isinstance(dream_result, Transition)
            assert dream_result.to == "dream_derive"
            assert dream_result.output is not None
            assert dream_result.output.data["new_facts"] == 2

            # the VERY NEXT chat turn's prompt carries a dream-extracted fact.
            _push_typed_chat(
                chat_source,
                chat_reply_broker,
                text="What did I do this weekend?",
                context_hook=context_hook,
                received_at=_FIXED_NOW,
            )
            await _wait_until(lambda: queue.pending_count() >= 1)

            final_e = await engine.run(
                run_id="run-e-post-dream",
                session_id="sess-e",
                pathway_id=_PATHWAY_ID,
                initial=_initial_artifact(),
            )
            assert final_e.status is RunStatus.COMPLETED
            assert queue.drain() == []  # acked

            _system_msg_e, user_msg_e = shared_model.calls[3]
            assert "The user goes for a run every Sunday morning." in user_msg_e.content
        finally:
            await registry.stop()
    finally:
        queue.close()
        daily_ledger.close()
        chat_turn_store.close()
        user_facts_store.close()
