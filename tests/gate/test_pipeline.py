"""TK-21 — gate data model & pipeline skeleton acceptance criteria."""

from __future__ import annotations

import pathlib

from wombat.gate.models import GateAction, GateDecision, GateItem, ItemKind
from wombat.gate.pipeline import Gate


def _item(
    item_id: str, *, kind: ItemKind = ItemKind.GENERIC, created_at: float = 1000.0
) -> GateItem:
    return GateItem(item_id=item_id, item_kind=kind, created_at=created_at)


def _gate(*, decay_ttl_seconds: float = 60.0, now: float = 1000.0) -> Gate:
    return Gate(
        urgency=lambda it: 0.0,
        cognitive_load=lambda it: 0.0,
        decay_ttl_seconds=decay_ttl_seconds,
        clock=lambda: now,
    )


def test_gateaction_is_the_one_closed_canonical_vocabulary() -> None:
    # AC1: GateAction is a single closed Enum and item_kind is a closed Enum.
    assert {a.value for a in GateAction} == {"hold", "surface_immediate", "surface_flush"}
    assert {k.value for k in ItemKind} == {"brief", "reflection", "draft", "generic"}


def test_no_competing_decision_vocabulary_under_src() -> None:
    # AC1: no module under src/wombat defines a competing surface/hold/flush/speak-event string set.
    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "wombat"
    forbidden = ("speak-event", "text-only")
    offenders = [
        (p.name, token)
        for p in src.rglob("*.py")
        for token in forbidden
        if token in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_surfaced_artifact_carries_item_kind() -> None:
    # AC1: the surfaced artifact carries item_kind so consumers route by kind.
    gate = _gate()
    gate.accumulate([_item("a", kind=ItemKind.BRIEF)])
    assert gate.score_pending()[0].item_kind is ItemKind.BRIEF


def test_accumulate_is_idempotent_by_item_id() -> None:
    # AC2: duplicates (by item_id) are rejected idempotently.
    gate = _gate()
    gate.accumulate([_item("a"), _item("a")])
    assert len(gate.score_pending()) == 1


def test_score_pending_uses_injected_callables_no_model() -> None:
    # AC3: scores come from the injected callables (here pure lambdas) — no model call.
    gate = Gate(
        urgency=lambda it: 0.7,
        cognitive_load=lambda it: 0.3,
        decay_ttl_seconds=60.0,
        clock=lambda: 1000.0,
    )
    gate.accumulate([_item("a")])
    scored = gate.score_pending()[0]
    assert (scored.urgency, scored.load) == (0.7, 0.3)


def test_decay_removes_stale_and_emits_event() -> None:
    # AC4: an item older than decay_ttl is removed and a DecayEvent emitted.
    now = 2000.0
    gate = _gate(now=now)
    gate.accumulate([_item("old", created_at=now - 120.0), _item("fresh", created_at=now - 10.0)])
    events = gate.decay()
    assert {e.item_id for e in events} == {"old"}
    assert {s.item_id for s in gate.score_pending()} == {"fresh"}


def test_pipeline_holds_when_no_thresholds_crossed() -> None:
    # AC5: end-to-end with no thresholds crossed -> HOLD, no surfaced items.
    decision = _gate().pipeline([_item("a")])
    assert isinstance(decision, GateDecision)
    assert decision.action is GateAction.HOLD
    assert decision.items == ()
