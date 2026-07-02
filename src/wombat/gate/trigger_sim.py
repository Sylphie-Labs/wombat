"""TK-26 SPIKE (RISK-2) — trigger-arm + daily-ceiling sensitivity sweep.

Pure, model-free (NG-4) replay harness over a representative day of scored items (the TK-22
fixture). It applies the two trigger arms against parameterized thresholds and a hard per-class
daily ceiling, and reports surfacings/day so thresholds can be tuned before hardening (RISK-2).

Two DISTINCT surfacing classes are counted (per the TK-26 AC / DEC-16):

* immediate-voice  — fired when an item's ``urgency`` >= ``urgency_threshold``. Counted
  per sender CLASS and capped by the per-class daily ceiling.
* load-flush       — fired AT MOST ONCE/day when the day's cumulative load crosses
  ``load_flush_threshold``. This is the LOAD-triggered consolidated flush, a separate class
  from the once-daily morning-brief forced flush (TK-99), which this harness does not model.

Nothing here calls a model, does I/O, or mutates global state; ``sweep`` is a pure function of
its inputs.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from wombat.gate.models import GateItem, ItemKind
from wombat.gate.scoring import cognitive_load, urgency
from wombat.rating.params import RatingParams


@dataclass(frozen=True, slots=True)
class ScoredRow:
    """A pre-scored day item: its sender class plus urgency/load. Pure data."""

    sender_class: str
    urgency: float
    load: float


@dataclass(frozen=True, slots=True)
class SweepResult:
    """One row of the sensitivity sweep for a (urgency_threshold, load_threshold) pair."""

    urgency_threshold: float
    load_threshold: float
    immediate_voice_count: int  # total surfacings after the per-class ceiling is applied
    flush_count: int  # 0 or 1 — the load-triggered consolidated flush
    ceiling_hits: int  # immediate-voice candidates suppressed by the per-class ceiling


def score_day(
    items: list[GateItem], params: RatingParams
) -> list[ScoredRow]:
    """Score a day of GateItems into ScoredRows. Pure (delegates to the pure scorers)."""
    rows: list[ScoredRow] = []
    for it in items:
        rows.append(
            ScoredRow(
                sender_class=str(it.payload.get("sender_class", "automated")),
                urgency=urgency(it, params),
                load=cognitive_load(it, params),
            )
        )
    return rows


def evaluate_day(
    rows: list[ScoredRow],
    urgency_threshold: float,
    load_flush_threshold: float,
    per_class_ceiling: int,
) -> SweepResult:
    """Evaluate one threshold combo over a day. Pure.

    Immediate-voice: an item surfaces when urgency >= threshold, but no more than
    ``per_class_ceiling`` items per sender class surface in the day; the rest are ceiling hits.
    Load-flush: fires once if total day load >= ``load_flush_threshold``.
    """
    surfaced_per_class: Counter[str] = Counter()
    ceiling_hits = 0
    immediate = 0
    for row in rows:
        if row.urgency >= urgency_threshold:
            if surfaced_per_class[row.sender_class] < per_class_ceiling:
                surfaced_per_class[row.sender_class] += 1
                immediate += 1
            else:
                ceiling_hits += 1

    total_load = sum(r.load for r in rows)
    flush = 1 if total_load >= load_flush_threshold else 0

    return SweepResult(
        urgency_threshold=urgency_threshold,
        load_threshold=load_flush_threshold,
        immediate_voice_count=immediate,
        flush_count=flush,
        ceiling_hits=ceiling_hits,
    )


def sweep(
    rows: list[ScoredRow],
    urgency_thresholds: tuple[float, ...],
    load_thresholds: tuple[float, ...],
    per_class_ceiling: int,
) -> list[SweepResult]:
    """Full Cartesian sweep over the threshold grid. Pure; deterministic order."""
    results: list[SweepResult] = []
    for u in urgency_thresholds:
        for load_t in load_thresholds:
            results.append(evaluate_day(rows, u, load_t, per_class_ceiling))
    return results


def to_csv(results: list[SweepResult]) -> str:
    """Render sweep results as CSV text (one row per combo). Pure."""
    header = (
        "urgency_threshold,load_threshold,immediate_voice_count,flush_count,ceiling_hits"
    )
    lines = [header]
    for r in results:
        lines.append(
            f"{r.urgency_threshold},{r.load_threshold},"
            f"{r.immediate_voice_count},{r.flush_count},{r.ceiling_hits}"
        )
    return "\n".join(lines) + "\n"


def items_from_fixture(rows: list[dict[str, Any]]) -> list[GateItem]:
    """Build GateItems from raw fixture item dicts. Pure helper for the harness/test."""
    out: list[GateItem] = []
    for row in rows:
        features: dict[str, Any] = dict(row["features"])
        out.append(
            GateItem(
                item_id=str(row["item_id"]),
                item_kind=ItemKind.GENERIC,
                created_at=0.0,
                payload=features,
            )
        )
    return out
