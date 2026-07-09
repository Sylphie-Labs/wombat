"""The deterministic gate stub — Hold vs Surface with presence conditioning (TK-6, EP-4, Q-48).

Two functions, both pure and model-free (NG-4):

* ``gate_item_from_queue_item`` maps a drained ``QueueItem`` (TK-5/Q-47) to the canonical
  ``GateItem`` (TK-21, ``gate/models.py``) — no new vocabulary, no redefinition.
* ``stub_evaluate`` is the concrete stub evaluator TK-6 ships. It is presence-first (Q-12:
  presence is a gate-level hold, never a scoring input) and only reads a stub urgency value out
  of the payload — TK-27 later swaps in the production evaluator behind the SAME
  ``(GateItem, PresenceSnapshot | None) -> GateDecision`` call shape (the replacement seam
  ``GateStage`` is built around); this module never changes when that swap happens.

``gate/decision.py`` is DROPPED from TK-6's scope (Q-48): ``GateAction``/``GateDecision``/
``ScoredItem`` already live canonically in ``gate/models.py`` (TK-21, ISS-4) and are imported
here, never redefined.
"""

from __future__ import annotations

from wombat.gate.models import GateAction, GateDecision, GateItem, ItemKind, ScoredItem
from wombat.gate.presence_hold import presence_hold
from wombat.queue import QueueItem
from wombat.sources.presence import PresenceSnapshot

# stub_urgency payload value -> score (Q-48). Any other/missing value defaults to "low" (quiet
# default) at the lookup site below, matching the vision-wide quiet-by-default bias.
_STUB_URGENCY_SCORES: dict[str, float] = {"high": 0.9, "low": 0.1}


def gate_item_from_queue_item(queue_item: QueueItem) -> GateItem:
    """Map a drained ``QueueItem`` to the canonical ``GateItem`` (Q-48 mapping).

    * ``item_id`` = ``queue_item.idempotency_key`` — the stable string identity already
      established at enqueue time (TK-2), reused rather than re-derived.
    * ``item_kind`` = ``ItemKind(payload["item_kind"])`` when the payload carries a valid
      ``ItemKind`` value; any missing/invalid value falls back to ``ItemKind.GENERIC``.
    * ``created_at`` = ``0.0`` — the stub gate does not use item time at all (no decay, no
      TTL comparison here); TK-27/TK-12 thread the real item timestamp through once decay
      lands on the production evaluator.
    * ``payload`` passes through unchanged — ``stub_evaluate`` reads ``stub_urgency`` from it.
    """
    raw_kind = queue_item.payload.get("item_kind")
    try:
        item_kind = ItemKind(raw_kind)
    except ValueError:
        item_kind = ItemKind.GENERIC

    return GateItem(
        item_id=queue_item.idempotency_key,
        item_kind=item_kind,
        created_at=0.0,
        payload=queue_item.payload,
    )


def stub_evaluate(
    gate_item: GateItem,
    presence: PresenceSnapshot | None,
    *,
    urgency_threshold: float,
    staleness_ceiling_s: float,
    confidence_floor: float,
) -> GateDecision:
    """The TK-6 stub evaluator: presence-first, then a deterministic stub score (Q-48).

    Presence is applied BEFORE scoring and is never itself a scoring input (Q-12): if
    ``presence`` is ``None`` or ``presence_hold(presence, ...)`` is ``True`` (unknown / stale /
    low-confidence / idle / away — the conservative fail-safe hardened to production in TK-11),
    this returns ``GateAction.HOLD`` immediately without touching the stub urgency value at all.
    The snapshot's own ``taken_at`` is passed as ``now`` — the snapshot is treated as fresh at
    evaluation time (the provider already applied the staleness ceiling at provision per Q-49,
    so this call site's own staleness check is inert BY DESIGN; the predicate's Layer-2 check
    still runs and would catch a provider bug); this never reads a wall clock (NG-4 /
    determinism).

    Otherwise the stub score is read straight out of the payload:
    ``payload["stub_urgency"]`` of ``"high"`` -> 0.9, ``"low"`` (or anything else / missing) ->
    0.1 (the quiet default). ``urgency > urgency_threshold`` surfaces immediately (TK-171:
    strict, aligned with the production ``trigger.is_surfacing_worthy`` predicate — an item
    exactly AT the threshold holds); otherwise it holds. ``urgency_threshold``,
    ``staleness_ceiling_s``, and ``confidence_floor`` are all injected args — composition binds
    the real ``OperatingParams`` values; no inline literal lives in this module.

    Exactly one ``ScoredItem`` is returned per call either way (a HOLD from a presence fail-safe
    still identifies which item held, with a zero score since it was never actually scored) — no
    model call anywhere.
    """
    if presence is None or presence_hold(
        presence,
        presence.taken_at,
        staleness_ceiling_s=staleness_ceiling_s,
        confidence_floor=confidence_floor,
    ):
        return GateDecision(
            action=GateAction.HOLD,
            items=(
                ScoredItem(
                    item_id=gate_item.item_id,
                    item_kind=gate_item.item_kind,
                    # Never scored — presence held before scoring ran (Q-12).
                    urgency=0.0,
                    load=0.0,
                ),
            ),
        )

    stub_urgency = _STUB_URGENCY_SCORES.get(
        str(gate_item.payload.get("stub_urgency", "low")), 0.1
    )
    action = (
        GateAction.SURFACE_IMMEDIATE if stub_urgency > urgency_threshold else GateAction.HOLD
    )
    return GateDecision(
        action=action,
        items=(
            ScoredItem(
                item_id=gate_item.item_id,
                item_kind=gate_item.item_kind,
                urgency=stub_urgency,
                load=0.0,
            ),
        ),
    )


__all__ = ["gate_item_from_queue_item", "stub_evaluate"]
