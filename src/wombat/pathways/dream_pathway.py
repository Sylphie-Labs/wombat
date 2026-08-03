"""build_dream_pathway — the wombat.dream pathway (TK-46 scaffold, TK-175 outcome pass, TK-47
consolidation sweep, TK-49 tuner pass, TK-111 behavior-log pass, TK-112 window-detect pass, TK-113
pattern-detect pass, TK-297 facts pass, TK-299 derive pass, TK-324 screenpipe pass, TK-346
biometrics pass, Q-33/Q-85/Q-90/Q-91/Q-98/Q-99e/Q-99f, DEC-12/DEC-23/DEC-66/DEC-70h).

MIRRORS ``brief_pathway.py``'s posture: pure graph assembly, no bootstrap import (avoids an import
cycle — ``bootstrap.py`` imports this module, not the reverse). TK-346 (RULING R6, superseding
TK-324's shape) RULES the dream graph's end-state: ``dream_consolidate`` (entry, TK-47) ->
``dream_outcome`` (TK-175) -> ``dream_tune`` (TK-49) -> ``dream_persona`` (TK-214) ->
``dream_facts`` (TK-297) -> ``dream_derive`` (TK-299) -> ``dream_observe`` (TK-314) ->
``dream_screenpipe`` (TK-324) -> ``dream_biometrics`` (TK-346) -> ``dream_behavior_log`` (TK-111)
-> ``dream_window`` (TK-112) -> ``dream_pattern`` (TK-113) -> ``dream_run`` (terminal) — TK-52's
later recurrence/fence inserts UPSTREAM of ``dream_consolidate`` so ``dream_run`` stays the ONE
reachable terminal and TK-46's isolation proofs keep passing.

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

``DreamTuneStage`` (TK-49, EP-14) is the nightly bounded rating-parameter tuner pass: it drives
``wombat.rating.rating_tuner.RatingTuner`` (already fully wired over the shared user-scope entity
KG/``ObservationWriter``/``OperatingParams``, injected here — this stage NEVER constructs a
``RatingTuner``) over ``ctx.clock()``. Deterministic, model-free (NG-4 intact) — no LLM call, no
``ctx.journal`` touch. A tuning failure is caught, logged LOUD, and the stage still transitions on
(mirrors ``DreamOutcomeStage``'s own per-pass error posture): one bad night's tuning pass must
never block the reachable terminal.

``DreamPersonaStage`` (TK-214, EP-35, DEC-36/DEC-37(h), Q-112) is the nightly bounded
persona-feedback tuner pass, inserted between ``dream_tune`` and ``dream_facts`` (TK-297): it folds
the trailing-24h window of ``wombat_behavior_events`` rows TK-213's ASR-seam recorder wrote
(``event_type='persona_feedback'``) through the PURE ``wombat.persona.tuner.decide_persona_steps``
(at most one clamped step per UNPINNED axis, RatingTuner-pattern custody) and applies any decided
steps to the shared ``LivePersona`` via the EXISTING ``wombat.persona.commands.apply`` saturating
clamp, then ``LivePersona.set(..., explicit=False)`` ONCE — a dream nudge never stamps the
DEC-37(h) 7-day explicit-set pin. Deterministic, model-free (NG-4 intact), no ``ctx.journal``
touch.

``DreamBehaviorLogStage`` (TK-111, EP-21, Q-98) is the nightly append-only behavioral-event-log
writer: it walks the CLOSED ``EventClass`` set exactly as ``DreamOutcomeStage`` does, reading
ACTIVE claims bearing a TERMINAL ``OUTCOME_*`` predicate (``OUTCOME_LOAD_BEARING``/
``OUTCOME_REGRETTED``/``OUTCOME_IGNORED`` — never ``OUTCOME_PENDING``) off the SAME shared
user-scope entity KG, and upserts one row per claim into ``wombat.behavior.event_log.
BehaviorEventLog`` keyed on the canonical TK-12 ``idempotency_key`` the claim's payload carries as
``item_ref``. It NEVER touches ``ctx.journal`` and makes NO model call. A per-claim failure
(malformed payload, an un-invertible ``item_ref``, or a store write failure) is caught, logged
LOUD, and skipped — mirrors ``DreamTuneStage``'s own never-block-the-terminal posture: one bad
night's write never blocks ``dream_run``.

``WriteWindowSummariesStage`` (TK-112, EP-21, Q-99e; ``wombat.behavior.stages.
write_window_summaries``) is the nightly ``dream_window`` stage — it is NOT defined in this
module (it lives with the behavioral event log it reads, ``wombat.behavior``), but is spliced into
this graph exactly like every other dream stage, between ``dream_behavior_log`` and
``dream_pattern`` (TK-113).

``DreamObserveStage`` (TK-314, EP-37, DEC-68(d)(2); ``wombat.behavior.stages.dream_observe``) is
the nightly ``dream_observe`` stage — NOT defined in this module (it lives beside the sibling
distillation passes in ``wombat.behavior.stages``), spliced in between ``dream_derive`` and
``dream_screenpipe`` (TK-324's stage, its new downstream neighbor, superseding
``dream_behavior_log``). PURE CODE, NO model call: it distills the ``wombat_observations`` ledger's
screen/mic segments through closed templates into ``UserFactsStore`` rows with
``source='behavior'`` (the DEC-66-reserved provenance tier).

``DreamScreenpipeStage`` (TK-324, EP-37, DEC-70h; ``wombat.behavior.stages.dream_screenpipe``) is
the nightly ``dream_screenpipe`` stage — also NOT defined in this module, spliced in between
``dream_observe`` and ``dream_biometrics`` (TK-346's stage, its new downstream neighbor, superseding
``dream_behavior_log``). Deterministically folds a trailing 21-day window of the injected
``ScreenpipeClient``'s search results into a bounded projection, THEN makes the ONE DEC-23-admitted
model call this stage ever makes (the ``DreamFactsStage`` seam pattern) to propose facts, clamped by
the SAME custody ``DreamFactsStage`` carries, and writes accepted proposals as ``UserFactsStore``
rows with ``source='behavior'`` (DEC-70h: the observational tier regardless of distillation
mechanism). A ``None`` client (the observe-toggle-off default) makes the stage structurally inert —
zero client/model contact, immediate onward transition.

``DreamBiometricsStage`` (TK-346, EP-41; ``wombat.behavior.stages.dream_biometrics``) is the
nightly ``dream_biometrics`` stage — also NOT defined in this module, spliced in between
``dream_screenpipe`` and ``dream_behavior_log`` (RULING R6: the only splice that touches neither
``dream_observe`` nor ``dream_facts``, both protected). The ``dream_observe`` pattern (TK-314)
pointed at a second ``wombat_observations`` channel, ``channel='biometric'`` (TK-341): PURE CODE,
NO model call, distilling closed sleep/resting-HR segments through two closed templates into
``UserFactsStore`` rows with ``source='behavior'``. A ``None`` ``ObservationStore`` (the
``wombat_observe_biometrics`` toggle-off default) degrades to a one-line nightly no-op.

``PatternDetectorStage`` (TK-113, EP-22, Q-99b/f/g; ``wombat.behavior.stages.pattern_detector``)
is the nightly ``dream_pattern`` stage — also NOT defined in this module (it lives alongside
``WriteWindowSummariesStage`` in ``wombat.behavior.stages``), spliced in between ``dream_window``
and the ``dream_run`` terminal. It reads the ``productivity_window`` claim ``dream_window`` just
wrote, matches it against the loaded psychology KB (TK-115/TK-116), and enqueues AT MOST ONE
``pattern_reflection`` ``QueueItem`` per night via the injected shared ``WombatQueue.enqueue``
(ASMP-2) — for the standard gate (EP-9) to judge next morning, never a gate bypass. NEVER touches
``ctx.journal`` and makes NO model call (mirrors every other dream stage's off-path posture).

``DreamScaffoldStage`` remains the reachable terminal, off-path (S1/S11), no-op stage — no
recurrence/fence (TK-52), no model call.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.coherence.reconciler import CoherenceReconciler
from cogworx.loop.graph import StageGraph
from cogworx.loop.result import Done, StageResult, Transition
from cogworx.loop.stage import Stage, StageContext
from cogworx.runtime.claim_extractor import ClaimExtractor
from cogworx.substrate.entity_kg import EntityKG

from wombat.behavior.event_log import BehaviorEventLog
from wombat.domain.item_identity import split_idempotency_key
from wombat.persona.commands import PersonaCommand
from wombat.persona.commands import apply as commands_apply
from wombat.persona.live import LivePersona
from wombat.persona.tuner import PERSONA_FEEDBACK_WINDOW_HOURS, decide_persona_steps
from wombat.rating.params import EventClass
from wombat.rating.rating_tuner import RatingTuner
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

# DreamTuneStage's committed output kind (TK-49) — a contentless, system-provenance proof that the
# night's tuning pass ran, mirroring DREAM_REPORT_KIND/DREAM_OUTCOME_REPORT_KIND's own
# contentless-proof idiom: the durable record is the rating_params claims RatingTuner wrote (or
# didn't, for a no-corpus class), never repeated onto this artifact.
DREAM_TUNE_REPORT_KIND = "wombat.dream_tune_report"

# DreamPersonaStage's committed output kind (TK-214, EP-35) — a small system-provenance count
# artifact mirroring DREAM_TUNE_REPORT_KIND's own idiom: per-axis direction/up_count/down_count
# only (CON-4/CON-6, motive-free), never a why — the durable record is the persisted persona
# matrix + pins LivePersona.set already wrote.
DREAM_PERSONA_REPORT_KIND = "wombat.dream_persona_report"

# DreamConsolidationStage's committed output kind (TK-47) — a system-provenance summary artifact:
# accumulated ReconcilerStats counters, claims extracted, ticks driven, and the stall flag. No
# claim/adjudication payloads ride this artifact (mirrors DREAM_REPORT_KIND/DREAM_OUTCOME_REPORT_
# KIND's own contentless-proof idiom) — the durable record is what the sweepers wrote to the KG.
DREAM_CONSOLIDATION_REPORT_KIND = "wombat.dream_consolidation_report"

# DreamBehaviorLogStage's committed output kind (TK-111, Q-98) — a contentless, system-provenance
# count artifact mirroring DREAM_TUNE_REPORT_KIND's own idiom: no claim/row payloads ride this
# artifact, only counts — the durable record is the wombat_behavior_events rows the stage upserted.
DREAM_BEHAVIOR_LOG_REPORT_KIND = "wombat.dream_behavior_log_report"

# The terminal OUTCOME_* predicate values DreamBehaviorLogStage logs (never OUTCOME_PENDING — an
# unresolved item has no outcome yet to log).
_TERMINAL_OUTCOME_PREDICATES = frozenset(
    {
        ClaimPredicate.OUTCOME_LOAD_BEARING.value,
        ClaimPredicate.OUTCOME_REGRETTED.value,
        ClaimPredicate.OUTCOME_IGNORED.value,
    }
)

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
    off-path posture). ``run()`` always ``Transition``s onward to ``dream_tune`` — even an
    entirely empty corpus (AC2) completes cleanly with zero claim writes.
    """

    name: str = "dream_outcome"
    transitions: tuple[str, ...] = ("dream_tune",)

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
            to="dream_tune",
            output=Artifact(
                kind=DREAM_OUTCOME_REPORT_KIND,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
                data={"items_collected": len(resolutions), "labeled": labeled, "errors": errors},
            ),
        )


class DreamTuneStage:
    """The nightly bounded rating-parameter tuner pass (TK-49, EP-14, Q-91).

    Keyword-injected collaborator only (TK-42/TK-44/TK-45/TK-175/TK-47 precedent): ``tuner`` is
    ``wombat.rating.rating_tuner.RatingTuner``, already fully wired over the SAME shared user-scope
    entity KG/``ObservationWriter`` and the loaded ``OperatingParams`` (``bootstrap.
    assemble_runtime``) — this stage NEVER constructs a ``RatingTuner`` itself.

    ``run()`` calls ``tuner.tune(ctx.clock())`` — deterministic, model-free (NG-4 intact): no LLM
    call, no ``ctx.journal`` touch (mirrors ``DreamOutcomeStage``'s own off-path posture). A
    tuning-pass failure is caught, logged LOUD, and the stage STILL transitions onward to
    ``dream_persona`` (TK-214, EP-35 — this stage's downstream neighbor since the persona-tuner
    pass was inserted between the rating tuner and ``dream_behavior_log``) — a bad night's tuning
    pass must never block the reachable terminal.
    """

    name: str = "dream_tune"
    transitions: tuple[str, ...] = ("dream_persona",)

    def __init__(self, *, tuner: RatingTuner) -> None:
        self._tuner = tuner

    async def run(self, ctx: StageContext) -> StageResult:
        now = ctx.clock()
        try:
            await self._tuner.tune(now)
        except Exception:
            logger.error(
                "dream_tune: RatingTuner.tune failed; tonight's tuning pass is skipped — rating "
                "params stay unchanged until the next successful run",
                exc_info=True,
            )

        return Transition(
            to="dream_persona",
            output=Artifact(
                kind=DREAM_TUNE_REPORT_KIND,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
                data={},
            ),
        )


class DreamPersonaStage:
    """The nightly bounded persona-feedback tuner pass (TK-214, EP-35, DEC-36/DEC-37(h), Q-112).

    Keyword-injected collaborators only (``RatingTuner``/``DreamTuneStage`` precedent):
    ``event_log`` is ``wombat.behavior.event_log.BehaviorEventLog`` (the SAME shared instance
    ``DreamBehaviorLogStage`` and the bootstrap-owned persona-feedback recorder both already
    write/read — this stage never constructs one); ``live_persona`` is the SAME shared
    ``wombat.persona.live.LivePersona`` runtime authority every mouth call site reads.

    ``run()`` reads the trailing ``wombat.persona.tuner.PERSONA_FEEDBACK_WINDOW_HOURS`` window of
    ``event_log.events_between`` rows filtered to ``event_type == 'persona_feedback'``, decides via
    the PURE ``wombat.persona.tuner.decide_persona_steps`` (fed each row's ``outcome_label`` phrase
    plus ``live_persona.pinned_axes(now)`` — a pinned axis never steps), then applies every decided
    step to ``live_persona.matrix`` via the EXISTING ``wombat.persona.commands.apply`` saturating
    clamp (no second clamp/custody mechanism) and calls ``live_persona.set(new_matrix,
    explicit=False)`` EXACTLY ONCE if any axis stepped — a dream nudge never stamps a pin (DEC-37
    (h)), so a second consecutive night's fresh signal can still step again.

    Deterministic, model-free (NG-4 intact): no LLM call, no ``ctx.journal`` touch. One INFO
    journal line per stepped axis names the axis, direction, and the up/down counts that drove it
    (CON-4: counts only, motive-free CON-6 — never a why). A raising collaborator (a bad
    ``events_between`` read, a malformed matrix apply, a ``live_persona.set`` failure) is caught,
    logged ERROR, and the stage STILL transitions onward to ``dream_facts`` (TK-297, EP-13 — this
    stage's downstream neighbor since the getting-to-know pass was inserted between the
    persona-tuner pass and ``dream_behavior_log``, later resplit by TK-299's ``dream_derive``
    insertion between ``dream_facts`` and ``dream_behavior_log``; mirrors ``DreamTuneStage``'s own
    never-block-the-terminal posture) — one bad night's persona-tuning pass must never block the
    reachable terminal.
    """

    name: str = "dream_persona"
    transitions: tuple[str, ...] = ("dream_facts",)

    def __init__(self, *, event_log: BehaviorEventLog, live_persona: LivePersona) -> None:
        self._event_log = event_log
        self._live_persona = live_persona

    async def run(self, ctx: StageContext) -> StageResult:
        now = ctx.clock()
        stepped: list[dict[str, int | str]] = []

        try:
            window_start = now - timedelta(hours=PERSONA_FEEDBACK_WINDOW_HOURS)
            events = self._event_log.events_between(window_start, now)
            phrases = [
                event.outcome_label for event in events if event.event_type == "persona_feedback"
            ]
            pinned_axes = self._live_persona.pinned_axes(now)
            decisions = decide_persona_steps(phrases, pinned_axes)

            if decisions:
                matrix = self._live_persona.matrix
                for decision in decisions:
                    matrix = commands_apply(
                        matrix, PersonaCommand(axis=decision.axis, step=decision.direction)
                    )
                self._live_persona.set(matrix, explicit=False)

                for decision in decisions:
                    direction_word = "up" if decision.direction == 1 else "down"
                    logger.info(
                        "dream_persona: stepped axis=%s direction=%s up_count=%d down_count=%d",
                        decision.axis,
                        direction_word,
                        decision.up_count,
                        decision.down_count,
                    )
                    stepped.append(
                        {
                            "axis": decision.axis,
                            "direction": direction_word,
                            "up_count": decision.up_count,
                            "down_count": decision.down_count,
                        }
                    )
        except Exception:
            logger.error(
                "dream_persona: tonight's persona-feedback tuning pass failed; the persona "
                "matrix stays unchanged until the next successful run",
                exc_info=True,
            )

        return Transition(
            to="dream_facts",
            output=Artifact(
                kind=DREAM_PERSONA_REPORT_KIND,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
                data={"stepped": stepped},
            ),
        )


class DreamBehaviorLogStage:
    """The nightly append-only behavioral-event-log write pass (TK-111, EP-21, Q-98).

    Keyword-injected collaborators only (TK-42/TK-44/TK-45/TK-175/TK-47/TK-49 precedent):
    ``store`` is ``wombat.behavior.event_log.BehaviorEventLog`` (this stage never constructs
    one); ``entity_kg`` is the RAW cog-worx ``EntityKG`` Protocol, read the SAME way
    ``DreamOutcomeStage`` does (no ``ScopedKG``/write token minted here — this stage never writes
    to the entity KG); ``user_id`` forms the ``user:<user_id>`` scope every read is restricted to.

    COLLECTION (Q-98, mirrors ``DreamOutcomeStage``'s own idiom exactly): for each ``EventClass``
    member, read ``claims_about(event_class.value, limit=..., scope=...)`` and keep the ACTIVE
    (``valid_to is None``) claims whose predicate is a TERMINAL ``OUTCOME_*`` value
    (``OUTCOME_LOAD_BEARING``/``OUTCOME_REGRETTED``/``OUTCOME_IGNORED`` — never
    ``OUTCOME_PENDING``, an unresolved item has no outcome yet to log). Each claim's ``payload``
    is the double-encoded envelope ``OutcomeLabeler.label_terminal`` writes: ``json.loads(claim.
    payload)['value']`` parsed again yields ``{item_ref, outcome, source, rule_name,
    resolved_at}``.

    ROW MAPPING (Q-98 ruling c): ``idempotency_key`` = the claim payload's ``item_ref`` (the
    canonical TK-12 key, verbatim — never re-derived); ``event_type`` = the enumerating
    ``event_class.value``; ``source_id`` = ``domain.item_identity.split_idempotency_key(item_ref)
    ``'s first element (the pure inverse, TK-111); ``timestamp_utc`` = the payload's
    ``resolved_at``; ``outcome_label`` = the claim's own predicate value (already one of TK-43's
    closed ``OUTCOME_*`` members — no motive field exists, CON-6/NG-1); ``duration_seconds`` =
    ``None`` (v1: no duration signal exists yet, recorded honestly rather than synthesized).

    NO ``ctx.journal`` touch and NO model call (mirrors ``DreamOutcomeStage``'s own off-path
    posture). A per-claim failure — a malformed payload, an ``item_ref`` that
    ``split_idempotency_key`` cannot invert, or a ``store.upsert`` failure — is caught, logged
    LOUD, and skipped (AC5: never a partial write, never blocks the rest of the pass). ``run()``
    ALWAYS ``Transition``s onward to ``dream_window`` (TK-112, Q-99e — this stage's downstream
    neighbor since the window-detect pass was inserted between the behavior log and the
    terminal) — mirrors ``DreamTuneStage``'s own never-block-the-terminal posture: one bad
    night's write never blocks the reachable terminal.
    """

    name: str = "dream_behavior_log"
    transitions: tuple[str, ...] = ("dream_window",)

    def __init__(self, *, store: BehaviorEventLog, entity_kg: EntityKG, user_id: str) -> None:
        self._store = store
        self._entity_kg = entity_kg
        self._user_id = user_id
        self._scope = f"user:{user_id}"

    async def run(self, ctx: StageContext) -> StageResult:
        now = ctx.clock()

        rows_upserted = 0
        errors = 0

        for event_class in EventClass:
            try:
                scored_claims = await self._entity_kg.claims_about(
                    event_class.value, limit=_CLAIMS_LIMIT, scope=self._scope
                )
            except Exception:
                logger.error(
                    "dream_behavior_log: claims_about failed collecting terminal OUTCOME_* "
                    "claims (event_class=%r); skipping this class for tonight's pass",
                    event_class.value,
                    exc_info=True,
                )
                errors += 1
                continue

            for scored_claim in scored_claims:
                claim = scored_claim.claim
                if claim.valid_to is not None:
                    continue  # not ACTIVE — already invalidated (superseded or defeated)
                predicate = claim.predicate
                if predicate is None or predicate not in _TERMINAL_OUTCOME_PREDICATES:
                    continue  # OUTCOME_PENDING (or anything else) — not a terminal outcome yet

                try:
                    envelope = json.loads(claim.payload)
                    outcome_value = json.loads(envelope["value"])
                    item_ref = outcome_value["item_ref"]
                    resolved_at = datetime.fromisoformat(outcome_value["resolved_at"])
                    source_id, _source_natural_id = split_idempotency_key(item_ref)
                except Exception:
                    logger.error(
                        "dream_behavior_log: malformed terminal OUTCOME_* claim payload or "
                        "un-invertible item_ref (claim_id=%r, event_class=%r); skipping this row",
                        claim.id,
                        event_class.value,
                        exc_info=True,
                    )
                    errors += 1
                    continue

                try:
                    self._store.upsert(
                        idempotency_key=item_ref,
                        event_type=event_class.value,
                        source_id=source_id,
                        timestamp_utc=resolved_at,
                        outcome_label=predicate,
                    )
                    rows_upserted += 1
                except Exception:
                    logger.error(
                        "dream_behavior_log: BehaviorEventLog.upsert failed (item_ref=%r, "
                        "event_class=%r); skipping this row — it will be retried on a later "
                        "night if the claim is still ACTIVE",
                        item_ref,
                        event_class.value,
                        exc_info=True,
                    )
                    errors += 1

        return Transition(
            to="dream_window",
            output=Artifact(
                kind=DREAM_BEHAVIOR_LOG_REPORT_KIND,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
                data={"rows_upserted": rows_upserted, "errors": errors},
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
    consolidate: Stage,
    outcome: Stage,
    tune: Stage,
    persona: Stage,
    facts: Stage,
    derive: Stage,
    observe: Stage,
    screenpipe: Stage,
    biometrics: Stage,
    behavior_log: Stage,
    window: Stage,
    pattern: Stage,
    terminal: Stage | None = None,
) -> StageGraph:
    """Assemble the ``wombat.dream`` ``StageGraph``, entered at ``consolidate.name`` (TK-346
    end-state, superseding TK-324's shape: ``dream_consolidate`` -> ``dream_outcome`` ->
    ``dream_tune`` -> ``dream_persona`` -> ``dream_facts`` -> ``dream_derive`` ->
    ``dream_observe`` -> ``dream_screenpipe`` -> ``dream_biometrics`` -> ``dream_behavior_log`` ->
    ``dream_window`` -> ``dream_pattern`` -> ``dream_run``,
    TK-47/TK-175/TK-49/TK-214/TK-297/TK-299/TK-314/TK-324/TK-346/TK-111/TK-112/TK-113).

    ``consolidate``, ``outcome``, ``tune``, ``persona``, ``facts``, ``derive``, ``observe``,
    ``screenpipe``, ``biometrics``, ``behavior_log``, ``window``, and ``pattern`` are ALL REQUIRED
    and supplied by the caller (mirrors ``build_brief_pathway``'s all-stages-injected convention) —
    production callers pass a ``DreamConsolidationStage`` built with its real
    ``reconciler``/``extractor`` collaborators (TK-54's ``build_dream_substrate``), a
    ``DreamOutcomeStage`` built with its real ``entity_kg``/``labeler``/``user_id`` collaborators,
    a ``DreamTuneStage`` built with its real ``RatingTuner``, a ``DreamPersonaStage`` (TK-214)
    built with its real ``event_log``/``live_persona`` collaborators, a ``DreamFactsStage``
    (``wombat.behavior.stages.dream_facts``, TK-297) built with its real
    ``model``/``chat_turns``/``user_facts`` collaborators, a ``DreamDeriveStage``
    (``wombat.behavior.stages.dream_derive``, TK-299) built with its real
    ``external_items``/``user_facts`` collaborators, a ``DreamObserveStage``
    (``wombat.behavior.stages.dream_observe``, TK-314) built with its real
    ``observations``/``user_facts`` collaborators, a ``DreamScreenpipeStage``
    (``wombat.behavior.stages.dream_screenpipe``, TK-324) built with its real
    ``client``/``model``/``user_facts``/``tz`` collaborators, a ``DreamBiometricsStage``
    (``wombat.behavior.stages.dream_biometrics``, TK-346) built with its real
    ``observations``/``user_facts`` collaborators, a ``DreamBehaviorLogStage`` built with its real
    ``store``/``entity_kg``/``user_id`` collaborators, a ``WriteWindowSummariesStage``
    (``wombat.behavior.stages.write_window_summaries``, TK-112) built with its real
    ``store``/``writer``/``tz`` collaborators, and a ``PatternDetectorStage``
    (``wombat.behavior.stages.pattern_detector``, TK-113) built with its real
    ``entity_kg``/``kb``/``enqueue``/``user_id``/``tz`` collaborators; this module never constructs
    those (no bootstrap import, pure graph assembly). ``terminal`` KEEPS the TK-46
    injectable-stage seam: it defaults to ``DreamScaffoldStage()`` — since
    ``PatternDetectorStage.transitions`` names the literal ``"dream_run"`` target, a substituted
    terminal double (e.g. an always-raising stage, AC2's off-path error-isolation proof) must
    keep that SAME name to be reachable.
    """
    dream_terminal = terminal if terminal is not None else DreamScaffoldStage()
    return StageGraph(
        [
            consolidate,
            outcome,
            tune,
            persona,
            facts,
            derive,
            observe,
            screenpipe,
            biometrics,
            behavior_log,
            window,
            pattern,
            dream_terminal,
        ],
        entry=consolidate.name,
    )


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
    "DREAM_BEHAVIOR_LOG_REPORT_KIND",
    "DREAM_CONSOLIDATION_REPORT_KIND",
    "DREAM_OUTCOME_REPORT_KIND",
    "DREAM_PATHWAY_ID",
    "DREAM_PERSONA_REPORT_KIND",
    "DREAM_REPORT_KIND",
    "DREAM_TRIGGER_KIND",
    "DREAM_TUNE_REPORT_KIND",
    "MAX_TICKS",
    "DreamBehaviorLogStage",
    "DreamConsolidationStage",
    "DreamOutcomeStage",
    "DreamPersonaStage",
    "DreamScaffoldStage",
    "DreamTuneStage",
    "build_dream_pathway",
    "dream_trigger_artifact",
]
