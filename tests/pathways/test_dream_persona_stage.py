"""TK-214 — DreamPersonaStage acceptance criteria (EP-35, DEC-36/DEC-37(h), Q-112 pre-ruled).

In-memory/monkeypatched substrate, ZERO network: mirrors ``tests/pathways/
test_dream_behavior_log_stage.py``'s own idiom — ``event_log`` is a REAL ``BehaviorEventLog`` over
an unreachable DSN (lazy — never actually connects) with ``events_between`` monkeypatched to a
recording/canned/raising double; ``live_persona`` is a REAL ``LivePersona`` over an in-memory
``SettingsStore`` double (TK-243 — the genuine pg round-trip for the event log lives in
``tests/behavior/test_event_log.py``, pg-gated; this module is about ``DreamPersonaStage``'s own
read/decide/apply/journal-line logic).

  AC1 (row mapping + apply): two same-direction in-window ``persona_feedback`` rows step the axis
      exactly once, clamped via ``wombat.persona.commands.apply`` — proven on ``live_persona.
      matrix``, the persisted ``wombat_settings`` persona key, and a caplog INFO line naming
      axis/direction/counts; a ``WOMBAT_TEST_PG_DSN``-gated variant drives it through a real
      ``BehaviorEventLog``. A non-``persona_feedback`` row (e.g. an ``OUTCOME_*`` row a sibling
      writer left) is never counted.
  AC2 (conservative-by-construction): mixed signals, a single (below-threshold) signal, and an
      empty window move NOTHING — ``live_persona.set`` is never called (no store write).
  AC3 (pin custody, DEC-37(h)): a pin (created either via ``set()``'s default ``explicit=True`` or
      via a ``poll_settings``-detected app edit) blocks a qualifying signal within its 7-day
      window but no longer once the pin is 8+ days old; a dream nudge (``explicit=False``) never
      creates a pin, so a second consecutive night's fresh signal steps again.
  AC4 (never-block): a raising collaborator (event log read, or a raising ``live_persona.set``) is
      caught, logged ERROR, and ``run()`` STILL ``Transition``s to ``dream_facts`` (TK-297's
      stage, this stage's downstream neighbor post-splice) — proven both as a direct unit call AND
      end-to-end through a real ``Engine`` drive reaching ``dream_run``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import StageResult, Transition
from cogworx.loop.stage import StageContext
from cogworx.loop.state import RunStatus
from cogworx.model.registry import ModelRegistry
from cogworx.runtime.engine import Engine

from tests.support.stage_context_fake import FakeModel, StageContextFake
from wombat.behavior.event_log import BehaviorEventLog, BehaviorEventRow
from wombat.behavior.event_log import ensure_schema as ensure_behavior_event_log_schema
from wombat.pathways.dream_pathway import (
    DREAM_PATHWAY_ID,
    DreamPersonaStage,
    build_dream_pathway,
    dream_trigger_artifact,
)
from wombat.persona.live import LivePersona
from wombat.persona.matrix import DEFAULT_MATRIX, Brevity, Directness, Warmth
from wombat.settings_store import SettingsStore
from wombat.substrate import cold_boot_bundle

_NOW = datetime(2026, 7, 10, 3, 0, 0, tzinfo=UTC)
_UNREACHABLE_DSN = "postgresql://nonexistent-host-should-never-be-dialed:1/db"


class _FakeStore(SettingsStore):
    """In-memory ``SettingsStore`` double (never opens a real connection — both public methods
    are fully overridden), mirroring ``tests/persona/test_live.py``'s own fake."""

    def __init__(self, *, initial: dict[str, Any] | None = None) -> None:
        super().__init__(dsn="postgresql://unused/fake")
        self._rows: dict[str, Any] = dict(initial or {})
        self.put_calls: list[dict[str, Any]] = []

    def get_all(self) -> dict[str, Any]:
        return dict(self._rows)

    def put(self, mapping: dict[str, Any]) -> None:
        self.put_calls.append(dict(mapping))
        self._rows.update(mapping)


def _row(
    phrase: str, *, event_type: str = "persona_feedback", ts: datetime = _NOW
) -> BehaviorEventRow:
    return BehaviorEventRow(
        idempotency_key=f"persona_feedback:{phrase}:{ts.isoformat()}",
        event_type=event_type,
        source_id="asr",
        timestamp_utc=ts,
        outcome_label=phrase,
        duration_seconds=None,
    )


def _fake_event_log(
    monkeypatch: pytest.MonkeyPatch,
    rows: tuple[BehaviorEventRow, ...],
    *,
    raises: BaseException | None = None,
) -> tuple[BehaviorEventLog, list[tuple[datetime, datetime]]]:
    calls: list[tuple[datetime, datetime]] = []

    def _events_between(self: BehaviorEventLog, start: datetime, end: datetime) -> object:
        calls.append((start, end))
        if raises is not None:
            raise raises
        return rows

    monkeypatch.setattr(BehaviorEventLog, "events_between", _events_between)
    return BehaviorEventLog(_UNREACHABLE_DSN), calls


def _current(live_persona: LivePersona, axis: str) -> object:
    """A narrowing-proof read of one matrix axis's CURRENT value: ``getattr`` with a non-literal
    ``axis`` name returns ``Any`` to mypy, so a later assertion in the same test function is never
    stale-narrowed by an earlier ``is``/``==`` check against the SAME attribute chain before an
    intervening ``await stage.run(...)`` mutated it."""
    return getattr(live_persona.matrix, axis)


# ================================================================================================
# AC1: two same-direction tokens step the axis once; mapped/persisted/journaled
# ================================================================================================


async def test_ac1_two_same_direction_tokens_step_the_axis_once_persisted_and_journaled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = _FakeStore()
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", store=store)
    # DEFAULT_MATRIX.brevity is TERSE (the floor) — "too terse" is brevity/up, a VISIBLE step.
    rows = (_row("too terse"), _row("too terse"))
    event_log, calls = _fake_event_log(monkeypatch, rows)

    stage = DreamPersonaStage(event_log=event_log, live_persona=live_persona)
    ctx = StageContextFake(now_fn=lambda: _NOW)

    with caplog.at_level(logging.INFO, logger="wombat.pathways.dream_pathway"):
        result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "dream_facts"
    assert result.output.data == {
        "stepped": [{"axis": "brevity", "direction": "up", "up_count": 2, "down_count": 0}]
    }

    # The window read: [now - 24h, now].
    assert calls == [(_NOW - timedelta(hours=24), _NOW)]

    # live_persona's in-memory matrix reflects the clamped step.
    assert live_persona.matrix.brevity is Brevity.BALANCED

    # Persisted to wombat_settings (LivePersona.set's own key-level upsert).
    assert store.get_all()["wombat_persona_brevity"] == "balanced"

    # A dream nudge (explicit=False) never stamps a pin.
    assert live_persona.pinned_axes(_NOW) == frozenset()

    # One INFO journal line naming axis/direction/counts (CON-4, motive-free CON-6).
    info_lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "axis=brevity" in line
        and "direction=up" in line
        and "up_count=2" in line
        and "down_count=0" in line
        for line in info_lines
    )


async def test_ac1_a_non_persona_feedback_row_is_never_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeStore()
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", store=store)
    # A sibling writer's OUTCOME_* row happens to share the SAME phrase-shaped outcome_label
    # string — it must never be counted since its event_type isn't 'persona_feedback'.
    rows = (
        _row("too terse", event_type="calendar_conflict"),
        _row("too terse", event_type="calendar_conflict"),
    )
    event_log, _calls = _fake_event_log(monkeypatch, rows)
    stage = DreamPersonaStage(event_log=event_log, live_persona=live_persona)

    result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.output.data == {"stepped": []}
    assert live_persona.matrix == DEFAULT_MATRIX
    assert store.put_calls == []  # live_persona.set was never called


_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping the TK-214 real-Postgres AC1 proof. Start one "
        "with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres"
    ),
)


@pytest.fixture
def clean_table() -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_behavior_event_log_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE wombat_behavior_events")
        conn.commit()


@_requires_pg
async def test_ac1_pg_gated_real_behavior_event_log_round_trip(clean_table: None) -> None:
    assert _DSN is not None
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward")  # store-less — not what this AC covers
    event_store = BehaviorEventLog(_DSN)
    try:
        event_store.upsert(
            idempotency_key="persona_feedback:one",
            event_type="persona_feedback",
            source_id="asr",
            timestamp_utc=_NOW - timedelta(hours=1),
            outcome_label="too terse",
            duration_seconds=None,
        )
        event_store.upsert(
            idempotency_key="persona_feedback:two",
            event_type="persona_feedback",
            source_id="asr",
            timestamp_utc=_NOW - timedelta(minutes=30),
            outcome_label="too terse",
            duration_seconds=None,
        )

        stage = DreamPersonaStage(event_log=event_store, live_persona=live_persona)
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

        assert isinstance(result, Transition)
        assert live_persona.matrix.brevity is Brevity.BALANCED
    finally:
        event_store.close()


# ================================================================================================
# AC2: mixed / single / empty windows move NOTHING
# ================================================================================================


async def test_ac2_mixed_signals_move_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore()
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", store=store)
    event_log, _calls = _fake_event_log(monkeypatch, (_row("too chatty"), _row("too terse")))
    stage = DreamPersonaStage(event_log=event_log, live_persona=live_persona)

    result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.output.data == {"stepped": []}
    assert live_persona.matrix == DEFAULT_MATRIX
    assert store.put_calls == []


async def test_ac2_single_below_threshold_signal_moves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeStore()
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", store=store)
    event_log, _calls = _fake_event_log(monkeypatch, (_row("too terse"),))
    stage = DreamPersonaStage(event_log=event_log, live_persona=live_persona)

    result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.output.data == {"stepped": []}
    assert live_persona.matrix == DEFAULT_MATRIX
    assert store.put_calls == []


async def test_ac2_empty_window_moves_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore()
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", store=store)
    event_log, _calls = _fake_event_log(monkeypatch, ())
    stage = DreamPersonaStage(event_log=event_log, live_persona=live_persona)

    result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.output.data == {"stepped": []}
    assert live_persona.matrix == DEFAULT_MATRIX
    assert store.put_calls == []


# ================================================================================================
# AC3: pin custody — 7-day window, explicit-only, dream nudge never pins
# ================================================================================================


async def test_ac3_a_pin_created_via_set_blocks_within_seven_days_but_not_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeStore()
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", store=store)

    # An explicit (default) set bumps brevity — a real TK-212 voice-command-shaped call — and
    # stamps a pin for brevity at "now" (wall-clock).
    bumped = replace(DEFAULT_MATRIX, brevity=Brevity.BALANCED)
    live_persona.set(bumped)
    stamped_at = datetime.fromisoformat(store.get_all()["wombat_persona_pins"]["brevity"])

    # Two qualifying "up" signals would step brevity BALANCED -> EXPANSIVE if unpinned.
    rows = (_row("too terse"), _row("too terse"))
    event_log, _calls = _fake_event_log(monkeypatch, rows)
    stage = DreamPersonaStage(event_log=event_log, live_persona=live_persona)

    # +3 days: still within the 7-day pin window — the signal is blocked.
    within_window = stamped_at + timedelta(days=3)
    result_blocked = await stage.run(StageContextFake(now_fn=lambda: within_window))
    assert isinstance(result_blocked, Transition)
    assert result_blocked.output.data == {"stepped": []}
    assert live_persona.matrix.brevity is Brevity.BALANCED  # unchanged

    # +8 days: the pin has expired — the SAME signal now steps normally.
    past_window = stamped_at + timedelta(days=8)
    result_stepped = await stage.run(StageContextFake(now_fn=lambda: past_window))
    assert isinstance(result_stepped, Transition)
    assert result_stepped.output.data == {
        "stepped": [{"axis": "brevity", "direction": "up", "up_count": 2, "down_count": 0}]
    }
    assert _current(live_persona, "brevity") == Brevity.EXPANSIVE


async def test_ac3_a_pin_created_via_poll_detected_edit_blocks_within_seven_days_but_not_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeStore()
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", store=store)
    live_persona.poll_settings()  # first beat -- establishes the cursor over an empty table

    # An app-edit (TK-200 UI path): an external writer updates the row, then the standing
    # Sweeper beat picks it up via poll_settings — itself an "explicit" edit (TK-214).
    # BLUNT (the ceiling) leaves room for "too blunt" (directness/down) to move if unpinned.
    store.put({"wombat_persona_directness": "blunt"})
    live_persona.poll_settings()
    assert live_persona.matrix.directness is Directness.BLUNT
    stamped_at = datetime.fromisoformat(store.get_all()["wombat_persona_pins"]["directness"])

    rows = (_row("too blunt"), _row("too blunt"))
    event_log, _calls = _fake_event_log(monkeypatch, rows)
    stage = DreamPersonaStage(event_log=event_log, live_persona=live_persona)

    # +3 days: still within the 7-day pin window — the signal is blocked.
    within_window = stamped_at + timedelta(days=3)
    result_blocked = await stage.run(StageContextFake(now_fn=lambda: within_window))
    assert isinstance(result_blocked, Transition)
    assert result_blocked.output.data == {"stepped": []}
    assert live_persona.matrix.directness is Directness.BLUNT  # unchanged

    # +8 days: the pin has expired — the SAME signal now steps normally.
    past_window = stamped_at + timedelta(days=8)
    result_stepped = await stage.run(StageContextFake(now_fn=lambda: past_window))
    assert isinstance(result_stepped, Transition)
    assert result_stepped.output.data == {
        "stepped": [{"axis": "directness", "direction": "down", "up_count": 0, "down_count": 2}]
    }
    assert _current(live_persona, "directness") == Directness.PLAIN


async def test_ac3_a_dream_nudge_never_pins_a_second_night_can_step_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", store=_FakeStore())
    rows = (_row("too stiff"), _row("too stiff"))  # warmth/up
    event_log, _calls = _fake_event_log(monkeypatch, rows)
    stage = DreamPersonaStage(event_log=event_log, live_persona=live_persona)

    first = await stage.run(StageContextFake(now_fn=lambda: _NOW))
    assert isinstance(first, Transition)
    assert live_persona.matrix.warmth is Warmth.NEUTRAL  # RESERVED -> NEUTRAL
    assert live_persona.pinned_axes(_NOW) == frozenset()  # the dream nudge never pins

    second_night = _NOW + timedelta(hours=24)
    second = await stage.run(StageContextFake(now_fn=lambda: second_night))
    assert isinstance(second, Transition)
    assert _current(live_persona, "warmth") == Warmth.WARM  # steps AGAIN — never blocked by a pin
    assert first.output.data == {
        "stepped": [{"axis": "warmth", "direction": "up", "up_count": 2, "down_count": 0}]
    }
    assert second.output.data == first.output.data  # both nights stepped identically


# ================================================================================================
# AC4: never-block — a raising collaborator is caught, logged, and the stage still transitions
# ================================================================================================


async def test_ac4_raising_event_log_is_caught_logged_and_still_transitions(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", store=_FakeStore())
    event_log, _calls = _fake_event_log(
        monkeypatch, (), raises=RuntimeError("simulated event-log read failure — AC4")
    )
    stage = DreamPersonaStage(event_log=event_log, live_persona=live_persona)

    with caplog.at_level(logging.ERROR, logger="wombat.pathways.dream_pathway"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_facts"
    assert result.output.data == {"stepped": []}
    assert live_persona.matrix == DEFAULT_MATRIX
    assert any(
        record.levelno == logging.ERROR and "failed" in record.message
        for record in caplog.records
    )


async def test_ac4_raising_live_persona_set_is_caught_logged_and_still_transitions(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", store=_FakeStore())

    def _boom(self: LivePersona, matrix: object, *, explicit: bool = True) -> None:
        raise RuntimeError("simulated LivePersona.set failure — AC4")

    monkeypatch.setattr(LivePersona, "set", _boom)
    event_log, _calls = _fake_event_log(monkeypatch, (_row("too terse"), _row("too terse")))
    stage = DreamPersonaStage(event_log=event_log, live_persona=live_persona)

    with caplog.at_level(logging.ERROR, logger="wombat.pathways.dream_pathway"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_facts"
    assert any(record.levelno == logging.ERROR for record in caplog.records)


@dataclass
class _PassthroughStage:
    """A trivial always-transitions-onward double standing in for whichever real dream stage this
    module doesn't exercise (mirrors ``test_dream_behavior_log_stage.py``'s own passthrough-stage
    convention) — this module's ACs are about ``DreamPersonaStage`` alone."""

    name: str
    to: str
    transitions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.transitions = (self.to,)

    async def run(self, ctx: StageContext) -> StageResult:
        return Transition(
            to=self.to,
            output=Artifact(
                kind="test.passthrough",
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={},
            ),
        )


async def test_ac4_engine_drive_completes_even_when_the_event_log_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a REAL ``Engine`` drives ``wombat.dream`` through ``dream_persona`` with a
    raising event log — the run still reaches COMPLETED (dream_run)."""
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", store=_FakeStore())
    event_log, _calls = _fake_event_log(
        monkeypatch, (), raises=RuntimeError("simulated event-log read failure — AC4 engine")
    )
    persona_stage = DreamPersonaStage(event_log=event_log, live_persona=live_persona)

    bundle = cold_boot_bundle()
    dream_graph = build_dream_pathway(
        _PassthroughStage(name="dream_consolidate", to="dream_outcome"),
        _PassthroughStage(name="dream_outcome", to="dream_tune"),
        _PassthroughStage(name="dream_tune", to="dream_persona"),
        persona_stage,
        _PassthroughStage(name="dream_facts", to="dream_derive"),
        _PassthroughStage(name="dream_derive", to="dream_observe"),
        _PassthroughStage(name="dream_observe", to="dream_screenpipe"),
        _PassthroughStage(name="dream_screenpipe", to="dream_behavior_log"),
        _PassthroughStage(name="dream_behavior_log", to="dream_window"),
        _PassthroughStage(name="dream_window", to="dream_pattern"),
        _PassthroughStage(name="dream_pattern", to="dream_run"),
    )
    bundle.pathways.register(DREAM_PATHWAY_ID, dream_graph)

    models = ModelRegistry()
    models.register_factory(
        "deepseek",
        lambda guard: FakeModel(raises=AssertionError("dream stages never call the mouth")),
    )
    engine = Engine(
        models=models,
        journal=bundle.journal,
        graph_store=bundle.graph_store,
        latent=bundle.latent,
        pathways=bundle.pathways,
        model_profile="deepseek",
        clock=lambda: _NOW,
    )

    final = await engine.run(
        run_id="run-ac4-engine",
        session_id="run-ac4-engine",
        pathway_id=DREAM_PATHWAY_ID,
        initial=dream_trigger_artifact(_NOW),
    )

    assert final.status is RunStatus.COMPLETED
    stage_names = [step.stage_name for step in final.steps]
    assert stage_names[-9:] == [
        "dream_persona",
        "dream_facts",
        "dream_derive",
        "dream_observe",
        "dream_screenpipe",
        "dream_behavior_log",
        "dream_window",
        "dream_pattern",
        "dream_run",
    ]
