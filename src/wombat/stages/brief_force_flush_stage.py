"""BriefForceFlushStage — deterministic forced gate flush over the brief's OWN items (TK-99, Q-75).

Second stage of the morning-brief cluster (TK-98's ``brief_gather`` emits the ``BriefPayload``
this stage reads via ``ctx.last_output("brief_gather")``). Derives calendar conflicts, adapts
three item families (events, conflicts, gmail) into ``GateItem``s, runs the gate's threshold-free
per-item selection over them, and seals the result into an immutable ``BriefDecisionArtifact`` —
selection is complete/irrevocable before any model call. NO model call here (NG-4).

STRUCTURAL SEAM (Q-75 ruling 1): the stage constructor-injects the narrow async ``select_items``
callable (a bound ``Gate.select_items``, the Q-30 threshold-free, pending-set-PRESERVING selection
seam — ``gate/pipeline.py``) + ``tz`` (a ``ZoneInfo``, the DEC-21 zone, bound at composition) —
NEVER the whole ``Gate``. ``urgency_threshold`` is already baked into the injected callable (a
bound method closes over the constructing ``Gate``'s own threshold), so it is not a separate
constructor arg here. This makes touching ``pipeline()``/``clear()``/the pending set structurally
impossible from this stage.

CONFLICT DERIVATION FOLDED IN (ruling 2): this stage calls
``wombat.calendar.conflict.detect_conflicts(list(payload.calendar_events), tz)`` itself — there is
no separate DetectConflicts stage.

THREE-FAMILY GATEITEM ADAPTATION (ruling 3): all items get ``item_kind=ItemKind.BRIEF`` and
``created_at=payload.generated_at.timestamp()`` (derived from the JOURNALED artifact's
``generated_at``, NEVER ``ctx.clock`` — replay-stable). Artifact-local prefixed item_ids
(``brief-event-<i>`` / ``brief-conflict-<i>`` / ``brief-gmail-<i>``) are ephemeral, brief-local
identities — TK-12 canonical identity is not implicated here.

  * events (-> prep bucket): ``event.to_payload()`` fields + ``is_timed=True``,
    ``seconds_to_event=(event.start - payload.generated_at).total_seconds()``,
    ``sender_class="self"``, NO ``event_class`` key -> falls back through ``ItemKind.BRIEF`` to
    ``EventClass.MORNING_BRIEF`` (Q-41).
  * conflicts (-> conflict bucket): ``conflict_to_payload(c)`` (carries
    ``event_class="calendar_conflict"`` -> ``EventClass.CALENDAR_CONFLICT``) +
    ``sender_class="self"``.
  * gmail (-> recap bucket): ``item.to_payload()`` + a deterministic ``PriorityBand``->
    ``sender_class`` bridge (HIGH -> known_human, NORMAL -> automated).

All three families feed ONE ``select_items`` call; the selected ``ScoredItem``s are then bucketed
back into recap/conflict/prep by their artifact-local item_id prefix family.

Touches ONLY ``ctx.last_output("brief_gather")`` and ``ctx.clock`` (provenance timestamp only,
mirroring every other stage in this cluster) — NEVER ``ctx.journal``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from zoneinfo import ZoneInfo

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import StageResult, Transition
from cogworx.loop.stage import StageContext

from wombat.calendar.conflict import DailyConflict, conflict_to_payload, detect_conflicts
from wombat.calendar.models import CalendarEvent
from wombat.domain.brief_decision_artifact import BriefBucket, BriefDecisionArtifact
from wombat.domain.brief_payload import BriefPayload, GmailBriefItem
from wombat.gate.models import GateItem, ItemKind, ScoredItem
from wombat.integrations.gmail.triage import PriorityBand
from wombat.stages.artifacts import BRIEF_DECISION

# The Q-75 ruling-1 seam: the narrow, already-threshold-bound selection callable this stage
# injects instead of the whole Gate.
SelectItems = Callable[[Iterable[GateItem]], Awaitable[list[ScoredItem]]]

# Deterministic PriorityBand -> sender_class bridge (ruling 3). Any band not listed here maps to
# the quiet "automated" default, matching the scoring module's own quiet-by-default posture.
_PRIORITY_BAND_SENDER_CLASS: dict[PriorityBand, str] = {
    PriorityBand.HIGH: "known_human",
    PriorityBand.NORMAL: "automated",
}


class BriefForceFlushStage:
    """Derives conflicts, force-selects the brief's own items, seals a ``BriefDecisionArtifact``."""

    name: str = "brief_force_flush"
    transitions: tuple[str, ...] = ("brief_compose",)

    def __init__(self, *, select_items: SelectItems, tz: ZoneInfo) -> None:
        self._select_items = select_items
        self._tz = tz

    async def run(self, ctx: StageContext) -> StageResult:
        art = await ctx.last_output("brief_gather")
        if art is None:
            msg = "brief_force_flush: no brief_gather output available yet"
            raise RuntimeError(msg)
        payload = BriefPayload.from_payload(art.data)

        conflicts = detect_conflicts(list(payload.calendar_events), self._tz)
        created_at = payload.generated_at.timestamp()

        gate_items: list[GateItem] = []
        event_by_id: dict[str, CalendarEvent] = {}
        conflict_by_id: dict[str, DailyConflict] = {}
        gmail_by_id: dict[str, GmailBriefItem] = {}

        for i, event in enumerate(payload.calendar_events):
            item_id = f"brief-event-{i}"
            event_by_id[item_id] = event
            item_payload = event.to_payload()
            item_payload["is_timed"] = True
            item_payload["seconds_to_event"] = (
                event.start - payload.generated_at
            ).total_seconds()
            item_payload["sender_class"] = "self"
            gate_items.append(
                GateItem(
                    item_id=item_id,
                    item_kind=ItemKind.BRIEF,
                    created_at=created_at,
                    payload=item_payload,
                )
            )

        for i, conflict in enumerate(conflicts):
            item_id = f"brief-conflict-{i}"
            conflict_by_id[item_id] = conflict
            item_payload = conflict_to_payload(conflict)
            item_payload["sender_class"] = "self"
            gate_items.append(
                GateItem(
                    item_id=item_id,
                    item_kind=ItemKind.BRIEF,
                    created_at=created_at,
                    payload=item_payload,
                )
            )

        for i, gmail_item in enumerate(payload.gmail_items):
            item_id = f"brief-gmail-{i}"
            gmail_by_id[item_id] = gmail_item
            item_payload = gmail_item.to_payload()
            item_payload["sender_class"] = _PRIORITY_BAND_SENDER_CLASS.get(
                gmail_item.priority_band, "automated"
            )
            gate_items.append(
                GateItem(
                    item_id=item_id,
                    item_kind=ItemKind.BRIEF,
                    created_at=created_at,
                    payload=item_payload,
                )
            )

        selected = await self._select_items(gate_items)

        prep = tuple(event_by_id[s.item_id] for s in selected if s.item_id in event_by_id)
        conflict_bucket = tuple(
            conflict_to_payload(conflict_by_id[s.item_id])
            for s in selected
            if s.item_id in conflict_by_id
        )
        recap = tuple(gmail_by_id[s.item_id] for s in selected if s.item_id in gmail_by_id)

        artifact = BriefDecisionArtifact(
            bucket=BriefBucket(recap=recap, conflict=conflict_bucket, prep=prep),
            calendar_unavailable=payload.calendar_unavailable,
            gmail_unavailable=payload.gmail_unavailable,
        )

        return Transition(
            to="brief_compose",
            output=Artifact(
                kind=BRIEF_DECISION,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data=artifact.to_payload(),
            ),
        )


__all__ = ["BriefForceFlushStage", "SelectItems"]
