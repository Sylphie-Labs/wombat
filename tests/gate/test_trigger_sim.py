"""TK-26 SPIKE (RISK-2) — trigger-arm + per-class ceiling sensitivity sweep.

Uses the TK-22 scoring functions + the de-identified day fixture as a representative day.
Confirms there exist threshold values yielding <=3 immediate-voice surfacings/day per class and
<=1 load-triggered flush/day, and writes the sensitivity CSV + a written recommendation.

COMPUTATIONAL spike: if the targets are met on the fixture, TK-26 is proven on the fixture
(inheriting TK-22's fixture-provenance caveat — the day is de-identified-real + a few flagged
synthetic human items).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

from wombat.gate.trigger_sim import (
    items_from_fixture,
    score_day,
    sweep,
    to_csv,
)
from wombat.rating.params import RatingParams

FIXTURE = Path(__file__).parent / "fixtures" / "scoring_fixture.real.yaml"

# Identity params: base=0, gain=1 -> the hardened scorers reproduce the spike's raw scores
# EXACTLY (Q-42), so every ported call site is score-identical and the TK-26 finding is undisturbed.
_IDENTITY = RatingParams(urgency_base=0.0, urgency_gain=1.0, load_base=0.0, load_gain=1.0)

URGENCY_GRID = (0.5, 0.65, 0.75, 0.85)
LOAD_GRID = (0.7, 1.0, 1.3)
PER_CLASS_CEILING = 3

# Targets from poc.question / AC1.
MAX_IMMEDIATE_PER_CLASS = 3
MAX_FLUSH = 1


def _load_fixture() -> dict[str, Any]:
    if not FIXTURE.exists():
        builder_path = FIXTURE.parent / "_build_scoring_fixture.py"
        spec = importlib.util.spec_from_file_location("_build_scoring_fixture", builder_path)
        assert spec is not None and spec.loader is not None
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        data = builder.build()
        FIXTURE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    data_out: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data_out


def test_ac_sweep_writes_csv_with_one_row_per_combo() -> None:
    fixture = _load_fixture()
    items = items_from_fixture(fixture["items"])
    rows = score_day(items, _IDENTITY)
    results = sweep(rows, URGENCY_GRID, LOAD_GRID, PER_CLASS_CEILING)

    assert len(results) == len(URGENCY_GRID) * len(LOAD_GRID) == 12
    csv_text = to_csv(results)
    assert csv_text.splitlines()[0] == (
        "urgency_threshold,load_threshold,immediate_voice_count,flush_count,ceiling_hits"
    )
    assert len(csv_text.strip().splitlines()) == 13  # header + 12 rows

    out = FIXTURE.parent / "trigger_sensitivity.real.csv"
    out.write_text(csv_text, encoding="utf-8")


def _per_class_immediate(rows: list[Any], urgency_threshold: float) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        if r.urgency > urgency_threshold:  # TK-171: strict, matching evaluate_day
            counts[r.sender_class] = counts.get(r.sender_class, 0) + 1
    return counts


def test_ac_target_thresholds_meet_quiet_targets() -> None:
    """At urgency=0.75, load=1.0 the day stays <=3 immediate/class and <=1 flush."""
    fixture = _load_fixture()
    items = items_from_fixture(fixture["items"])
    rows = score_day(items, _IDENTITY)

    # Per-class immediate-voice count BEFORE the ceiling caps it (the ceiling is a backstop;
    # we want the natural rate already within target).
    per_class = _per_class_immediate(rows, 0.75)
    for sender_class, n in per_class.items():
        assert n <= MAX_IMMEDIATE_PER_CLASS, (
            f"class {sender_class} has {n} immediate surfacings/day at urgency=0.75 (>3)"
        )

    from wombat.gate.trigger_sim import evaluate_day

    result = evaluate_day(rows, 0.75, 1.0, PER_CLASS_CEILING)
    assert result.flush_count <= MAX_FLUSH


def test_recommendation_appended_with_two_sweep_rows() -> None:
    """AC3: write a recommendation referencing >=2 sweep rows."""
    fixture = _load_fixture()
    items = items_from_fixture(fixture["items"])
    rows = score_day(items, _IDENTITY)
    results = sweep(rows, URGENCY_GRID, LOAD_GRID, PER_CLASS_CEILING)
    by_key = {(r.urgency_threshold, r.load_threshold): r for r in results}

    chosen = by_key[(0.75, 1.0)]
    looser = by_key[(0.5, 1.0)]

    rec = [
        "TK-26 RISK-2 trigger sensitivity recommendation",
        "",
        "RECOMMENDED PRODUCTION THRESHOLDS: urgency_threshold=0.75, "
        "load_flush_threshold=1.0, per_class_daily_ceiling=3.",
        "",
        "Rationale (>=2 sweep rows):",
        f"  row (u=0.75, load=1.0): immediate_voice={chosen.immediate_voice_count}, "
        f"flush={chosen.flush_count}, ceiling_hits={chosen.ceiling_hits} "
        f"-> meets <=3/class and <=1 flush.",
        f"  row (u=0.50, load=1.0): immediate_voice={looser.immediate_voice_count}, "
        f"flush={looser.flush_count}, ceiling_hits={looser.ceiling_hits} "
        f"-> a looser urgency bar admits more immediate surfacings, confirming 0.75 is the "
        f"quieter operating point while the per-class ceiling backstops edge clusters.",
        "",
        "CAVEAT: proven on the de-identified-real + flagged-synthetic day fixture (inherits "
        "TK-22's fixture-provenance caveat). Re-run on additional real days before hardening.",
    ]
    out = FIXTURE.parent / "trigger_recommendation.real.txt"
    out.write_text("\n".join(rec), encoding="utf-8")

    assert chosen.immediate_voice_count <= 3 * 5  # ceiling caps each of <=5 classes at 3
    assert chosen.flush_count <= MAX_FLUSH
    assert looser.immediate_voice_count >= chosen.immediate_voice_count


def test_tk171_urgency_exactly_at_threshold_holds_across_prod_sim_and_stub() -> None:
    """CR-6/CR-9: the worth predicate is STRICT (``>``, not ``>=``) everywhere it's evaluated —
    production's ``is_surfacing_worthy``, this module's ``evaluate_day`` sim, and the TK-6 gate
    stub. An item whose urgency lands EXACTLY on the threshold must HOLD in all three, never
    surface."""
    from wombat.gate.gate import stub_evaluate
    from wombat.gate.models import GateAction, GateItem, ItemKind, ScoredItem
    from wombat.gate.trigger import is_surfacing_worthy
    from wombat.gate.trigger_sim import ScoredRow, evaluate_day
    from wombat.sources.presence import PresenceSnapshot, PresenceState

    threshold = 0.9  # matches the stub's "high" score exactly (_STUB_URGENCY_SCORES)

    # 1. production: trigger.is_surfacing_worthy
    scored = ScoredItem(item_id="a", item_kind=ItemKind.GENERIC, urgency=threshold, load=0.0)
    assert is_surfacing_worthy(scored, threshold) is False

    # 2. sim: trigger_sim.evaluate_day
    row = ScoredRow(sender_class="automated", urgency=threshold, load=0.0)
    result = evaluate_day(
        [row], urgency_threshold=threshold, load_flush_threshold=999.0, per_class_ceiling=3
    )
    assert result.immediate_voice_count == 0

    # 3. stub: gate.gate.stub_evaluate
    gate_item = GateItem(
        item_id="b", item_kind=ItemKind.GENERIC, created_at=0.0, payload={"stub_urgency": "high"}
    )
    presence = PresenceSnapshot(state=PresenceState.ACTIVE, confidence=1.0, idle_ms=0, taken_at=0.0)
    decision = stub_evaluate(
        gate_item,
        presence,
        urgency_threshold=threshold,
        staleness_ceiling_s=300.0,
        confidence_floor=0.5,
    )
    assert decision.action is GateAction.HOLD
