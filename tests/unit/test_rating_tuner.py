"""Tests for RatingTuner — the nightly bounded rating-parameter tuner (TK-49, EP-14, Q-91).

  AC1 (corpus read + provenance write): seven nights of real OUTCOME_LOAD_BEARING/OUTCOME_IGNORED
      claims (written through a real OutcomeLabeler/ObservationWriter) -> tune() writes an updated
      urgency_base claim readable back via claims_about + params_from_claim_payload, whose
      SourceDeclaration ref names this tuner and tonight's date. ``test_ac1_...``.
  AC2/AC3 (ceiling/floor): a corpus whose unclamped delta would cross the band -> the written
      value is EXACTLY clamp_ceiling (resp. clamp_floor). ``test_ac2_...``/``test_ac3_...``.
  AC4 (no inline constants + delta-bound-before-band-clamp): the module source carries none of the
      TK-48 joint-block literals as constants; a single night's delta is bounded by delta_bound
      BEFORE the floor/ceiling clamp. ``test_ac4_...``.
  AC5 (no-corpus no-op): zero in-window terminal claims -> zero writer calls, existing params
      claim unchanged. ``test_ac5_...``.

``asyncio_mode = "auto"`` is configured in pyproject.toml (pytest-asyncio), so async test
functions run directly — no manual ``asyncio.run()`` driving needed.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from cogworx.knowledge.scopes import ScopeRegistry
from cogworx.testing.doubles import InMemoryEntityKG

from wombat.params import load_operating_params
from wombat.rating import rating_tuner as rating_tuner_module
from wombat.rating.params import (
    RATING_CLAIM_PREDICATE,
    EventClass,
    RatingParams,
    default_params_for,
    params_from_claim_payload,
)
from wombat.rating.rating_tuner import RECALL_WINDOW_NIGHTS, RatingTuner
from wombat.user_model.observation_writer import ObservationWriter
from wombat.user_model.outcome_inference import Outcome, OutcomeSignal
from wombat.user_model.outcome_labeler import OutcomeLabeler

_NOW = datetime(2026, 7, 9, 3, 0, 0, tzinfo=UTC)
_SCOPE = "user:alice"


async def _seed_terminal(
    labeler: OutcomeLabeler,
    *,
    event_class: EventClass,
    outcome: Outcome,
    item_ref: str,
    resolved_at: datetime,
) -> None:
    """Write one genuine terminal OUTCOME_* claim through the real stamp/label round-trip."""
    pending_id = await labeler.stamp_pending(
        item_ref=item_ref,
        event_class=event_class,
        disposition="surfaced",
        resolved_at=resolved_at,
    )
    signal = OutcomeSignal(
        item_ref=item_ref, outcome=outcome, source="inferred", rule_name="test-rule"
    )
    await labeler.label_terminal(
        pending_claim_id=pending_id,
        event_class=event_class,
        signal=signal,
        resolved_at=resolved_at,
    )


async def _current_rating_claim(kg: InMemoryEntityKG, event_class: EventClass) -> RatingParams:
    """The newest ACTIVE ``rating_params`` claim for ``event_class`` (mirrors the Q-41 read
    discipline: ``record_rating_params`` never invalidates a prior write, so more than one
    active claim can coexist — the newest one, per ``claims_about``'s newest-first contract, is
    current)."""
    scored = await kg.claims_about(event_class.value, scope=_SCOPE)
    rating_claims = [
        s
        for s in scored
        if s.claim.valid_to is None and s.claim.predicate == RATING_CLAIM_PREDICATE
    ]
    assert rating_claims, f"expected at least one active rating_params claim, got {scored}"
    return params_from_claim_payload(rating_claims[0].claim.payload)


# --- AC1: seven nights of real terminal claims -> a provenance-bearing param write --------------


async def test_ac1_seven_nights_of_outcomes_write_a_provenanced_rating_params_claim() -> None:
    kg = InMemoryEntityKG()
    writer = ObservationWriter(entity_kg=kg, scope_registry=ScopeRegistry(), user_id="alice")
    labeler = OutcomeLabeler(writer=writer)

    # Five LOAD_BEARING + two IGNORED across the last seven nights (net_signal = 3/7).
    outcomes = [Outcome.LOAD_BEARING] * 5 + [Outcome.IGNORED] * 2
    for i, outcome in enumerate(outcomes):
        await _seed_terminal(
            labeler,
            event_class=EventClass.GENERIC,
            outcome=outcome,
            item_ref=f"item-{i}",
            resolved_at=_NOW - timedelta(days=i),
        )

    op = load_operating_params()
    tuner = RatingTuner(entity_kg=kg, writer=writer, params=op, user_id="alice", clock=lambda: _NOW)

    await tuner.tune(_NOW)

    default = default_params_for(EventClass.GENERIC)
    updated = await _current_rating_claim(kg, EventClass.GENERIC)
    # raw_delta = gain(0.20) * (5-2)/7 ~= 0.0857 -> clamped to +delta_bound(0.05).
    assert updated.urgency_base == pytest.approx(default.urgency_base + op.rating_tuner.delta_bound)
    assert updated.load_base == pytest.approx(default.load_base - op.rating_tuner.delta_bound)
    assert updated.urgency_gain == default.urgency_gain  # v1: never tuned
    assert updated.load_gain == default.load_gain  # v1: never tuned

    scored = await kg.claims_about(EventClass.GENERIC.value, scope=_SCOPE)
    rating_claim = next(s.claim for s in scored if s.claim.predicate == RATING_CLAIM_PREDICATE)
    night_date = _NOW.date().isoformat()
    source_ref = rating_claim.provenance.source_ref
    assert source_ref is not None
    assert "wombat.rating_tuner:" in source_ref
    assert night_date in source_ref


async def test_recall_window_excludes_claims_older_than_seven_nights() -> None:
    kg = InMemoryEntityKG()
    writer = ObservationWriter(entity_kg=kg, scope_registry=ScopeRegistry(), user_id="alice")
    labeler = OutcomeLabeler(writer=writer)

    # Outside the RECALL_WINDOW_NIGHTS=7 window -> must not contribute to the corpus.
    await _seed_terminal(
        labeler,
        event_class=EventClass.GENERIC,
        outcome=Outcome.LOAD_BEARING,
        item_ref="stale-item",
        resolved_at=_NOW - timedelta(days=RECALL_WINDOW_NIGHTS + 5),
    )

    op = load_operating_params()
    tuner = RatingTuner(entity_kg=kg, writer=writer, params=op, user_id="alice", clock=lambda: _NOW)
    await tuner.tune(_NOW)

    scored = await kg.claims_about(EventClass.GENERIC.value, scope=_SCOPE)
    rating_claims = [s for s in scored if s.claim.predicate == RATING_CLAIM_PREDICATE]
    assert rating_claims == []  # AC5-shaped: a stale-only corpus writes nothing


# --- CR2-11/TK-185/Q-95: first-night tunes move WITH the outcome signal --------------------
#
# TK-41's per-class defaults are now reconciled into the RatingTuner's locked [clamp_floor,
# clamp_ceiling] band (Q-95 ruling: the band stands, the DEFAULTS moved). This directly pins the
# register's two exact repros (CR2-11) plus generalizes the check to every EventClass, so a
# future out-of-band default regresses loudly instead of silently snapping on the first tune.


async def test_cr2_11_first_night_tune_moves_with_the_outcome_signal_for_every_class() -> None:
    """Starting from the documented (now in-band) defaults, an all-load-bearing corpus must
    never LOWER urgency_base and an all-ignored corpus must never RAISE it, on the very first
    tune night (no prior rating_params claim), for EVERY EventClass — including the register's
    two exact repros: CALENDAR_CONFLICT + all-load-bearing, and REFLECTION + all-ignored."""
    op = load_operating_params()

    for event_class in EventClass:
        default = default_params_for(event_class)

        # All-load-bearing corpus: urgency_base must not decrease.
        kg_up = InMemoryEntityKG()
        writer_up = ObservationWriter(
            entity_kg=kg_up, scope_registry=ScopeRegistry(), user_id="alice"
        )
        labeler_up = OutcomeLabeler(writer=writer_up)
        for i in range(3):
            await _seed_terminal(
                labeler_up,
                event_class=event_class,
                outcome=Outcome.LOAD_BEARING,
                item_ref=f"lb-{i}",
                resolved_at=_NOW - timedelta(days=i),
            )
        tuner_up = RatingTuner(
            entity_kg=kg_up, writer=writer_up, params=op, user_id="alice", clock=lambda: _NOW
        )
        await tuner_up.tune(_NOW)
        updated_up = await _current_rating_claim(kg_up, event_class)
        assert updated_up.urgency_base >= default.urgency_base, (
            f"{event_class}: all-load-bearing corpus lowered urgency_base "
            f"({default.urgency_base} -> {updated_up.urgency_base})"
        )

        # All-ignored corpus: urgency_base must not increase.
        kg_down = InMemoryEntityKG()
        writer_down = ObservationWriter(
            entity_kg=kg_down, scope_registry=ScopeRegistry(), user_id="alice"
        )
        labeler_down = OutcomeLabeler(writer=writer_down)
        for i in range(3):
            await _seed_terminal(
                labeler_down,
                event_class=event_class,
                outcome=Outcome.IGNORED,
                item_ref=f"ig-{i}",
                resolved_at=_NOW - timedelta(days=i),
            )
        tuner_down = RatingTuner(
            entity_kg=kg_down, writer=writer_down, params=op, user_id="alice", clock=lambda: _NOW
        )
        await tuner_down.tune(_NOW)
        updated_down = await _current_rating_claim(kg_down, event_class)
        assert updated_down.urgency_base <= default.urgency_base, (
            f"{event_class}: all-ignored corpus raised urgency_base "
            f"({default.urgency_base} -> {updated_down.urgency_base})"
        )


# --- AC2/AC3: ceiling/floor -----------------------------------------------------------------


async def test_ac2_unclamped_delta_above_ceiling_writes_exactly_clamp_ceiling() -> None:
    kg = InMemoryEntityKG()
    writer = ObservationWriter(entity_kg=kg, scope_registry=ScopeRegistry(), user_id="alice")
    labeler = OutcomeLabeler(writer=writer)
    op = load_operating_params()
    bounds = op.rating_tuner

    # Seed a current params claim close to the ceiling.
    seeded = RatingParams(
        urgency_base=bounds.clamp_ceiling - 0.02, urgency_gain=0.5, load_base=0.5, load_gain=0.5
    )
    await writer.record_rating_params(EventClass.CALENDAR_CONFLICT, seeded)

    # Max positive signal: every outcome LOAD_BEARING -> net_signal = 1.0.
    for i in range(3):
        await _seed_terminal(
            labeler,
            event_class=EventClass.CALENDAR_CONFLICT,
            outcome=Outcome.LOAD_BEARING,
            item_ref=f"item-{i}",
            resolved_at=_NOW - timedelta(days=i),
        )

    tuner = RatingTuner(entity_kg=kg, writer=writer, params=op, user_id="alice", clock=lambda: _NOW)
    await tuner.tune(_NOW)

    updated = await _current_rating_claim(kg, EventClass.CALENDAR_CONFLICT)
    # Unclamped: seeded.urgency_base(0.63) + delta_bound(0.05) = 0.68 > ceiling(0.65).
    assert updated.urgency_base == bounds.clamp_ceiling


async def test_ac3_unclamped_delta_below_floor_writes_exactly_clamp_floor() -> None:
    kg = InMemoryEntityKG()
    writer = ObservationWriter(entity_kg=kg, scope_registry=ScopeRegistry(), user_id="alice")
    labeler = OutcomeLabeler(writer=writer)
    op = load_operating_params()
    bounds = op.rating_tuner

    # Seed a current params claim close to the floor.
    seeded = RatingParams(
        urgency_base=bounds.clamp_floor + 0.02, urgency_gain=0.5, load_base=0.5, load_gain=0.5
    )
    await writer.record_rating_params(EventClass.REFLECTION, seeded)

    # Max negative signal: every outcome IGNORED -> net_signal = -1.0.
    for i in range(3):
        await _seed_terminal(
            labeler,
            event_class=EventClass.REFLECTION,
            outcome=Outcome.IGNORED,
            item_ref=f"item-{i}",
            resolved_at=_NOW - timedelta(days=i),
        )

    tuner = RatingTuner(entity_kg=kg, writer=writer, params=op, user_id="alice", clock=lambda: _NOW)
    await tuner.tune(_NOW)

    updated = await _current_rating_claim(kg, EventClass.REFLECTION)
    # Unclamped: seeded.urgency_base(0.37) - delta_bound(0.05) = 0.32 < floor(0.35).
    assert updated.urgency_base == bounds.clamp_floor


# --- AC4: no inline TK-48 literals + delta-bound-before-band-clamp -------------------------


def test_ac4_module_source_has_no_inlined_bound_literals() -> None:
    source = inspect.getsource(rating_tuner_module)
    tree = ast.parse(source)
    forbidden = {0.35, 0.65, 0.05, 0.20}
    found = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, float)
        and node.value in forbidden
    }
    assert found == set(), f"forbidden TK-48 joint-block literal(s) inlined: {found}"


async def test_ac4_single_night_delta_is_bounded_by_delta_bound_before_band_clamp() -> None:
    kg = InMemoryEntityKG()
    writer = ObservationWriter(entity_kg=kg, scope_registry=ScopeRegistry(), user_id="alice")
    labeler = OutcomeLabeler(writer=writer)
    op = load_operating_params()

    # net_signal = 1.0 -> raw_delta = gain(0.20) * 1.0 = 0.20, which MUST be clamped down to
    # delta_bound(0.05), not applied raw.
    await _seed_terminal(
        labeler,
        event_class=EventClass.GENERIC,
        outcome=Outcome.LOAD_BEARING,
        item_ref="item-0",
        resolved_at=_NOW,
    )

    tuner = RatingTuner(entity_kg=kg, writer=writer, params=op, user_id="alice", clock=lambda: _NOW)
    await tuner.tune(_NOW)

    default = default_params_for(EventClass.GENERIC)
    updated = await _current_rating_claim(kg, EventClass.GENERIC)
    change = updated.urgency_base - default.urgency_base
    assert change == pytest.approx(op.rating_tuner.delta_bound)  # +0.05, NOT +0.20
    assert change != pytest.approx(op.rating_tuner.gain)


# --- AC5: no-corpus no-op ------------------------------------------------------------------


async def test_ac5_no_in_window_corpus_means_zero_writer_calls_and_unchanged_params() -> None:
    kg = InMemoryEntityKG()
    real_writer = ObservationWriter(entity_kg=kg, scope_registry=ScopeRegistry(), user_id="alice")
    existing = RatingParams(urgency_base=0.55, urgency_gain=0.5, load_base=0.45, load_gain=0.5)
    await real_writer.record_rating_params(EventClass.DRAFT_REPLY, existing)

    spy_writer = AsyncMock(spec=ObservationWriter)
    op = load_operating_params()
    tuner = RatingTuner(
        entity_kg=kg, writer=spy_writer, params=op, user_id="alice", clock=lambda: _NOW
    )

    await tuner.tune(_NOW)

    spy_writer.record_rating_params.assert_not_called()

    unchanged = await _current_rating_claim(kg, EventClass.DRAFT_REPLY)
    assert unchanged == existing
