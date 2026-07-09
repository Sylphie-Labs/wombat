"""build_dream_pathway — the wombat.dream pathway (TK-46 scaffold, TK-175 outcome pass, Q-33/
Q-85/Q-90, DEC-23).

MIRRORS ``brief_pathway.py``'s posture: pure graph assembly, no bootstrap import (avoids an import
cycle — ``bootstrap.py`` imports this module, not the reverse). Q-90 RULES the dream graph's
end-state: ``dream_outcome`` (entry) -> ``dream_run`` (terminal) — every later dream ticket
(TK-47's reconciler/extractor, TK-52's recurrence/fence) inserts UPSTREAM of ``dream_outcome`` so
``dream_run`` stays the ONE reachable terminal and TK-46's isolation proofs keep passing.

``DreamOutcomeStage`` (TK-175, EP-12) is the nightly collect/infer/label pass: it walks the CLOSED
``EventClass`` set, collects ACTIVE ``OUTCOME_PENDING`` claims + their ``BEHAVIOR_OBSERVED``
feedback off the shared user-scope entity KG (read via the raw ``EntityKG.claims_about`` exactly
as ``UserModel`` does — no ``ScopedKG``/write-token minting here), folds them through TK-50's pure
``infer_outcomes``, and writes each resulting terminal label via TK-45's ``OutcomeLabeler`` (the
invalidate-then-assert supersede). It NEVER touches ``ctx.journal`` and makes NO model call — a
per-item failure is caught, logged LOUD, and skipped so one bad item never kills the night's pass.

``DreamScaffoldStage`` remains the reachable terminal, off-path (S1/S11), no-op stage — no tuner,
no reconciler/extractor (TK-47), no recurrence/fence (TK-52), no model call. Those land once
TK-150 (residency predicate) unblocks the reconciler/extractor cluster (Q-33).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.graph import StageGraph
from cogworx.loop.result import Done, StageResult, Transition
from cogworx.loop.stage import Stage, StageContext
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
# off-path run happened, nothing more (no tuner/reconciler/extractor payload, TK-47).
DREAM_REPORT_KIND = "wombat.dream_report"

# DreamOutcomeStage's committed output kind — a small system-provenance count artifact (TK-175),
# following DREAM_REPORT_KIND's own contentless-proof idiom: no claim payloads ride this artifact,
# only counts (the claims themselves are the durable record, written straight to the entity KG).
DREAM_OUTCOME_REPORT_KIND = "wombat.dream_outcome_report"

# Q-90 v1 shape: a generous per-query ceiling. wombat is single-user, nightly-cadence — the real
# per-event-class/per-item corpus is bounded by one day's drain volume, nowhere near this ceiling.
_CLAIMS_LIMIT = 500


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
    inference is admitted only in TK-47's later sweepers, not this scaffold). Provenance is
    ``source="system"`` (the as-built control-plane convention, mirrors ``brief_trigger_artifact``).
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


def build_dream_pathway(outcome: Stage, terminal: Stage | None = None) -> StageGraph:
    """Assemble the ``wombat.dream`` ``StageGraph``, entered at ``outcome.name`` (Q-90 end-state:
    ``dream_outcome`` -> ``dream_run``, TK-175).

    ``outcome`` is REQUIRED and supplied by the caller (mirrors ``build_brief_pathway``'s
    all-stages-injected convention) — production callers pass a ``DreamOutcomeStage`` built with
    its real ``entity_kg``/``labeler``/``user_id`` collaborators; this module never constructs
    those (no bootstrap import, pure graph assembly). ``terminal`` KEEPS the TK-46 injectable-stage
    seam: it defaults to ``DreamScaffoldStage()`` — since ``DreamOutcomeStage.transitions`` names
    the literal ``"dream_run"`` target, a substituted terminal double (e.g. an always-raising
    stage, AC2's off-path error-isolation proof) must keep that SAME name to be reachable.
    """
    dream_terminal = terminal if terminal is not None else DreamScaffoldStage()
    return StageGraph([outcome, dream_terminal], entry=outcome.name)


def dream_trigger_artifact(now: datetime) -> Artifact:
    """The initial drive's input for ``wombat.dream`` — a system-provenanced, contentless trigger
    (mirrors ``brief_trigger_artifact``). Neither dream stage reads this artifact's ``data``; it
    only satisfies the engine's ``initial: Artifact`` requirement to start a run.
    """
    return Artifact(
        kind=DREAM_TRIGGER_KIND,
        produced_by="wombat.runtime",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
        data={},
    )


__all__ = [
    "DREAM_OUTCOME_REPORT_KIND",
    "DREAM_PATHWAY_ID",
    "DREAM_REPORT_KIND",
    "DREAM_TRIGGER_KIND",
    "DreamOutcomeStage",
    "DreamScaffoldStage",
    "build_dream_pathway",
    "dream_trigger_artifact",
]
