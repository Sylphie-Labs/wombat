"""TK-1 — wombat composition root acceptance criteria."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.graph import StageGraph
from cogworx.loop.pathway import PathwayRegistry
from cogworx.loop.result import StageResult, Transition
from cogworx.loop.state import RunStatus
from cogworx.model.registry import ModelRegistry
from cogworx.runtime.engine import Engine
from cogworx.testing.doubles import InMemoryGraphStore, InMemoryJournal, InMemoryLatentStore
from google.auth.exceptions import RefreshError
from pydantic import SecretStr

import wombat.sources.bootstrap as sources_bootstrap_module
from tests.support.stage_context_fake import FakeModel
from wombat import bootstrap
from wombat.bootstrap import (
    _ENGINE_MAX_STEPS,
    MODEL_PROFILE,
    _log_engine_event,
    build_engine,
    reset_engine,
)
from wombat.config import ConfigurationError, WombatConfig, load_config
from wombat.domain.item_identity import idempotency_key as derive_key
from wombat.external_store import ExternalItemStore
from wombat.gate.pending_set import InMemoryPendingJournal, PendingSet
from wombat.params import load_operating_params
from wombat.pathways.brief_pathway import brief_timer_tick_artifact, build_brief_schedule_pathway
from wombat.scratchpad import ScratchpadStore
from wombat.sources.seen_ledger import DedupingEnqueuer, SeenLedger
from wombat.stages.brief_timer_stage import BriefTimerStage
from wombat.substrate import cold_boot_bundle
from wombat.voice.reply_context import LastSpokenRegister

# The ten seams the Engine must carry after composition (4 required substrate + 6 optional).
_ENGINE_SEAMS = (
    "_models",
    "_journal",
    "_graph_store",
    "_latent",
    "_pathways",
    "_budget_policy",
    "_registry",
    "_recall_stack",
    "_personality",
    "_rules",
)


@pytest.fixture(autouse=True)
def _reset_singleton() -> Iterator[None]:
    reset_engine()
    yield
    reset_engine()


def _config() -> WombatConfig:
    return WombatConfig(deepseek_api_key="sk-test", deepseek_base_url="https://api.deepseek.com")


def test_ac1_cold_launch_returns_engine_with_all_ten_seams() -> None:
    engine = build_engine(cold_boot_bundle(), config=_config())
    for seam in _ENGINE_SEAMS:
        assert getattr(engine, seam) is not None, f"seam {seam} is None"
    assert engine._model_profile == MODEL_PROFILE


def test_ac2_missing_api_key_raises_configuration_error_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # TK-186: chdir off the repo root so a real developer .env (if any) can't supply the
    # missing key out from under this test -- pydantic-settings resolves env_file=".env"
    # relative to CWD.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    with pytest.raises(ConfigurationError, match="DEEPSEEK_API_KEY"):
        load_config()


def test_ac2_missing_base_url_raises_configuration_error_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    with pytest.raises(ConfigurationError, match="DEEPSEEK_BASE_URL"):
        load_config()


def test_ac3_second_call_returns_same_singleton_no_duplicate() -> None:
    first = build_engine(cold_boot_bundle(), config=_config())
    second = build_engine(cold_boot_bundle(), config=_config())
    assert first is second


def test_deepseek_profile_registered_as_spec_no_model_built() -> None:
    # The model is a descriptor only — composition stays model-silent (registry resolves the spec).
    engine = build_engine(cold_boot_bundle(), config=_config())
    registry = engine._models
    assert registry.resolve_spec(MODEL_PROFILE) is not None


def test_module_exposes_build_engine() -> None:
    assert callable(bootstrap.build_engine)


# --- TK-283 (DEC-61): mouth_model_timeout_seconds is injected at every model-calling mouth site -

_FAKE_DSN = "postgresql://fake/db"

# A value distinct from every mouth stage's own ctor default (2.0 or 10.0) so a passing
# assertion proves the tunable was actually threaded through, not a coincidental match.
_DISTINCT_TIMEOUT = 7.25


class _FakeDraftTrailWriter:
    """Minimal ``DraftTrailWriter`` fake for construction-only tests (never called here)."""

    def record_proposal(
        self,
        *,
        action_id: str,
        action_type: Any,
        human_summary: str,
        target: str,
        proposed_at: datetime,
    ) -> object:
        raise NotImplementedError

    def record_refusal(
        self,
        *,
        action_id: str,
        human_summary: str,
        target: str,
        proposed_at: datetime,
    ) -> object:
        raise NotImplementedError


def _op_with_timeout(timeout_seconds: float) -> Any:
    return load_operating_params().model_copy(
        update={"mouth_model_timeout_seconds": timeout_seconds}
    )


def test_build_compose_stage_injects_mouth_model_timeout_seconds() -> None:
    op = _op_with_timeout(_DISTINCT_TIMEOUT)
    stage = bootstrap.build_compose_stage(
        config=_config(), dsn=_FAKE_DSN, params=op, tz=ZoneInfo("UTC")
    )
    assert stage._timeout_seconds == _DISTINCT_TIMEOUT


def test_build_brief_compose_stage_injects_mouth_model_timeout_seconds() -> None:
    op = _op_with_timeout(_DISTINCT_TIMEOUT)
    stage = bootstrap.build_brief_compose_stage(
        config=_config(), dsn=_FAKE_DSN, params=op, tz=ZoneInfo("UTC")
    )
    assert stage._timeout_seconds == _DISTINCT_TIMEOUT


def test_build_speech_shape_stage_injects_mouth_model_timeout_seconds() -> None:
    op = _op_with_timeout(_DISTINCT_TIMEOUT)
    stage = bootstrap.build_speech_shape_stage(
        config=_config(), dsn=_FAKE_DSN, params=op, tz=ZoneInfo("UTC"), adapter_present=False
    )
    assert stage._timeout_seconds == _DISTINCT_TIMEOUT


def test_build_draft_composer_stage_default_preserves_ctor_default() -> None:
    # No timeout_seconds passed -- the standalone-caller posture must keep DraftComposer's own
    # ctor default byte-identical (AC4).
    from wombat.integrations.gmail.draft_composer import _DEFAULT_TIMEOUT_SECONDS

    stage = bootstrap.build_draft_composer_stage(writer=_FakeDraftTrailWriter())
    assert stage._timeout_seconds == _DEFAULT_TIMEOUT_SECONDS


def test_build_draft_composer_stage_forwards_mouth_model_timeout_seconds_when_given() -> None:
    stage = bootstrap.build_draft_composer_stage(
        writer=_FakeDraftTrailWriter(), timeout_seconds=_DISTINCT_TIMEOUT
    )
    assert stage._timeout_seconds == _DISTINCT_TIMEOUT


def test_assemble_runtime_compose_stage_carries_mouth_model_timeout_seconds() -> None:
    op = _op_with_timeout(_DISTINCT_TIMEOUT)
    bundle = bootstrap.assemble_runtime(
        config=_config(),
        dsn="postgresql://fake-host/fake-db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )
    assert bundle.compose_stage._timeout_seconds == _DISTINCT_TIMEOUT


def test_assemble_runtime_reflection_compose_stage_carries_mouth_model_timeout_seconds() -> None:
    op = _op_with_timeout(_DISTINCT_TIMEOUT)
    bundle = bootstrap.assemble_runtime(
        config=_config(),
        dsn="postgresql://fake-host/fake-db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )
    graph = bundle.pathways.get(bundle.drain_pathway_id)
    stage = graph.get("reflection_compose")
    assert getattr(stage, "_timeout_seconds") == _DISTINCT_TIMEOUT  # noqa: B009


# --- TK-101: WOMBAT_BRIEF_PATH / WOMBAT_VOICE_ENABLED are OPTIONAL -------------------------------


def test_wombat_config_boots_without_brief_path_or_voice_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # TK-202 (CR3-4, Q-103): chdir off the repo root so a populated operator .env can't supply
    # WOMBAT_BRIEF_PATH/WOMBAT_VOICE_ENABLED out from under this test -- delenv only clears the
    # process env var, and pydantic-settings resolves env_file=".env" relative to CWD (mirrors
    # TK-186's test_ac2_missing_api_key_raises... precedent above).
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WOMBAT_BRIEF_PATH", raising=False)
    monkeypatch.delenv("WOMBAT_VOICE_ENABLED", raising=False)
    config = _config()  # must not raise -- neither is in REQUIRED_ENV
    assert config.wombat_brief_path is None
    assert config.wombat_voice_enabled is False


# --- TK-172 (CR-10): the mid-batch-surface/whole-batch-ack coupling guard -----------------------


def test_guard_drain_batch_size_raises_for_non_one() -> None:
    with pytest.raises(ValueError, match="mid-batch"):
        bootstrap._guard_drain_batch_size(2)


def test_guard_drain_batch_size_noop_for_one() -> None:
    bootstrap._guard_drain_batch_size(1)  # must not raise


def test_assemble_runtime_still_succeeds_at_current_batch_size_of_one() -> None:
    """AC1: the guard is a no-op at the current composition (_DRAIN_BATCH_SIZE == 1) -- assembly
    is byte-identical, no new raise on the real boot path."""
    op = load_operating_params()
    # A fake Postgres DSN -- every adapter assemble_runtime wires is lazy (no connection at
    # construction) with replay_pending=False, so this never touches a real Postgres (mirrors
    # tests/unit/test_runtime.py).
    bundle = bootstrap.assemble_runtime(
        config=_config(),
        dsn="postgresql://fake-host/fake-db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )
    assert bundle.drain_pathway_id == bootstrap.DRAIN_PATHWAY_ID


# --- TK-166 (CR-1, Q-83): replay_pending is the ONE eager-read boot-replay flag -----------------


def test_assemble_runtime_default_replay_pending_calls_rebuild_from_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The DEFAULT (``replay_pending=True``, the ``serve()`` production posture) routes the
    gate's pending set through ``PendingSet.rebuild_from_journal`` -- proven via a spy that
    returns a COLD ``PendingSet`` so no real I/O ever happens against the fake DSN."""
    op = load_operating_params()
    calls: list[object] = []
    cold = PendingSet(journal=InMemoryPendingJournal(), max_pending=op.max_pending)

    def spy_rebuild(journal: object, *, max_pending: int) -> PendingSet:
        calls.append(journal)
        return cold

    monkeypatch.setattr(PendingSet, "rebuild_from_journal", spy_rebuild)
    # TK-203 (Q-104): the schema pre-flight also runs unconditionally on this replay_pending=True
    # posture, ahead of rebuild_from_journal -- stubbed out here (a real, separate connection
    # attempt against the fake DSN) so this test stays about ONE thing: rebuild_from_journal
    # routing. Real pg-backed pre-flight coverage lives in tests/unit/test_schema_preflight.py.
    monkeypatch.setattr(bootstrap, "ensure_all_schemas", lambda dsn: None)

    bundle = bootstrap.assemble_runtime(
        config=_config(), dsn="postgresql://fake-host/fake-db", params=op, tz=ZoneInfo("UTC")
    )

    assert len(calls) == 1  # the default path calls rebuild_from_journal exactly once
    assert bundle.drain_pathway_id == bootstrap.DRAIN_PATHWAY_ID


def test_assemble_runtime_replay_pending_false_never_calls_rebuild_from_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``replay_pending=False`` never touches ``PendingSet.rebuild_from_journal`` -- the cold
    constructor stands, so a fake/unreachable DSN stays connection-free."""
    op = load_operating_params()
    calls: list[object] = []
    real_rebuild = PendingSet.rebuild_from_journal

    def spy_rebuild(journal: object, *, max_pending: int) -> PendingSet:
        calls.append(journal)
        return real_rebuild(journal, max_pending=max_pending)  # type: ignore[arg-type]

    monkeypatch.setattr(PendingSet, "rebuild_from_journal", spy_rebuild)

    bundle = bootstrap.assemble_runtime(
        config=_config(),
        dsn="postgresql://fake-host/fake-db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )

    assert calls == []  # never called -- the opted-out path stays connection-free
    assert bundle.drain_pathway_id == bootstrap.DRAIN_PATHWAY_ID


# --- TK-269 (DEC-56a): RuntimeBundle.chat_source mirrors chat_surface's None/non-None shape ------


def test_assemble_runtime_chat_source_is_none_when_chat_handshake_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # TK-186/TK-202 chdir+delenv precedent (see test_wombat_config_boots_without_brief_path_or_
    # voice_env above): an operator .env may set WOMBAT_CHAT_HANDSHAKE_FILE, so isolate from it.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WOMBAT_CHAT_HANDSHAKE_FILE", raising=False)
    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(
        config=_config(),  # wombat_chat_handshake_file blank/absent -> chat disabled
        dsn="postgresql://fake-host/fake-db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )
    assert bundle.chat_surface is None
    assert bundle.chat_source is None


def test_assemble_runtime_chat_source_is_the_same_instance_registered_into_source_registry(
    tmp_path: Path,
) -> None:
    """TK-269 WIRING: ``bundle.chat_source`` is the EXACT ``ChatSource`` instance ``source_
    registry`` already has registered — a pass-through, not a second construction — so ``runtime.
    _drive_and_serve`` wiring a wake onto it reaches the SAME source the registry polls."""
    op = load_operating_params()
    config = _config().model_copy(
        update={"wombat_chat_handshake_file": str(tmp_path / "chat_handshake.json")}
    )
    bundle = bootstrap.assemble_runtime(
        config=config,
        dsn="postgresql://fake-host/fake-db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )
    assert bundle.chat_surface is not None
    assert bundle.chat_source is not None
    assert bundle.chat_source is bundle.chat_surface._source
    assert bundle.chat_source.wake is None  # unwired at assembly time -- _drive_and_serve's job


# --- TK-280 (DEC-60c server half): the ASR turn_hook -> voice-turn ledger wiring ------------------


class _FakeVoiceTranscriber:
    def __init__(self, text: str) -> None:
        self._text = text

    def transcribe(self, path: Path) -> str:
        return self._text


async def test_assemble_runtime_wires_asr_turn_hook_into_the_voice_turn_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TK-280: with chat enabled, ASRSource's turn_hook registers a real drop-dir transcript into
    the SAME broker's voice-turn ledger, under the item_id derived via idempotency_key('asr',
    event_key) -- the EXACT canonical derivation sources/registry.py's own enqueue path uses
    (ASRSource.id == 'asr'), proving the composition-root closure and the registry agree."""
    monkeypatch.setattr(
        sources_bootstrap_module,
        "build_transcriber",
        lambda config: _FakeVoiceTranscriber("what's the weather"),
    )
    drop_dir = tmp_path / "drop"
    drop_dir.mkdir()
    (drop_dir / "note.wav").write_bytes(b"turn-hook-wiring-bytes")

    op = load_operating_params()
    config = _config().model_copy(
        update={
            "wombat_chat_handshake_file": str(tmp_path / "chat_handshake.json"),
            "wombat_asr_drop_dir": str(drop_dir),
        }
    )
    bundle = bootstrap.assemble_runtime(
        config=config,
        dsn="postgresql://fake-host/fake-db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )
    assert bundle.chat_surface is not None
    broker = bundle.chat_surface._broker
    asr_source = bundle.source_registry._sources["asr"]

    events = await asr_source.poll()

    assert len(events) == 1
    expected_id = derive_key("asr", events[0].event_key)
    assert broker.voice_turns_snapshot() == [
        {
            "id": expected_id,
            "transcript": "what's the weather",
            "captured_at": events[0].payload["captured_at"],
            "reply": None,
        }
    ]


def test_assemble_runtime_asr_turn_hook_is_none_when_chat_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TK-280: chat disabled (blank handshake path, broker None) -> ASRSource's turn_hook stays
    None -- a byte-identical ASRSource, no ledger side effect wired at all."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WOMBAT_CHAT_HANDSHAKE_FILE", raising=False)
    captured_kwargs: dict[str, Any] = {}

    class _SpyASRSource:
        id: str = "asr"

        def __init__(self, **kwargs: Any) -> None:
            captured_kwargs.update(kwargs)
            self.poll_interval_seconds = kwargs.get("poll_interval_seconds", 1.0)

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

        async def poll(self) -> list[Any]:
            return []

    monkeypatch.setattr(sources_bootstrap_module, "ASRSource", _SpyASRSource)
    monkeypatch.setattr(sources_bootstrap_module, "build_transcriber", lambda config: object())

    op = load_operating_params()
    config = _config().model_copy(update={"wombat_asr_drop_dir": str(tmp_path)})
    bundle = bootstrap.assemble_runtime(
        config=config,
        dsn="postgresql://fake-host/fake-db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )

    assert bundle.chat_surface is None  # chat disabled
    assert captured_kwargs["turn_hook"] is None


# --- TK-46 (Q-85): wombat.dream registers UNCONDITIONALLY, connection-free -----------------------


# --- TK-288 (DEC-64 gap A, v2.151 ruling): ONE shared LastSpokenRegister threaded into BOTH the
# drain-graph SpeakSink and the brief pathway's BriefDeliverStage ---------------------------------


def test_assemble_runtime_wires_one_shared_last_spoken_register_into_both_speak_sites(
    tmp_path: Path,
) -> None:
    op = load_operating_params()
    config = _config().model_copy(
        update={"wombat_brief_path": str(tmp_path / "brief.txt")}
    )
    bundle = bootstrap.assemble_runtime(
        config=config,
        dsn="postgresql://fake-host/fake-db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )

    assert bundle.brief_pathway_id is not None
    speak_stage = bundle.pathways.get(bundle.drain_pathway_id).get("speak")
    brief_deliver_stage = bundle.pathways.get(bundle.brief_pathway_id).get("brief_deliver")

    speak_on_spoken = getattr(speak_stage, "_on_spoken")  # noqa: B009
    brief_on_spoken = getattr(brief_deliver_stage, "_on_spoken")  # noqa: B009
    assert speak_on_spoken is not None
    assert brief_on_spoken is not None
    # Both are the SAME register's note_spoken bound method -- one shared instance, not two.
    assert isinstance(speak_on_spoken.__self__, LastSpokenRegister)
    assert speak_on_spoken.__self__ is brief_on_spoken.__self__

    register = speak_on_spoken.__self__
    assert register.current() is None
    speak_on_spoken("i-1", "spoken via drain")
    assert register.current() == "spoken via drain"
    brief_on_spoken("brief:run-1", "spoken via brief")
    assert register.current() == "spoken via brief"


# --- TK-289 (DEC-64 gap A, half 2): the ASR context_hook -> LastSpokenRegister wiring -------------


async def test_assemble_runtime_wires_asr_context_hook_reading_the_shared_register(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TK-289: a fresh (within-TTL) register entry stamps replying_to onto a real drop-dir
    transcript's payload -- proving the composition-root closure reads the SAME shared
    last_spoken_register the drain/brief speak sites feed (TK-288)."""
    monkeypatch.setattr(
        sources_bootstrap_module,
        "build_transcriber",
        lambda config: _FakeVoiceTranscriber("yes, do that"),
    )
    drop_dir = tmp_path / "drop"
    drop_dir.mkdir()
    (drop_dir / "note.wav").write_bytes(b"context-hook-wiring-bytes")

    op = load_operating_params()
    config = _config().model_copy(
        update={"wombat_asr_drop_dir": str(drop_dir)}
    )
    bundle = bootstrap.assemble_runtime(
        config=config,
        dsn="postgresql://fake-host/fake-db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )
    asr_source = bundle.source_registry._sources["asr"]

    speak_stage = bundle.pathways.get(bundle.drain_pathway_id).get("speak")
    register = getattr(speak_stage, "_on_spoken").__self__  # noqa: B009
    assert isinstance(register, LastSpokenRegister)

    # Before anything has been spoken, the register is empty -- no replying_to key.
    events_before = await asr_source.poll()
    assert "replying_to" not in events_before[0].payload

    # Note something spoken, drop a second file -- the fresh text stamps this one.
    register.note_spoken("i-spoken", "Should I send the reply now?")
    (drop_dir / "note2.wav").write_bytes(b"context-hook-wiring-bytes-2")
    events_after = await asr_source.poll()
    assert events_after[0].payload["replying_to"] == "Should I send the reply now?"


# --- TK-245 (ruling v2.68 r5): assemble_runtime ALWAYS constructs ExternalItemStore(dsn) ------


def test_assemble_runtime_exposes_a_real_external_item_store() -> None:
    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(
        config=_config(),
        dsn="postgresql://fake-host/fake-db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )
    assert isinstance(bundle.external_item_store, ExternalItemStore)


# --- TK-247 (ruling v2.68 r5): assemble_runtime ALWAYS constructs ScratchpadStore(dsn) ---------


def test_assemble_runtime_exposes_a_real_scratchpad_store() -> None:
    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(
        config=_config(),
        dsn="postgresql://fake-host/fake-db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )
    assert isinstance(bundle.scratchpad_store, ScratchpadStore)


# --- TK-286 (DEC-63a): the DedupingEnqueuer/SeenLedger seam wires into build_source_registry
# ONLY -- PatternDetectorStage keeps the raw queue.enqueue byte-untouched -----------------------


def test_assemble_runtime_wires_deduping_enqueuer_into_source_registry_only() -> None:
    """AC6: ``source_registry`` is driven by a ``DedupingEnqueuer`` wrapping the SAME shared
    ``bundle.queue`` (never a second queue instance), while the nightly ``dream_pattern`` stage
    (``PatternDetectorStage``) keeps the raw, un-deduped ``bundle.queue.enqueue`` -- an internally
    -derived pattern event, not a re-polled source item, must never be silently swallowed by the
    seen-ledger."""
    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(
        config=_config(),
        dsn="postgresql://fake-host/fake-db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )

    deduping_enqueuer = bundle.source_registry._enqueue
    assert isinstance(deduping_enqueuer, DedupingEnqueuer)
    assert deduping_enqueuer._inner is bundle.queue
    assert isinstance(deduping_enqueuer._ledger, SeenLedger)

    dream_graph = bundle.pathways.get(bundle.dream_pathway_id)
    pattern_stage = dream_graph.get("dream_pattern")
    # raw, byte-untouched (bound method equality) -- getattr keeps this mypy-clean over Stage
    assert getattr(pattern_stage, "_enqueue") == bundle.queue.enqueue  # noqa: B009


def test_assemble_runtime_registers_dream_pathway_unconditionally() -> None:
    """The TK-166 connection-free assembly pattern (``replay_pending=False``, a fake DSN) proves
    ``wombat.dream`` is registered on the SAME resolvable pathway registry the drain pathway is —
    no ``WOMBAT_BRIEF_PATH``-style conditional gates it (Q-85)."""
    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(
        config=_config(),
        dsn="postgresql://fake-host/fake-db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )
    assert bundle.dream_pathway_id == "wombat.dream"
    assert bundle.pathways.get(bundle.dream_pathway_id) is not None


# --- TK-114 (EP-22, Q-102b-f): the reflection-render leg registers UNCONDITIONALLY ----------------


def test_assemble_runtime_registers_reflection_compose_in_drain_graph() -> None:
    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(
        config=_config(),
        dsn="postgresql://fake-host/fake-db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )
    graph = bundle.pathways.get(bundle.drain_pathway_id)

    assert "reflection_compose" in graph.names()
    # ComposeDispatchRouter's own declared edges cover the injected composer_by_kind map (Q-51) —
    # this proves ItemKind.REFLECTION routes to "reflection_compose" structurally.
    assert "reflection_compose" in graph.transitions_from("compose_dispatch")
    stage = graph.get("reflection_compose")
    assert stage.transitions == ()  # TERMINAL by ruling (Q-102c)


def test_assemble_runtime_reflection_kb_load_failure_boots_with_empty_kb_and_loud_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """CON-3: a psychology-KB load failure never fails the whole boot — ReflectionComposeStage
    is constructed with an empty kb and ONE loud warning is logged."""

    def _raise(path: Path | None = None) -> list[object]:
        raise FileNotFoundError("kb missing")

    monkeypatch.setattr(bootstrap, "load_psychology_kb", _raise)
    op = load_operating_params()

    with caplog.at_level("WARNING"):
        bundle = bootstrap.assemble_runtime(
            config=_config(),
            dsn="postgresql://fake-host/fake-db",
            params=op,
            replay_pending=False,
            tz=ZoneInfo("UTC"),
        )

    graph = bundle.pathways.get(bundle.drain_pathway_id)
    stage = graph.get("reflection_compose")
    assert stage._kb == []  # type: ignore[attr-defined]

    matching = [
        r
        for r in caplog.records
        if "ReflectionComposeStage boots with an empty KB" in r.getMessage()
    ]
    assert len(matching) == 1
    assert matching[0].levelname == "WARNING"


# --- TK-253 (DEC-49, CRF-6 precedent): expired stored gmail token degrades like absent -----------


class _FakeTokenStore:
    def __init__(self, *, initial: str | None = None) -> None:
        self._value = initial

    def load(self) -> str | None:
        return self._value

    def save(self, token: str) -> None:
        self._value = token

    def clear(self) -> None:
        self._value = None


def _google_config() -> WombatConfig:
    return WombatConfig(
        deepseek_api_key="sk-test",
        deepseek_base_url="https://api.deepseek.com",
        google_oauth_client_id="test-client-id",
        google_oauth_client_secret=SecretStr("test-client-secret"),
    )


def test_assemble_runtime_expired_gmail_token_degrades_like_google_less_boot(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC1: client creds configured + a stored gmail token present, but the token is expired/
    revoked (``make_gmail_session`` raises ``RefreshError``) -- ``assemble_runtime`` COMPLETES,
    no ``drafts.create`` capability, no DRAFT composer route, drain graph byte-identical to a
    Google-less boot; the WARNING names the gmail re-consent command."""
    op = load_operating_params()

    def _raise_refresh_error(config: WombatConfig, *, token_store: object) -> object:
        raise RefreshError("stored gmail token is expired/revoked")  # type: ignore[no-untyped-call]

    monkeypatch.setattr(bootstrap, "make_gmail_session", _raise_refresh_error)

    google_less = bootstrap.assemble_runtime(
        config=_config(),
        dsn="postgresql://fake-host/fake-db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )
    baseline_stages = set(google_less.pathways.get(google_less.drain_pathway_id).names())

    with caplog.at_level("WARNING"):
        bundle = bootstrap.assemble_runtime(
            config=_google_config(),
            dsn="postgresql://fake-host/fake-db",
            params=op,
            replay_pending=False,
            tz=ZoneInfo("UTC"),
            gmail_token_store=_FakeTokenStore(initial="expired-token"),
            # TK-254 (ISS-10(a)): _google_config() sets Google client creds directly, so
            # without an injected gcal store this would fall through to the real OS-keyring
            # GcalKeyringTokenStore (tripped by the root conftest hermeticity guard) even
            # though this test exercises the gmail seam only.
            gcal_token_store=_FakeTokenStore(),
        )

    graph = bundle.pathways.get(bundle.drain_pathway_id)
    assert set(graph.names()) == baseline_stages  # byte-identical to the Google-less boot
    assert "draft_composer" not in graph.names()
    assert "gmail outbound wiring not wired" in caplog.text
    assert "stored Gmail credential failed to refresh" in caplog.text
    assert "python -m wombat.integrations.gmail.auth" in caplog.text


# --- CRF-3 (DEC-41(e)): build_engine pins max_steps=100_000 + a logging event_sink --------------
# so a run's terminal RUN_FAILED (e.g. the max_steps ceiling tripping) is never dropped into a
# None sink and dies silent.


def test_ac1_build_engine_pins_max_steps_and_wombat_event_sink_never_none() -> None:
    engine = build_engine(cold_boot_bundle(), config=_config())
    assert engine._max_steps == 100_000 == _ENGINE_MAX_STEPS
    assert engine._event_sink is _log_engine_event
    assert engine._event_sink is not None


class _LooperStage:
    """A trivial self-looping stage (AC3 harness): every visit is a plain ``Transition`` back to
    itself, never a ``Wait`` — so ``seq`` climbs by one on every drive iteration until the
    engine's ``max_steps`` ceiling trips. ``looper_terminal`` is a declared-but-never-taken stub,
    mirroring ``BriefTimerTerminalStage``'s precedent for satisfying the "graph can end"
    structural invariant without changing runtime behavior.
    """

    name = "looper"
    transitions: tuple[str, ...] = ("looper", "looper_terminal")

    async def run(self, ctx: object) -> StageResult:
        return Transition(
            to="looper",
            output=Artifact(
                kind="tick",
                produced_by="looper",
                provenance=_system_provenance(),
                data={},
            ),
        )


class _LooperTerminalStage:
    name = "looper_terminal"
    transitions: tuple[str, ...] = ()

    async def run(self, ctx: object) -> StageResult:  # pragma: no cover - never reached
        raise RuntimeError("looper_terminal must never be entered")


def _looper_pathway() -> StageGraph:
    return StageGraph([_LooperStage(), _LooperTerminalStage()], entry="looper")


def _system_provenance() -> Provenance:
    return Provenance(source="system", confidence=1.0, recorded_at=datetime.now(UTC))


async def test_ac3_max_steps_ceiling_trip_logs_error_naming_run_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A harness Engine built with a tiny ``max_steps`` and the SAME wombat sink
    (``bootstrap._log_engine_event``): when the ceiling trips, the run flips FAILED and the sink
    logs an ERROR record naming the run_id — never a silent death."""
    pathways = PathwayRegistry()
    pathways.register("test.looper", _looper_pathway())
    models = ModelRegistry()
    # The looper stage never touches ctx.model(), but context assembly eagerly assembles ONE
    # regardless — a factory slot satisfies that eager assembly without ever being called.
    models.register_factory("default", lambda guard: FakeModel())
    engine = Engine(
        models=models,
        journal=InMemoryJournal(),
        graph_store=InMemoryGraphStore(),
        latent=InMemoryLatentStore(),
        pathways=pathways,
        max_steps=3,
        event_sink=_log_engine_event,
    )
    run_id = "ceiling-trip"

    with caplog.at_level(logging.ERROR):
        state = await engine.run(
            run_id=run_id,
            session_id=run_id,
            pathway_id="test.looper",
            initial=Artifact(
                kind="tick",
                produced_by="test",
                provenance=_system_provenance(),
                data={},
            ),
        )

    assert state.status is RunStatus.FAILED
    matching = [
        r for r in caplog.records if r.levelname == "ERROR" and run_id in r.getMessage()
    ]
    assert len(matching) == 1


async def test_ac2_brief_timer_shaped_self_park_survives_2000_wakes_never_fails() -> None:
    """AC2: an engine built with ``build_engine``'s kwargs (``max_steps=100_000`` + the wombat
    sink) lets a ``BriefTimerStage``-shaped eternal ``Wait(to=self)`` run (the TK-97/TK-52 shape)
    survive far past cog-worx's 1000-step default — driven past 2000 wakes via
    ``engine.fire_timer``, the run status stays WAITING, never FAILED."""

    async def _never_called_fire_brief(now: datetime) -> object:  # pragma: no cover
        raise AssertionError("fire_brief must never be called -- ran_today() is always True")

    timer_stage = BriefTimerStage(
        fire_brief=_never_called_fire_brief,  # type: ignore[arg-type]
        ran_today=lambda: True,  # always "already ran" -- pure re-park, no fire, every wake
        mark_ran=lambda: 0,
        tz=ZoneInfo("UTC"),
        brief_time=time(7, 0),
    )
    bundle = cold_boot_bundle()
    bundle.pathways.register("test.brief_schedule", build_brief_schedule_pathway(timer_stage))
    engine = build_engine(bundle, config=_config())

    run_id = "self-park-run"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    state = await engine.run(
        run_id=run_id,
        session_id=run_id,
        pathway_id="test.brief_schedule",
        initial=brief_timer_tick_artifact(now),
    )
    assert state.status is RunStatus.WAITING

    for _ in range(2000):
        state = await engine.fire_timer(run_id)
        assert state.status is RunStatus.WAITING  # never FAILED, across every one of 2000 wakes
