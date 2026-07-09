"""RatingTuner — the nightly, deterministic, LLM-free rating-parameter tuner (TK-49, EP-14,
Q-91).

Closes the adaptation chain the earlier EP-14/EP-12 tickets built: TK-45/TK-176 write terminal
``OUTCOME_*`` claims into the user scope, TK-42's ``UserModel.ratings_for`` reads personalized
``RatingParams`` back out for the gate — this module is the ONE place that WRITES an updated
``RatingParams`` claim, once per night, per ``EventClass``, from the terminal-outcome corpus. NO
gate/pipeline code changes here (out of scope): the gate re-reads params on its next drive via the
as-built ``UserModel.ratings_for`` seam, so a tuned parameter takes effect on the NEXT scored item,
never mid-flight.

MATH (TK-48 spike, ``wombat.rating.tuner_sim``, reproduced VERBATIM — that module stays a
throwaway spike, never imported by production code): per event class, fold the in-window terminal
outcome corpus into ``net_signal = (load_bearing - (ignored + regretted)) / total`` in
``[-1.0, 1.0]``; an empty corpus emits NO write for that class (AC5 — a no-corpus night is a true
no-op, not a zero-delta write). The raw proportional delta (``gain * net_signal``) is clamped to
``+/- delta_bound`` BEFORE it is applied, then the resulting ``urgency_base``/``load_base`` are
clamped to ``[clamp_floor, clamp_ceiling]`` — the two-stage bound the spike proved stable.
``urgency_gain``/``load_gain`` are NOT tuned in v1 (out of scope, spike shape). ALL FIVE bound
constants (``clamp_floor``/``clamp_ceiling``/``delta_bound``/``gain``/``surfacing_ceiling_per_day``)
are read from the injected ``OperatingParams.rating_tuner`` block (``wombat.params.
RatingTunerBounds``) — NEVER inlined here, so the LOCKED TK-48 joint-block values live in exactly
one place (``wombat_params.yaml``).

READ DISCIPLINE (Q-41, mirrored from ``UserModel.ratings_for``): the current parameter set for an
event class is the newest ACTIVE ``rating_params`` claim, read via ``claims_about`` + ``wombat.
rating.params.params_from_claim_payload``; absent or malformed, this falls back to ``default_
params_for`` and logs a warning — the tuner never crashes a night's pass over one bad claim.

WRITE SEAM: every update is written via the SAME ``ObservationWriter.record_rating_params`` the
read seam already round-trips against (Q-41 ruling 4 — no second wire), widened (additively) to
carry a per-write ``SourceDeclaration`` so a tuned claim's provenance names this tuner and the
night it ran, distinct from any other rating-params writer.

OUT OF SCOPE (this ticket): no LLM call (NG-4 intact — pure arithmetic over already-written
claims); no gate/pipeline edit; no runtime injection of a tuned parameter (the gate re-reads next
drive); no change to the TK-48 joint-block VALUES themselves (only ``wombat_params.yaml`` owns
those); no tuning of ``urgency_gain``/``load_gain``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from cogworx.knowledge.source_registry import SourceDeclaration
from cogworx.substrate.entity_kg import EntityKG

from wombat.params import OperatingParams
from wombat.rating.params import (
    RATING_CLAIM_PREDICATE,
    EventClass,
    RatingParams,
    default_params_for,
    params_from_claim_payload,
)
from wombat.user_model.claims import ClaimPredicate
from wombat.user_model.observation_writer import ObservationWriter

logger = logging.getLogger(__name__)

# The recall window this tuner folds terminal outcomes over: the most recent N nights, anchored at
# the ``now`` handed to ``tune()``. Structural — NOT part of the TK-48 joint bound block (that
# block governs the CLAMP shape; this governs how much corpus history feeds the signal).
RECALL_WINDOW_NIGHTS = 7

# A generous per-query ceiling (mirrors ``dream_pathway.py``'s own ``_CLAIMS_LIMIT`` reasoning):
# wombat is single-user, nightly-cadence — one event class's real terminal-outcome corpus is
# nowhere near this ceiling.
_CLAIMS_LIMIT = 500

# The closed set of terminal outcome predicates this tuner's corpus read counts (Q-90's own
# ``DreamOutcomeStage`` writes exactly these three via ``OutcomeLabeler.label_terminal``).
_TERMINAL_PREDICATES = (
    ClaimPredicate.OUTCOME_LOAD_BEARING,
    ClaimPredicate.OUTCOME_REGRETTED,
    ClaimPredicate.OUTCOME_IGNORED,
)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


@dataclass(frozen=True, slots=True)
class _OutcomeCounts:
    """One event class's in-window terminal-outcome histogram (TK-48 spike shape, reproduced)."""

    load_bearing: int = 0
    ignored: int = 0
    regretted: int = 0

    @property
    def total(self) -> int:
        return self.load_bearing + self.ignored + self.regretted

    def net_signal(self) -> float:
        """Net outcome signal in ``[-1.0, 1.0]``; callers must not call this on an empty corpus
        (AC5 — the empty-corpus case is a NO-WRITE short circuit, never a zero-signal write)."""
        return (self.load_bearing - (self.ignored + self.regretted)) / self.total


class RatingTuner:
    """The nightly bounded rating-parameter tuner (TK-49, EP-14, Q-91).

    Keyword-injected collaborators only (TK-42/TK-44/TK-45/TK-175 precedent): ``entity_kg`` is the
    raw cog-worx ``EntityKG`` Protocol (the SAME shared user-scope instance the read/write seams
    already share); ``writer`` is the ``ObservationWriter`` this tuner is the sole rating-params
    caller of tonight; ``params`` is the loaded ``OperatingParams`` (the TK-48 joint bound block
    lives at ``params.rating_tuner``); ``user_id`` forms the ``user:<user_id>`` scope every read is
    restricted to; ``clock`` is an injected wall-clock collaborator (DI-consistency with this
    module's sibling seams) — ``tune()`` itself always receives its operating ``now`` explicitly
    from the caller (``DreamTuneStage.run`` passes ``ctx.clock()``), so a night's window/provenance
    are anchored on that ONE explicit value, never on a second, independently-read clock.
    """

    def __init__(
        self,
        *,
        entity_kg: EntityKG,
        writer: ObservationWriter,
        params: OperatingParams,
        user_id: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._entity_kg = entity_kg
        self._writer = writer
        self._params = params
        self._user_id = user_id
        self._scope = f"user:{user_id}"
        self._clock = clock

    async def tune(self, now: datetime) -> None:
        """Run one nightly tuning pass over every ``EventClass`` (AC1-AC5).

        Per class: fold the in-window terminal-outcome corpus into a net signal (AC5 — an empty
        corpus writes nothing for that class), compute the bounded delta, clamp the updated
        ``urgency_base``/``load_base`` into the configured band (AC2/AC3), and write the result via
        ``ObservationWriter.record_rating_params`` under a provenance ref naming this tuner and
        tonight's date. A per-class failure (a claims-read error, or a malformed corpus/current-
        params claim) is caught, logged LOUD, and skipped — one bad class never kills the rest of
        the night's pass.
        """
        night_date = now.date().isoformat()
        bounds = self._params.rating_tuner
        source = SourceDeclaration(kind="system", ref=f"wombat.rating_tuner:{night_date}")

        for event_class in EventClass:
            try:
                counts = await self._collect_outcome_counts(event_class, now)
                if counts.total == 0:
                    continue  # AC5: no in-window corpus -> zero writer calls for this class

                raw_delta = bounds.gain * counts.net_signal()
                delta = _clamp(raw_delta, -bounds.delta_bound, bounds.delta_bound)

                current = await self._current_params(event_class)
                updated = current.with_updates(
                    urgency_base=_clamp(
                        current.urgency_base + delta, bounds.clamp_floor, bounds.clamp_ceiling
                    ),
                    load_base=_clamp(
                        current.load_base - delta, bounds.clamp_floor, bounds.clamp_ceiling
                    ),
                )

                await self._writer.record_rating_params(event_class, updated, source=source)
            except Exception:
                logger.error(
                    "RatingTuner.tune: tuning pass failed for event_class=%r; skipping this "
                    "class for tonight — its params stay unchanged until the next run",
                    event_class.value,
                    exc_info=True,
                )

    async def _collect_outcome_counts(
        self, event_class: EventClass, now: datetime
    ) -> _OutcomeCounts:
        """Read + window-filter the ACTIVE terminal ``OUTCOME_*`` corpus for ``event_class``.

        Mirrors ``DreamOutcomeStage.run``'s as-built collection pattern: ``claims_about`` scoped to
        ``user:<user_id>``, keep only ACTIVE claims (``valid_to is None``) under a terminal
        predicate, double-parse the ``ObservationWriter`` envelope (``json.loads(claim.payload)
        ['value']`` then ``json.loads`` that) to reach the terminal payload's ``resolved_at``, and
        drop anything outside the last ``RECALL_WINDOW_NIGHTS`` nights. A malformed claim is logged
        loud and skipped, never raised.
        """
        cutoff = now - timedelta(days=RECALL_WINDOW_NIGHTS)
        load_bearing = ignored = regretted = 0

        try:
            scored_claims = await self._entity_kg.claims_about(
                event_class.value, limit=_CLAIMS_LIMIT, scope=self._scope
            )
        except Exception:
            logger.error(
                "RatingTuner._collect_outcome_counts: claims_about failed (event_class=%r); "
                "treating this class as having no in-window corpus tonight",
                event_class.value,
                exc_info=True,
            )
            return _OutcomeCounts()

        for scored_claim in scored_claims:
            claim = scored_claim.claim
            if claim.valid_to is not None:
                continue  # not ACTIVE — superseded/defeated
            if claim.predicate not in {predicate.value for predicate in _TERMINAL_PREDICATES}:
                continue

            try:
                envelope = json.loads(claim.payload)
                value = json.loads(envelope["value"])
                resolved_at = datetime.fromisoformat(value["resolved_at"])
            except Exception:
                logger.error(
                    "RatingTuner._collect_outcome_counts: malformed terminal-outcome claim "
                    "payload (claim_id=%r, event_class=%r); skipping this claim",
                    claim.id,
                    event_class.value,
                    exc_info=True,
                )
                continue

            if resolved_at < cutoff:
                continue  # outside the recall window

            if claim.predicate == ClaimPredicate.OUTCOME_LOAD_BEARING.value:
                load_bearing += 1
            elif claim.predicate == ClaimPredicate.OUTCOME_IGNORED.value:
                ignored += 1
            elif claim.predicate == ClaimPredicate.OUTCOME_REGRETTED.value:
                regretted += 1

        return _OutcomeCounts(load_bearing=load_bearing, ignored=ignored, regretted=regretted)

    async def _current_params(self, event_class: EventClass) -> RatingParams:
        """The Q-41 read discipline, mirrored from ``UserModel.ratings_for``: the newest ACTIVE
        ``rating_params`` claim for ``event_class``, or ``default_params_for`` (+ a warning) when
        absent, unreadable, or malformed."""
        try:
            scored_claims = await self._entity_kg.claims_about(event_class.value, scope=self._scope)
        except Exception:
            logger.warning(
                "RatingTuner._current_params: entity-KG read failed (event_class=%r); falling "
                "back to defaults",
                event_class.value,
                exc_info=True,
            )
            return default_params_for(event_class)

        rating_claims = [
            scored_claim
            for scored_claim in scored_claims
            if scored_claim.claim.valid_to is None
            and scored_claim.claim.predicate == RATING_CLAIM_PREDICATE
        ]
        if not rating_claims:
            return default_params_for(event_class)

        try:
            return params_from_claim_payload(rating_claims[0].claim.payload)
        except ValueError:
            logger.warning(
                "RatingTuner._current_params: malformed rating-claim payload (event_class=%r); "
                "falling back to defaults",
                event_class.value,
                exc_info=True,
            )
            return default_params_for(event_class)


__all__ = ["RECALL_WINDOW_NIGHTS", "RatingTuner"]
