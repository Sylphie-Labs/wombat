"""PatternDetectorStage — the nightly pattern-detection queue-entry pass (TK-113, EP-22, Q-99b/f/g).

Keyword-injected collaborators only (mirrors ``DreamBehaviorLogStage``/``WriteWindowSummariesStage``
precedent): ``entity_kg`` is the RAW cog-worx ``EntityKG`` Protocol, read the SAME way
``DreamOutcomeStage`` does (no ``ScopedKG``/write token minted here — this stage never writes to
the entity KG); ``kb`` is the loaded ``Sequence[wombat.kb.schema.KBEntry]`` (TK-115's
``load_psychology_kb``, loaded ONCE at boot and injected — this stage never loads the KB itself);
``enqueue`` is the narrow ``Callable[[QueueItem], EnqueueResult]`` (TK-99 precedent) bound to the
ONE shared ``WombatQueue.enqueue`` (ASMP-2 custody — never a second queue/connection); ``user_id``
forms the ``user:<user_id>`` scope every read is restricted to; ``tz`` is the SAME configured
``ZoneInfo`` bootstrap threads everywhere (DEC-21).

READ (Q-99b/f, mirrors ``DreamOutcomeStage``'s own idiom): a single point-read of
``claims_about(f"productivity_window:{wombat_today(ctx.clock(), tz).isoformat()}", limit=...,
scope=...)``. Only the ACTIVE (``valid_to is None``) claim whose predicate is
``ClaimPredicate.PRODUCTIVITY_WINDOW`` counts, taking the NEWEST-FIRST first match (``claims_about``
already returns newest-first) — ``WriteWindowSummariesStage`` writes at most one such claim per
night, so "first match" and "the only match" coincide. The claim's ``payload`` is the
double-encoded ``ObservationWriter`` envelope: ``json.loads(claim.payload)["value"]`` parsed again
yields the JSON-native ``WindowSummary`` dict list (``window_summary_to_dict``'s own wire shape).

No claim tonight is a QUIET CON-3 default (mirrors ``pattern_warrants_nudge``'s own "absent metric
is not satisfied" posture) — no log, ``metrics`` stays ``None``, zero enqueues. A ``claims_about``
read failure or a malformed/un-parseable claim payload is DIFFERENT: caught, logged LOUD, counted
as an error, and ALSO leaves ``metrics`` ``None`` (zero enqueues) — but the stage still transitions
onward (never-block-the-terminal parity with every other dream stage).

METRICS (Q-99b, the CLOSED vocabulary — nothing else): ``switch_rate`` = the max ``switch_rate``
across tonight's ``WindowSummary`` list (``0.0`` if the list is empty); ``window_count`` = its
length; ``event_count`` = the sum of every summary's ``event_count``.

MATCH (Q-99h): iterate ``kb`` IN FILE ORDER, calling ``pattern_warrants_nudge(metrics, [entry])``
(TK-116's pure, model-free gate-conditioning function; its own bool signature is the ruled seam —
never a ``pattern_id``-returning variant). The FIRST entry whose condition matches wins — ONE
candidate ``pattern_id``, never more. No match (or an empty ``kb``, e.g. a boot-time KB load
failure that fell back to ``[]`` per TK-115 AC4) is silent: zero enqueues, no log.

ENQUEUE (Q-99f): a matched pattern builds exactly one ``QueueItem`` — ``idempotency_key =
idempotency_key("wombat.reflection", <wombat-date-iso>)`` (the canonical TK-12 derivation, date-
keyed so a same-night re-fire structurally collapses to ``EnqueueResult.ALREADY_QUEUED``, never a
second queue row for the same night) and ``payload`` carrying ONLY ``item_kind``, ``event_class``,
``kind``, ``pattern_id``, ``window_ref``, ``date`` — no motive field, no clinical label, no "why"
key (AC4). ``ItemKind.REFLECTION``/``EventClass.REFLECTION`` already exist in the closed
vocabularies (``gate/models.py``/``rating/params.py``) — no vocabulary bump here.
``EnqueueResult.ALREADY_QUEUED`` is logged at DEBUG (not an error — an expected, idempotent no-op).
``QueueFullError`` is caught, logged LOUD, counted as an error — the stage still transitions.

GATE PARITY (AC3): this stage has NO gate/pending-set/journal collaborator anywhere — its only
write path is the injected ``enqueue`` callable, so the enqueued item is judged by the standard
gate exactly like any other queued item, next morning's drain. NEVER touches ``ctx.journal`` and
makes NO model call (mirrors every other dream stage's off-path posture).

RESIDUAL (recorded at Q-99, not engineered around): if the night's item was already drained AND
acked before a crash-refire, a re-enqueue on a later retry of this stage succeeds (a fresh row, not
an ``ALREADY_QUEUED`` no-op) — it converges in the pending set via TK-181's idempotent re-add of
the same item_id and stays bounded by the per-class daily ceiling. This is a recorded, accepted
residual, not a bug this stage engineers around.

``name`` is ``dream_pattern``, inserted BETWEEN ``dream_window`` and the ``dream_run`` terminal in
the ``wombat.dream`` graph (``build_dream_pathway``, ``pathways/dream_pathway.py``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from typing import Any
from zoneinfo import ZoneInfo

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import StageResult, Transition
from cogworx.loop.stage import StageContext
from cogworx.substrate.entity_kg import EntityKG

from wombat.domain.daily_ledger import wombat_today
from wombat.domain.item_identity import idempotency_key
from wombat.kb.gate_conditioning import pattern_warrants_nudge
from wombat.kb.schema import KBEntry
from wombat.queue import EnqueueResult, QueueFullError, QueueItem
from wombat.user_model.claims import ClaimPredicate

logger = logging.getLogger(__name__)

# PatternDetectorStage's committed output kind (TK-113) — a contentless, system-provenance count
# artifact mirroring every other DREAM_*_REPORT_KIND idiom: no pattern/claim payloads ride this
# artifact, only counts — the durable record is the queue row this stage enqueued (or didn't).
DREAM_PATTERN_REPORT_KIND = "wombat.dream_pattern_report"

# Q-90 v1 shape (mirrors dream_pathway.py's own _CLAIMS_LIMIT): a generous per-query ceiling —
# wombat is single-user, nightly-cadence, and WriteWindowSummariesStage writes AT MOST one
# productivity_window claim per night, nowhere near this ceiling.
_CLAIMS_LIMIT = 500

# The source_id half of the canonical idempotency_key this stage's QueueItem is keyed on — a
# date-keyed key structurally caps this stage at one QueueItem per wombat-night (Q-99f).
_REFLECTION_SOURCE_ID = "wombat.reflection"


class PatternDetectorStage:
    """The nightly pattern-detect + gated queue-entry pass (TK-113, EP-22, Q-99b/f/g).

    See module docstring for the full read/metrics/match/enqueue contract.
    """

    name: str = "dream_pattern"
    transitions: tuple[str, ...] = ("dream_run",)

    def __init__(
        self,
        *,
        entity_kg: EntityKG,
        kb: Sequence[KBEntry],
        enqueue: Callable[[QueueItem], EnqueueResult],
        user_id: str,
        tz: ZoneInfo,
    ) -> None:
        self._entity_kg = entity_kg
        self._kb = kb
        self._enqueue = enqueue
        self._user_id = user_id
        self._scope = f"user:{user_id}"
        self._tz = tz

    async def run(self, ctx: StageContext) -> StageResult:
        now = ctx.clock()
        date_iso = wombat_today(now, self._tz).isoformat()
        window_ref = f"productivity_window:{date_iso}"

        errors = 0
        metrics: dict[str, float] | None = None

        try:
            scored_claims = await self._entity_kg.claims_about(
                window_ref, limit=_CLAIMS_LIMIT, scope=self._scope
            )
            summaries: list[Any] | None = None
            for scored_claim in scored_claims:
                claim = scored_claim.claim
                if claim.valid_to is not None:
                    continue  # not ACTIVE — already invalidated
                if claim.predicate != ClaimPredicate.PRODUCTIVITY_WINDOW.value:
                    continue
                envelope = json.loads(claim.payload)
                summaries = json.loads(envelope["value"])
                break  # newest-first — the first ACTIVE match is tonight's summary list

            if summaries:
                metrics = {
                    "switch_rate": max(float(s["switch_rate"]) for s in summaries),
                    "window_count": float(len(summaries)),
                    "event_count": float(sum(int(s["event_count"]) for s in summaries)),
                }
        except Exception:
            logger.error(
                "dream_pattern: claims_about read or claim payload parse failed "
                "(window_ref=%r); skipping tonight's pattern pass",
                window_ref,
                exc_info=True,
            )
            errors += 1
            metrics = None

        pattern_id: str | None = None
        if metrics is not None:
            for entry in self._kb:
                if pattern_warrants_nudge(metrics, [entry]):
                    pattern_id = entry.pattern_id
                    break

        enqueued = 0
        if pattern_id is not None:
            item = QueueItem(
                idempotency_key=idempotency_key(_REFLECTION_SOURCE_ID, date_iso),
                payload={
                    "item_kind": "reflection",
                    "event_class": "reflection",
                    "kind": "pattern_reflection",
                    "pattern_id": pattern_id,
                    "window_ref": window_ref,
                    "date": date_iso,
                },
            )
            try:
                result = self._enqueue(item)
            except QueueFullError:
                logger.error(
                    "dream_pattern: enqueue failed — wombat_queue is at capacity; tonight's "
                    "pattern_reflection (pattern_id=%r) is dropped",
                    pattern_id,
                    exc_info=True,
                )
                errors += 1
            else:
                if result is EnqueueResult.QUEUED:
                    enqueued = 1
                else:
                    logger.debug(
                        "dream_pattern: pattern_reflection already queued for %s "
                        "(pattern_id=%r) — same-night re-fire, no-op",
                        date_iso,
                        pattern_id,
                    )

        return Transition(
            to="dream_run",
            output=Artifact(
                kind=DREAM_PATTERN_REPORT_KIND,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
                data={"enqueued": enqueued, "pattern_id": pattern_id, "errors": errors},
            ),
        )


__all__ = ["DREAM_PATTERN_REPORT_KIND", "PatternDetectorStage"]
