"""TK-96 — the walking-skeleton brief pathway assembly, end-to-end (EP-1).

Everything here is IN-MEMORY / fast: no Postgres, no real network (mirrors ``test_drain_pathway_
e2e.py``'s own construction, minus the ``WOMBAT_TEST_PG_DSN`` gate — ``build_brief_pathway`` and
every stage it wires are pure/in-memory seams, so this module needs no real Postgres at all).

A REAL cog-worx ``Engine`` drives ``wombat.brief`` (``build_brief_pathway``) over a REAL
production ``Gate`` (``gate.select_items``, TK-27/Q-30) backed by an in-memory
``InMemoryPendingJournal`` — ``urgency_threshold=-1.0`` guarantees every gathered item clears
``is_surfacing_worthy`` (``scored.urgency > urgency_threshold``, always true for a urgency in
[0,1]), so this module tests ASSEMBLY, not scoring nuances. FAKE ``fetch_calendar``/
``fetch_gmail`` callables stand in for the real pollers (TK-98's own seam); ``BriefComposeStage``/
``BriefDeliverStage`` are constructed directly (no ``dsn``-requiring factory) since layer-2
budget wiring is TK-9's own concern, not this ticket's.

  AC1 one fire of ``wombat.brief`` delivers exactly ONE ``[run=...]`` header to the sink,
      carrying the gathered event + the gathered email's summary; a conflict variant (two
      overlapping events) also carries a conflict note.
  AC2 (CON-1) the sealed ``BriefDecisionArtifact`` ``brief_force_flush`` produces renders, via
      ``render_brief_lines``, to EXACTLY the body the fake model's captured prompt carries —
      the compose stage never re-derives or diverges from the sealed contents.
  AC3 (Q-77) a raising model degrades cleanly: the delivered sink text equals
      ``render_brief_lines(...)`` of the sealed decision EXACTLY (template == fallback).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from cogworx.loop.pathway import PathwayRegistry
from cogworx.loop.state import RunStatus
from cogworx.model.base import ModelResponse
from cogworx.model.registry import ModelRegistry
from cogworx.runtime.engine import Engine
from cogworx.testing.doubles import (
    InMemoryEntityKG,
    InMemoryGraphStore,
    InMemoryJournal,
    InMemoryLatentStore,
)

from tests.support.stage_context_fake import FakeModel
from wombat.calendar.models import CalendarEvent
from wombat.compose.brief_template import render_brief_lines
from wombat.config import WombatConfig
from wombat.domain.brief_decision_artifact import BriefDecisionArtifact
from wombat.gate.decay import LedgerReset
from wombat.gate.pending_set import InMemoryPendingJournal, PendingSet
from wombat.gate.pipeline import Gate
from wombat.integrations.gmail.models import GmailMessageItem
from wombat.integrations.gmail.triage import load_triage_rules
from wombat.pathways.brief_pathway import (
    BRIEF_PATHWAY_ID,
    brief_trigger_artifact,
    build_brief_pathway,
)
from wombat.stages.brief_compose_stage import BriefComposeStage
from wombat.stages.brief_deliver_stage import BriefDeliverStage
from wombat.stages.brief_force_flush_stage import BriefForceFlushStage
from wombat.stages.brief_gather_stage import BriefGatherStage
from wombat.user_model.user_model import UserModel

_TZ = ZoneInfo("America/Chicago")
_FIXED_NOW = datetime(2026, 7, 3, 8, 0, tzinfo=UTC)
# Guarantees every gathered item clears is_surfacing_worthy (scored.urgency > threshold, and
# urgency is always clamped into [0,1]) — this module tests ASSEMBLY, not gate scoring.
_URGENCY_THRESHOLD = -1.0


class _NoOpRollover:
    """A ``DayRolloverProtocol`` double that never fires — mirrors the drain e2e's own
    ``_NoOpRollover`` (this module proves the brief assembly, not decay/rollover)."""

    def check(self) -> LedgerReset | None:
        return None


class _UntouchedCeiling:
    """A ``CeilingProtocol`` double that raises if ever touched — ``Gate.select_items`` (Q-30)
    is documented to never read/record the ceiling, so this stands in as a runnable proof."""

    def allow(self, event_class: object) -> bool:  # pragma: no cover - must never be reached
        raise AssertionError("select_items must never read the ceiling")

    def record(self, event_class: object) -> None:  # pragma: no cover - must never be reached
        raise AssertionError("select_items must never record the ceiling")


class _UntouchedFlushLatch:
    """A ``FlushLatchProtocol`` double that raises if ever touched — ``Gate.select_items``
    (TK-287 AC3) must never read/record the flush latch either."""

    def allow(self) -> bool:  # pragma: no cover - must never be reached
        raise AssertionError("select_items must never read the flush latch")

    def record(self) -> None:  # pragma: no cover - must never be reached
        raise AssertionError("select_items must never record the flush latch")

    def note_denied(self) -> None:  # pragma: no cover - must never be reached
        raise AssertionError("select_items must never note-deny the flush latch")


def _config() -> WombatConfig:
    return WombatConfig(deepseek_api_key="sk-test", deepseek_base_url="https://api.deepseek.com")


def _real_gate() -> Gate:
    return Gate(
        user_model=UserModel(entity_kg=InMemoryEntityKG(), user_id="brief-e2e-user"),
        pending_set=PendingSet(journal=InMemoryPendingJournal(), max_pending=100),
        ceiling=_UntouchedCeiling(),
        urgency_threshold=_URGENCY_THRESHOLD,
        load_flush_threshold=10.0,
        flush_min_age_seconds=300.0,
        decay_ttl_seconds=float("inf"),
        day_rollover=_NoOpRollover(),
        clock=lambda: _FIXED_NOW.timestamp(),
        flush_latch=_UntouchedFlushLatch(),
    )


def _build_stack(
    *,
    fetch_calendar: object,
    fetch_gmail: object,
    model_factory: object,
    sink_path: Path,
) -> tuple[Engine, InMemoryJournal]:
    """Assemble the REAL wombat.brief pathway over a REAL Gate/UserModel, entirely in-memory.

    Mirrors ``tests/integration/test_drain_pathway_e2e.py``'s own construction — a REAL cog-worx
    ``Engine`` over a fresh ``PathwayRegistry``/``InMemoryJournal``, a FAKE model registered via
    ``ModelRegistry.register_factory`` (no network).
    """
    gate = _real_gate()
    gather = BriefGatherStage(
        fetch_calendar=fetch_calendar,  # type: ignore[arg-type]
        fetch_gmail=fetch_gmail,  # type: ignore[arg-type]
        triage_rules=load_triage_rules(),
        clock=lambda: _FIXED_NOW,
    )
    force_flush = BriefForceFlushStage(select_items=gate.select_items, tz=_TZ)
    compose = BriefComposeStage(config=_config(), tz=_TZ)
    deliver = BriefDeliverStage(sink_path=sink_path, tz=_TZ, voice_enabled=False)

    graph = build_brief_pathway(gather, force_flush, compose, deliver)
    journal = InMemoryJournal()
    pathways = PathwayRegistry()
    pathways.register(BRIEF_PATHWAY_ID, graph)

    models = ModelRegistry()
    models.register_factory("deepseek", model_factory)  # type: ignore[arg-type]

    engine = Engine(
        models=models,
        journal=journal,
        graph_store=InMemoryGraphStore(),
        latent=InMemoryLatentStore(),
        pathways=pathways,
        model_profile="deepseek",
        clock=lambda: _FIXED_NOW,
    )
    return engine, journal


def _one_event(event_id: str = "evt-1", title: str = "Dentist") -> CalendarEvent:
    start = _FIXED_NOW.astimezone(_TZ) + timedelta(days=1, hours=1)
    end = start + timedelta(hours=1)
    return CalendarEvent(event_id=event_id, title=title, start=start, end=end, all_day=False)


def _one_message(message_id: str = "m1") -> GmailMessageItem:
    return GmailMessageItem(
        message_id=message_id,
        subject="Invoice due",
        sender="billing@example.com",
        received_at=_FIXED_NOW - timedelta(hours=1),
        body_text="irrelevant — brief_gather never reads this field",
    )


# --- AC1: one fire delivers exactly one run-marked brief with the event + email -----------------


async def test_ac1_one_fire_delivers_one_brief_with_event_and_email(tmp_path: Path) -> None:
    """The mouth is UNREACHABLE (Q-77 degrade, mirrors AC3) so the delivered text is exactly the
    deterministic ``render_brief_lines`` rendering of the sealed decision — a stable, runnable
    place to assert the gathered event + email actually reached the sink, independent of what a
    real/fake model would have chosen to phrase."""
    sink = tmp_path / "brief.txt"
    event = _one_event()
    message = _one_message()
    unreachable_model = lambda guard: FakeModel(  # noqa: E731
        raises=ConnectionError("simulated DeepSeek outage")
    )
    engine, _journal = _build_stack(
        fetch_calendar=lambda: [event],
        fetch_gmail=lambda: [message],
        model_factory=unreachable_model,
        sink_path=sink,
    )

    run_id = "run-ac1"
    final = await engine.run(
        run_id=run_id,
        session_id=run_id,
        pathway_id=BRIEF_PATHWAY_ID,
        initial=brief_trigger_artifact(_FIXED_NOW),
    )

    assert final.status is RunStatus.COMPLETED
    text = sink.read_text(encoding="utf-8")
    assert text.count(f"[run={run_id}]") == 1  # exactly ONE header — one brief per invocation
    assert event.title in text
    assert message.subject in text
    assert message.sender in text


async def test_ac1_conflict_variant_includes_a_conflict_note(tmp_path: Path) -> None:
    """Same mouth-unreachable degrade as above — the delivered text is exactly ``render_brief_
    lines``, a stable place to assert the derived conflict note actually reached the sink."""
    sink = tmp_path / "brief.txt"
    day_start = (_FIXED_NOW.astimezone(_TZ) + timedelta(days=1)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    event_a = CalendarEvent(
        event_id="a", title="Standup", start=day_start, end=day_start + timedelta(hours=1),
        all_day=False,
    )
    event_b = CalendarEvent(
        event_id="b",
        title="1:1 with manager",
        start=day_start + timedelta(minutes=30),
        end=day_start + timedelta(hours=1, minutes=30),
        all_day=False,
    )
    unreachable_model = lambda guard: FakeModel(  # noqa: E731
        raises=ConnectionError("simulated DeepSeek outage")
    )
    engine, _journal = _build_stack(
        fetch_calendar=lambda: [event_a, event_b],
        fetch_gmail=lambda: [],
        model_factory=unreachable_model,
        sink_path=sink,
    )

    run_id = "run-conflict"
    final = await engine.run(
        run_id=run_id,
        session_id=run_id,
        pathway_id=BRIEF_PATHWAY_ID,
        initial=brief_trigger_artifact(_FIXED_NOW),
    )

    assert final.status is RunStatus.COMPLETED
    text = sink.read_text(encoding="utf-8")
    assert text.count(f"[run={run_id}]") == 1
    assert "Conflicts:" in text
    assert "conflicts with" in text


# --- AC2 (CON-1): the compose prompt is EXACTLY render_brief_lines of the sealed decision --------


async def test_ac2_compose_prompt_matches_render_brief_lines_of_the_sealed_decision(
    tmp_path: Path,
) -> None:
    sink = tmp_path / "brief.txt"
    event = _one_event()
    message = _one_message()
    model = FakeModel(
        response=ModelResponse(text="Good morning.", model_id="fake", finish_reason="stop")
    )
    success_model = lambda guard: model  # noqa: E731
    engine, _journal = _build_stack(
        fetch_calendar=lambda: [event],
        fetch_gmail=lambda: [message],
        model_factory=success_model,
        sink_path=sink,
    )

    final = await engine.run(
        run_id="run-ac2",
        session_id="run-ac2",
        pathway_id=BRIEF_PATHWAY_ID,
        initial=brief_trigger_artifact(_FIXED_NOW),
    )

    assert final.status is RunStatus.COMPLETED
    force_flush_step = next(s for s in final.steps if s.stage_name == "brief_force_flush")
    assert force_flush_step.result.output is not None
    sealed = BriefDecisionArtifact.from_payload(force_flush_step.result.output.data)
    expected_body = render_brief_lines(sealed, tz=_TZ)

    assert len(model.calls) == 1
    captured_prompt = model.calls[0]
    user_message = next(m for m in captured_prompt if m.role == "user")
    assert user_message.content == expected_body


# --- AC3 (Q-77): a raising model degrades to render_brief_lines EXACTLY -------------------------


async def test_ac3_raising_model_degrades_delivered_text_to_render_brief_lines_exactly(
    tmp_path: Path,
) -> None:
    sink = tmp_path / "brief.txt"
    event = _one_event()
    message = _one_message()
    raising_model = lambda guard: FakeModel(raises=ConnectionError("simulated DeepSeek outage"))  # noqa: E731
    engine, _journal = _build_stack(
        fetch_calendar=lambda: [event],
        fetch_gmail=lambda: [message],
        model_factory=raising_model,
        sink_path=sink,
    )

    run_id = "run-ac3"
    final = await engine.run(
        run_id=run_id,
        session_id=run_id,
        pathway_id=BRIEF_PATHWAY_ID,
        initial=brief_trigger_artifact(_FIXED_NOW),
    )

    assert final.status is RunStatus.COMPLETED
    force_flush_step = next(s for s in final.steps if s.stage_name == "brief_force_flush")
    assert force_flush_step.result.output is not None
    sealed = BriefDecisionArtifact.from_payload(force_flush_step.result.output.data)
    expected_body = render_brief_lines(sealed, tz=_TZ)

    text = sink.read_text(encoding="utf-8")
    # block == f"{header}\n{text}\n\n" (BriefDeliverStage) — strip the header line and the
    # trailing blank line to recover the delivered text exactly.
    _header, delivered_and_trailer = text.split("\n", 1)
    delivered_text = delivered_and_trailer.rstrip("\n")
    assert delivered_text == expected_body


# --- Google-less degrade: both fetches raise -> an honest degraded brief, never a crash ---------


async def test_both_sources_unavailable_still_delivers_an_honest_degraded_brief(
    tmp_path: Path,
) -> None:
    sink = tmp_path / "brief.txt"

    def _raise_calendar() -> list[CalendarEvent]:
        raise ConnectionError("calendar down")

    def _raise_gmail() -> list[GmailMessageItem]:
        raise ConnectionError("gmail down")

    # Mouth unreachable too — the delivered text equals render_brief_lines, which carries the
    # honest per-source degrade lines this test asserts on.
    unreachable_model = lambda guard: FakeModel(  # noqa: E731
        raises=ConnectionError("simulated DeepSeek outage")
    )
    engine, _journal = _build_stack(
        fetch_calendar=_raise_calendar,
        fetch_gmail=_raise_gmail,
        model_factory=unreachable_model,
        sink_path=sink,
    )

    run_id = "run-degraded"
    final = await engine.run(
        run_id=run_id,
        session_id=run_id,
        pathway_id=BRIEF_PATHWAY_ID,
        initial=brief_trigger_artifact(_FIXED_NOW),
    )

    assert final.status is RunStatus.COMPLETED
    text = sink.read_text(encoding="utf-8")
    assert "Calendar is unavailable right now." in text
    assert "Gmail is unavailable right now." in text
