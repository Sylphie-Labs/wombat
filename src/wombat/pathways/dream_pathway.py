"""build_dream_pathway — the wombat.dream pathway (TK-46 scaffold, TK-175 outcome pass, TK-47
consolidation sweep, Q-33/Q-85/Q-90, DEC-12/DEC-23).

MIRRORS ``brief_pathway.py``'s posture: pure graph assembly, no bootstrap import (avoids an import
cycle — ``bootstrap.py`` imports this module, not the reverse). Q-90 RULES the dream graph's
end-state: ``dream_consolidate`` (entry, TK-47) -> ``dream_outcome`` (TK-175) -> ``dream_run``
(terminal) — TK-52's later recurrence/fence inserts UPSTREAM of ``dream_consolidate`` so
``dream_run`` stays the ONE reachable terminal and TK-46's isolation proofs keep passing.

``DreamConsolidationStage`` (TK-47, EP-13) is the nightly consolidation sweep: it drives
cog-worx's ``CoherenceReconciler`` + ``ClaimExtractor`` sweepers to drain, off-path (S1) model
inference DEC-23 admits here (the model arrives ALREADY budget-guarded from TK-54 — this stage
never constructs one) while the GATE stays model-free (NG-4 intact). DEC-12: wombat composes the
shipped cog-worx sweepers, no bespoke extraction/reconciliation logic.

``DreamOutcomeStage`` (TK-175, EP-12) is the nightly collect/infer/label pass: it walks the CLOSED
``EventClass`` set, collects ACTIVE ``OUTCOME_PENDING`` claims + their ``BEHAVIOR_OBSERVED``
feedback off the shared user-scope entity KG (read via the raw ``EntityKG.claims_about`` exactly
as ``UserModel`` does — no ``ScopedKG``/write-token minting here), folds them through TK-50's pure
``infer_outcomes``, and writes each resulting terminal label via TK-45's ``OutcomeLabeler`` (the
invalidate-then-assert supersede). It NEVER touches ``ctx.journal`` and makes NO model call — a
per-item failure is caught, logged LOUD, and skipped so one bad item never kills the night's pass.

``DreamScaffoldStage`` remains the reachable terminal, off-path (S1/S11), no-op stage — no tuner
(TK-49), no recurrence/fence (TK-52), no model call.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.coherence.reconciler import CoherenceReconciler
from cogworx.loop.graph import StageGraph
from cogworx.loop.result import Done, StageResult, Transition
from cogworx.loop.stage import Stage, StageContext
from cogworx.runtime.claim_extractor import ClaimExtractor
from cogworx.substrate.entity_kg import EntityKG

from wombat.rating.params import EventClass
from wombat.user_model.claims import ClaimPredicate
from wombat.user_model.feedback_source import FeedbackSignal
from wombat.user_model.outcome_inference import ItemResolution, infer_outcomes
from wombat.user_model.outcome_labeler import OutcomeLabeler

logger = logging.getLogger(__name__)

DREAM_PATHWAY_ID = "wombat.dream"

# The seed artifact's kind (mirrors brief_pathway.py's own BRIEF_TRIGGER_KIND convention).
DREAM_TRIGGER_KIND = "wombat.dream_trigger"

# DreamScaffoldStage's committed output kind — a contentless, provenance-bearing proof that the
# off-path run happened, nothing more.
DREAM_REPORT_KIND = "wombat.dream_report"

# DreamOutcomeStage's committed output kind — a small system-provenance count artifact (TK-175),
# following DREAM_REPORT_KIND's own contentless-proof idiom: no claim payloads ride this artifact,
# only counts (the claims themselves are the durable record, written straight to the entity KG).
DREAM_OUTCOME_REPORT_KIND = "wombat.dream_outcome_report"

# DreamConsolidationStage's committed output kind (TK-47) — a system-provenance summary artifact:
# accumulated ReconcilerStats counters, claims extracted, ticks driven, and the stall flag. No
# claim/adjudication payloads ride this artifact (mirrors DREAM_REPORT_KIND/DREAM_OUTCOME_REPORT_
# KIND's own contentless-proof idiom) — the durable record is what the sweepers wrote to the KG.
DREAM_CONSOLIDATION_REPORT_KIND = "wombat.dream_consolidation_report"

# Q-90 v1 shape: a generous per-query ceiling. wombat is single-user, nightly-cadence — the real
# per-event-class/per-item corpus is bounded by one day's drain volume, nowhere near this ceiling.
_CLAIMS_LIMIT = 500

# TK-47 (Q-90 ruled): a hard structural safety cap on DreamConsolidationStage's run-to-drain loop
# — NOT a tunable, NOT expected to bind. wombat is single-user, nightly-cadence (mirrors
# _CLAIMS_LIMIT's own generous-ceiling reasoning above): one night's dirty-subject/journal-step
# volume is nowhere near this many ticks. It exists purely so a pathological loop cannot spin
# forever; hitting it is logged loud and the stage still transitions on.
MAX_TICKS = 100


class DreamConsolidationStage:
    """The nightly consolidation sweep: drive cog-worx's ``CoherenceReconciler`` +
    ``ClaimExtractor`` to drain (TK-47, EP-13, DEC-12/DEC-23, Q-90).

    Keyword-injected collaborators only (TK-42/TK-44/TK-45/TK-175 precedent): ``reconciler`` is
    cog-worx's ``CoherenceReconciler`` (already wired over the shared entity KG's
    ``CoherenceStore`` surface + a ``ConsistencyOracle``, TK-54's ``build_dream_substrate``);
    ``extractor`` is cog-worx's ``ClaimExtractor`` (already wired over the substrate journal, the
    SAME shared entity KG, and a budget-guarded ``Model``, also TK-54). This stage never
    constructs either collaborator and never constructs a model — DEC-23 rules the model arrives
    ALREADY budget-guarded, and DEC-12 forbids bespoke extraction/reconciliation logic here. The
    off-path (S1) model inference DEC-23 admits lives entirely inside ``extractor``/the oracle
    backing ``reconciler``; the GATE stays model-free (NG-4 intact) — this stage is never reached
    from the gate path.

    RUN-TO-DRAIN LOOP (Q-90 ruled): both sweepers are tick()-shaped, so this stage owns a BOUNDED
    loop capped at ``MAX_TICKS`` (a hard structural safety cap, not a tunable). Each iteration
    calls ``reconciler.tick()`` FIRST — it NEVER raises; a per-subject failure is caught inside
    cog-worx and counted in its own ``ReconcilerStats.subjects_failed`` — THEN
    ``extractor.tick()`` inside a try/except. The extractor is FAIL-STALL BY RAISING (a model
    failure or a JSON-parse failure leaves its cursor un-advanced, cog-worx D6): a raise is
    caught, logged LOUD naming the stall, and the loop stops immediately (``stalled=True``) — a
    stalled extractor must NEVER prevent this stage from transitioning onward to ``dream_outcome``
    (the outcome/labeling pass downstream must never be blocked by an upstream sweeper stall).

    TERMINATION: absent a stall, the loop stops the moment BOTH sweepers report zero further work
    in the SAME iteration — ``stats.subjects_processed == 0`` (no dirty subjects were even
    fetched off ``CoherenceStore.claim_dirty_subjects`` this tick) AND the extractor's own
    ``count == 0`` — that is one fully drained pass. Hitting ``MAX_TICKS`` without draining is
    logged loud; the stage still transitions on rather than blocking the night's outcome pass.

    Output: ALWAYS ``Transition``s to ``dream_outcome`` carrying a system-provenance
    reconciliation-summary artifact — the accumulated ``ReconcilerStats`` counters (summed across
    every tick this pass drove), the total claims extracted, the tick count, and the stall flag.
    NEVER written via ``ctx.journal`` directly (mirrors ``DreamOutcomeStage``'s own posture).
    """

    name: str = "dream_consolidate"
    transitions: tuple[str, ...] = ("dream_outcome",)

    def __init__(self, *, reconciler: CoherenceReconciler, extractor: ClaimExtractor) -> None:
        self._reconciler = reconciler
        self._extractor = extractor

    async def run(self, ctx: StageContext) -> StageResult:
        now = ctx.clock()

        ticks = 0
        claims_extracted = 0
        stalled = False
        subjects_processed = 0
        subjects_cleared = 0
        subjects_defeated = 0
        subjects_escalated = 0
        subjects_skipped = 0
        subjects_failed = 0
        oracle_calls = 0
        promotions = 0

        for tick in range(1, MAX_TICKS + 1):
            ticks = tick
            stats = await self._reconciler.tick()
            subjects_processed += stats.subjects_processed
            subjects_cleared += stats.subjects_cleared
            subjects_defeated += stats.subjects_defeated
            subjects_escalated += stats.subjects_escalated
            subjects_skipped += stats.subjects_skipped
            subjects_failed += stats.subjects_failed
            oracle_calls += stats.oracle_calls
            promotions += stats.promotions

            try:
                count = await self._extractor.tick()
            except Exception:
                logger.error(
                    "dream_consolidate: STALLED — ClaimExtractor.tick() raised on tick %d; the "
                    "extractor's cursor stays un-advanced and will retry on a later dream run, "
                    "but this run's dream_outcome/dream_run stages still proceed",
                    tick,
                    exc_info=True,
                )
                stalled = True
                break

            claims_extracted += count

            if stats.subjects_processed == 0 and count == 0:
                break
        else:
            logger.warning(
                "dream_consolidate: hit MAX_TICKS=%d without draining (subjects_processed=%d, "
                "claims_extracted=%d so far) — transitioning onward regardless",
                MAX_TICKS,
                subjects_processed,
                claims_extracted,
            )

        logger.info(
            "dream_consolidate: drained in %d tick(s) — claims_extracted=%d "
            "subjects_processed=%d subjects_cleared=%d subjects_defeated=%d "
            "subjects_escalated=%d subjects_skipped=%d subjects_failed=%d stalled=%s",
            ticks,
            claims_extracted,
            subjects_processed,
            subjects_cleared,
            subjects_defeated,
            subjects_escalated,
            subjects_skipped,
            subjects_failed,
            stalled,
        )

        return Transition(
            to="dream_outcome",
            output=Artifact(
                kind=DREAM_CONSOLIDATION_REPORT_KIND,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
                data={
                    "ticks": ticks,
                    "stalled": stalled,
                    "claims_extracted": claims_extracted,
                    "subjects_processed": subjects_processed,
                    "subjects_cleared": subjects_cleared,
                    "subjects_defeated": subjects_defeated,
                    "subjects_escalated": subjects_escalated,
                    "subjects_skipped": subjects_skipped,
                    "subjects_failed": subjects_failed,
                    "oracle_calls": oracle_calls,
                    "promotions": promotions,
                },
            ),
        )


class DreamOutcomeStage:
    """The nightly outcome collect/infer/label pass (TK-175, EP-12, Q-90 v1 shape).

    Keyword-injected collaborators only (TK-42/TK-44/TK-45 precedent): ``entity_kg`` is the RAW
    cog-worx ``EntityKG`` Protocol — read the SAME way ``UserModel`` does (no ``ScopedKG``/write
    token minted here, this stage never writes to the KG directly); ``labeler`` is TK-45's
    ``OutcomeLabeler``, the sole write seam for the terminal ``OUTCOME_*`` claim; ``user_id`` forms
    the ``user:<user_id>`` scope every read is restricted to.

    COLLECTION (Q-90 v1, FIXED): for each ``EventClass`` member, read ``claims_about(event_class.
    value, limit=..., scope=...)`` and keep the ACTIVE (``valid_to is None`` — not invalidated)
    claims whose predicate is ``ClaimPredicate.OUTCOME_PENDING``. ``ObservationWriter`` stores a
    claim's JSON-native value inside an envelope (``{"value": <value JSON string>, "event_id":
    ...}``) written as the claim's ``payload`` — this stage double-parses to reach the pending
    payload's ``{item_ref, disposition, resolved_at}``. Every collected item is RULED TTL-expired
    in v1 (``ItemResolution.ttl_expired=True`` unconditionally — no partial-night/TTL-window
    concept exists yet). Feedback: per collected ``item_ref``, ``claims_about(item_ref)`` filtered
    to the ACTIVE ``BEHAVIOR_OBSERVED`` predicate yields a ``FeedbackSignal`` (the newest active
    match; TK-176's hot path writes at most one). ``calendar_deltas``/``draft_fates`` stay EMPTY —
    those producers are a recorded v1 residual (TK-79/later; TK-50's rules are fixture-proven).

    PASS: the collected inputs fold through TK-50's pure ``infer_outcomes`` into one
    ``OutcomeSignal`` per item, then each signal is written via ``labeler.label_terminal`` —
    invalidate-then-assert supersede of that item's PENDING claim. A per-item failure (a malformed
    claim payload, or a labeler write failure) is caught, logged LOUD, and skipped — one bad item
    never kills the night's pass.

    NEVER touches ``ctx.journal`` and makes NO model call (mirrors ``DreamScaffoldStage``'s own
    off-path posture). ``run()`` always ``Transition``s onward to ``dream_run`` — even an entirely
    empty corpus (AC2) completes cleanly with zero claim writes.
    """

    name: str = "dream_outcome"
    transitions: tuple[str, ...] = ("dream_run",)

    def __init__(self, *, entity_kg: EntityKG, labeler: OutcomeLabeler, user_id: str) -> None:
        self._entity_kg = entity_kg
        self._labeler = labeler
        self._user_id = user_id
        self._scope = f"user:{user_id}"

    async def run(self, ctx: StageContext) -> StageResult:
        now = ctx.clock()

        resolutions: list[ItemResolution] = []
        pending_claim_id_by_item: dict[str, str] = {}
        event_class_by_item: dict[str, EventClass] = {}
        errors = 0

        for event_class in EventClass:
            try:
                scored_claims = await self._entity_kg.claims_about(
                    event_class.value, limit=_CLAIMS_LIMIT, scope=self._scope
                )
            except Exception:
                logger.error(
                    "dream_outcome: claims_about failed collecting OUTCOME_PENDING claims "
                    "(event_class=%r); skipping this class for tonight's pass",
                    event_class.value,
                    exc_info=True,
                )
                errors += 1
                continue

            for scored_claim in scored_claims:
                claim = scored_claim.claim
                if claim.valid_to is not None:
                    continue  # not ACTIVE — already invalidated (superseded or defeated)
                if claim.predicate != ClaimPredicate.OUTCOME_PENDING.value:
                    continue

                try:
                    envelope = json.loads(claim.payload)
                    pending_value = json.loads(envelope["value"])
                    item_ref = pending_value["item_ref"]
                    resolution = ItemResolution(
                        item_ref=item_ref,
                        disposition=pending_value["disposition"],
                        resolved_at=datetime.fromisoformat(pending_value["resolved_at"]),
                        ttl_expired=True,
                    )
                except Exception:
                    logger.error(
                        "dream_outcome: malformed OUTCOME_PENDING claim payload (claim_id=%r, "
                        "event_class=%r); skipping this item",
                        claim.id,
                        event_class.value,
                        exc_info=True,
                    )
                    errors += 1
                    continue

                resolutions.append(resolution)
                pending_claim_id_by_item[item_ref] = claim.id
                event_class_by_item[item_ref] = event_class

        feedback: list[FeedbackSignal] = []
        for item_ref in pending_claim_id_by_item:
            try:
                scored_feedback = await self._entity_kg.claims_about(
                    item_ref, limit=_CLAIMS_LIMIT, scope=self._scope
                )
            except Exception:
                logger.error(
                    "dream_outcome: claims_about failed collecting BEHAVIOR_OBSERVED feedback "
                    "(item_ref=%r); treating this item as having no feedback",
                    item_ref,
                    exc_info=True,
                )
                errors += 1
                continue

            for scored_claim in scored_feedback:
                claim = scored_claim.claim
                if claim.valid_to is not None:
                    continue
                if claim.predicate != ClaimPredicate.BEHAVIOR_OBSERVED.value:
                    continue
                try:
                    envelope = json.loads(claim.payload)
                    behavior_value = json.loads(envelope["value"])
                    feedback.append(
                        FeedbackSignal(item_ref=item_ref, response=behavior_value["response"])
                    )
                except Exception:
                    logger.error(
                        "dream_outcome: malformed BEHAVIOR_OBSERVED claim payload (claim_id=%r, "
                        "item_ref=%r); treating this item as having no feedback",
                        claim.id,
                        item_ref,
                        exc_info=True,
                    )
                    errors += 1
                    continue
                break  # newest-first — the first active match is the feedback this item carries

        signals = infer_outcomes(tuple(resolutions), feedback=tuple(feedback))

        labeled = 0
        for signal in signals:
            try:
                await self._labeler.label_terminal(
                    pending_claim_id=pending_claim_id_by_item[signal.item_ref],
                    event_class=event_class_by_item[signal.item_ref],
                    signal=signal,
                    resolved_at=now,
                )
                labeled += 1
            except Exception:
                logger.error(
                    "dream_outcome: label_terminal failed (item_ref=%r); skipping — the item's "
                    "PENDING claim stays active and will be re-collected on a later night",
                    signal.item_ref,
                    exc_info=True,
                )
                errors += 1

        return Transition(
            to="dream_run",
            output=Artifact(
                kind=DREAM_OUTCOME_REPORT_KIND,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
                data={"items_collected": len(resolutions), "labeled": labeled, "errors": errors},
            ),
        )


class DreamScaffoldStage:
    """The reachable terminal ``wombat.dream`` stage (TK-46 scaffold; ``transitions=()``).

    Does NOT call the model and NEVER touches ``ctx.journal`` directly (DEC-12/DEC-23 — model
    inference is admitted only in ``DreamConsolidationStage``'s sweepers, upstream of this
    scaffold, never here). Provenance is ``source="system"`` (the as-built control-plane
    convention, mirrors ``brief_trigger_artifact``).
    """

    name: str = "dream_run"
    transitions: tuple[str, ...] = ()

    async def run(self, ctx: StageContext) -> StageResult:
        return Done(
            output=Artifact(
                kind=DREAM_REPORT_KIND,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={"changes": 0, "scaffold": True},
            )
        )


def build_dream_pathway(
    consolidate: Stage, outcome: Stage, terminal: Stage | None = None
) -> StageGraph:
    """Assemble the ``wombat.dream`` ``StageGraph``, entered at ``consolidate.name`` (Q-90
    end-state: ``dream_consolidate`` -> ``dream_outcome`` -> ``dream_run``, TK-47/TK-175).

    ``consolidate`` and ``outcome`` are BOTH REQUIRED and supplied by the caller (mirrors
    ``build_brief_pathway``'s all-stages-injected convention) — production callers pass a
    ``DreamConsolidationStage`` built with its real ``reconciler``/``extractor`` collaborators
    (TK-54's ``build_dream_substrate``) and a ``DreamOutcomeStage`` built with its real
    ``entity_kg``/``labeler``/``user_id`` collaborators; this module never constructs those (no
    bootstrap import, pure graph assembly). ``terminal`` KEEPS the TK-46 injectable-stage seam: it
    defaults to ``DreamScaffoldStage()`` — since ``DreamOutcomeStage.transitions`` names the
    literal ``"dream_run"`` target, a substituted terminal double (e.g. an always-raising stage,
    AC2's off-path error-isolation proof) must keep that SAME name to be reachable.
    """
    dream_terminal = terminal if terminal is not None else DreamScaffoldStage()
    return StageGraph([consolidate, outcome, dream_terminal], entry=consolidate.name)


def dream_trigger_artifact(now: datetime) -> Artifact:
    """The initial drive's input for ``wombat.dream`` — a system-provenanced, contentless trigger
    (mirrors ``brief_trigger_artifact``). No dream stage reads this artifact's ``data``; it only
    satisfies the engine's ``initial: Artifact`` requirement to start a run.
    """
    return Artifact(
        kind=DREAM_TRIGGER_KIND,
        produced_by="wombat.runtime",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
        data={},
    )


__all__ = [
    "DREAM_CONSOLIDATION_REPORT_KIND",
    "DREAM_OUTCOME_REPORT_KIND",
    "DREAM_PATHWAY_ID",
    "DREAM_REPORT_KIND",
    "DREAM_TRIGGER_KIND",
    "MAX_TICKS",
    "DreamConsolidationStage",
    "DreamOutcomeStage",
    "DreamScaffoldStage",
    "build_dream_pathway",
    "dream_trigger_artifact",
]
