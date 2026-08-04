"""TK-348 AC5 — default-hold proven at the REAL gate for all three closed biometric event kinds
(DEC-80(d)). Mirrors ``tests/sources/test_screenpipe_source.py::
test_ac_e_one_derived_event_scores_screen_activity_and_holds_at_the_real_gate`` exactly: a real
``QueueItem`` through a real ``WombatQueue`` (throwaway pg), drained, mapped to a ``GateItem``,
resolved to ``EventClass.BIOMETRIC`` via the SAME payload ``'event_class'`` override path, scored
by the real ``urgency()`` against the real ``load_operating_params().urgency_threshold`` — and
proven to land BELOW it (HOLD), never a model call anywhere on this path (NG-4).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from wombat.domain.item_identity import idempotency_key as derive_key
from wombat.gate.gate import gate_item_from_queue_item
from wombat.gate.models import ScoredItem
from wombat.gate.scoring import urgency
from wombat.gate.trigger import is_surfacing_worthy
from wombat.params import load_operating_params
from wombat.queue import QueueItem, WombatQueue
from wombat.queue import ensure_schema as ensure_queue_schema
from wombat.rating.params import EventClass, default_params_for
from wombat.user_model.user_model import resolve_event_class_for_item

_BASE = datetime(2026, 7, 31, 8, 0, tzinfo=UTC)

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping TK-348's pg-armed real-gate default-hold "
        "tests. Start a throwaway Postgres with:\n"
        "  docker run --rm -d -p 5440:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5440/postgres"
    ),
)


def _assert_holds_at_the_real_gate(payload: dict[str, object], event_key: str) -> None:
    assert _DSN is not None
    import psycopg

    with psycopg.connect(_DSN) as conn:
        ensure_queue_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE wombat_queue")
        conn.commit()

    queue_item = QueueItem(
        idempotency_key=derive_key("biometric_events", event_key), payload=payload
    )

    queue = WombatQueue(_DSN, max_size=1000)
    try:
        queue.enqueue(queue_item)
        drained = queue.drain()
        assert len(drained) == 1

        gate_item = gate_item_from_queue_item(drained[0])
        # Zero model calls anywhere below — this is the SAME deterministic, model-free scoring
        # path TK-321/TK-322 already proved (NG-4).
        event_class = resolve_event_class_for_item(gate_item)
        assert event_class is EventClass.BIOMETRIC

        params = default_params_for(event_class)
        scored_urgency = urgency(gate_item, params)
        scored = ScoredItem(
            item_id=gate_item.item_id,
            item_kind=gate_item.item_kind,
            urgency=scored_urgency,
            load=0.0,
        )
        urgency_threshold = load_operating_params().urgency_threshold

        assert not is_surfacing_worthy(scored, urgency_threshold)  # HOLD
    finally:
        queue.close()


@_requires_pg
def test_workout_ended_holds_at_the_real_gate() -> None:
    started_at = _BASE + timedelta(hours=1)
    payload = {
        "event_class": "biometric",
        "kind": "workout_ended",
        "activity": "running",
        "duration_seconds": 1800,
        "active_energy_kcal": 250.0,
    }
    event_key = f"workout_ended:{started_at.isoformat()}"
    _assert_holds_at_the_real_gate(payload, event_key)


@_requires_pg
def test_resting_hr_out_of_band_holds_at_the_real_gate() -> None:
    day_key = (_BASE - timedelta(days=10)).date()
    payload = {
        "event_class": "biometric",
        "kind": "resting_hr_out_of_band",
        "bpm": 90,
    }
    event_key = f"resting_hr_out_of_band:{day_key.isoformat()}"
    _assert_holds_at_the_real_gate(payload, event_key)


@_requires_pg
def test_sleep_debt_crossed_holds_at_the_real_gate() -> None:
    day_key = (_BASE - timedelta(days=1)).date()
    payload = {
        "event_class": "biometric",
        "kind": "sleep_debt_crossed",
        "asleep_minutes": 200,
        "in_bed_minutes": 230,
        "awakenings": 1,
    }
    event_key = f"sleep_debt_crossed:{day_key.isoformat()}"
    _assert_holds_at_the_real_gate(payload, event_key)
