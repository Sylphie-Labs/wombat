"""wombat.user_model.outcome_labeler — OutcomeLabeler, the OUTCOME_* claim labeler (TK-45, EP-12).

WHAT THIS DOES: two writes over the widened ``ObservationWriter`` (TK-45's other half).
``stamp_pending`` writes an ``OUTCOME_PENDING`` claim when an item is resolved but its outcome
isn't known yet. ``label_terminal`` later supersedes that pending claim with the terminal
``OUTCOME_LOAD_BEARING``/``OUTCOME_REGRETTED``/``OUTCOME_IGNORED`` claim TK-50's
``OutcomeSignal`` names, via ``ObservationWriter.record_superseding`` (invalidate-then-assert,
Q-90's as-built supersede).

DEPENDENCY DIRECTION (deliberate, TK-45): this module imports ``Outcome``/``OutcomeSignal`` from
``wombat.user_model.outcome_inference`` and ``Claim``/``ClaimPredicate`` from
``wombat.user_model.claims``. ``outcome_inference.py`` must NEVER import this module — it is the
pure, off-path inference engine (TK-50); this labeler is the write seam that consumes its output.

CLAIM SHAPE (Q-90, FIXED): subject = ``event_class.value`` (the enumerable entity — the
rating-params precedent, TK-41); predicate = the ``ClaimPredicate.OUTCOME_*`` member; value = a
JSON-native payload string (Q-49 convention). The pending claim's payload carries
``{item_ref, disposition, resolved_at}``; the terminal claim's payload carries
``{item_ref, outcome, source, rule_name, resolved_at}`` (mapped from the ``OutcomeSignal`` TK-50
hands in — ``label_terminal`` has no disposition input, so the terminal payload never repeats
it). Per-item binding rides ``value.item_ref``; per-class enumeration rides
``claims_about(event_class.value)`` (AC5).

OUT OF SCOPE (this ticket): no tuner/corpus query (TK-49); no hot-path or dream call site
(TK-176/TK-175 wire it); no outcome DECISIONS (TK-50 produces signals, this labeler writes
exactly what it is handed); no motive fields, ever (NG-1/CON-6).
"""

from __future__ import annotations

import json
from datetime import datetime

from wombat.rating.params import EventClass
from wombat.user_model.claims import Claim, ClaimPredicate
from wombat.user_model.observation_writer import ObservationWriter
from wombat.user_model.outcome_inference import ItemDisposition, Outcome, OutcomeSignal

# The CLOSED Outcome -> ClaimPredicate mapping (Q-90). Every Outcome member has an entry, so a
# genuine Outcome value can never miss the map — only a non-Outcome duck-typed stand-in can
# (guarded explicitly in label_terminal, AC4).
_OUTCOME_TO_PREDICATE: dict[Outcome, ClaimPredicate] = {
    Outcome.LOAD_BEARING: ClaimPredicate.OUTCOME_LOAD_BEARING,
    Outcome.REGRETTED: ClaimPredicate.OUTCOME_REGRETTED,
    Outcome.IGNORED: ClaimPredicate.OUTCOME_IGNORED,
}


class OutcomeLabeler:
    """Writes ``OUTCOME_*`` claims for one item's lifecycle: pending, then terminal (TK-45).

    Keyword-injected deps only (TK-42/TK-44 precedent): ``writer`` is the ``ObservationWriter``
    this labeler is the sole caller of for outcome claims. No entity-KG/scope construction here —
    ``ObservationWriter`` already owns that (S7).
    """

    def __init__(self, *, writer: ObservationWriter) -> None:
        self._writer = writer

    async def stamp_pending(
        self,
        *,
        item_ref: str,
        event_class: EventClass,
        disposition: ItemDisposition,
        resolved_at: datetime,
        event_id: str | None = None,
    ) -> str:
        """Write one ``OUTCOME_PENDING`` claim for ``item_ref`` (AC1). Returns the claim id —
        callers hold this id to pass as ``pending_claim_id`` to ``label_terminal`` later.

        subject = ``event_class.value``; value = JSON ``{item_ref, disposition, resolved_at}``
        (no outcome/source/rule_name yet — the outcome isn't known at this point).
        """
        value = json.dumps(
            {
                "item_ref": item_ref,
                "disposition": disposition,
                "resolved_at": resolved_at.isoformat(),
            }
        )
        claim = Claim(
            predicate=ClaimPredicate.OUTCOME_PENDING,
            subject=event_class.value,
            value=value,
            event_id=event_id,
            observed_at=resolved_at,
        )
        return await self._writer.record(claim)

    async def label_terminal(
        self,
        *,
        pending_claim_id: str,
        event_class: EventClass,
        signal: OutcomeSignal,
        resolved_at: datetime,
    ) -> str:
        """Supersede ``pending_claim_id`` with the terminal ``OUTCOME_*`` claim ``signal`` names
        (AC2/AC3). Returns the new claim's id.

        Maps ``signal.outcome`` (``Outcome.LOAD_BEARING``/``REGRETTED``/``IGNORED``) onto
        ``ClaimPredicate.OUTCOME_LOAD_BEARING``/``OUTCOME_REGRETTED``/``OUTCOME_IGNORED`` and
        writes via ``ObservationWriter.record_superseding`` — invalidate-then-assert (Q-90).

        ``signal.outcome`` is re-validated as an ``Outcome`` BEFORE any I/O (``TypeError``
        otherwise, AC4) — defense in depth beyond ``OutcomeSignal.__post_init__``, since a
        duck-typed stand-in can carry a hand-rolled outcome value past a type-only check; the
        writer sees zero calls in that case.
        """
        if not isinstance(signal.outcome, Outcome):
            raise TypeError(
                f"OutcomeLabeler.label_terminal: signal.outcome must be an Outcome, got "
                f"{type(signal.outcome).__name__}: {signal.outcome!r}"
            )
        predicate = _OUTCOME_TO_PREDICATE[signal.outcome]
        value = json.dumps(
            {
                "item_ref": signal.item_ref,
                "outcome": signal.outcome.value,
                "source": signal.source,
                "rule_name": signal.rule_name,
                "resolved_at": resolved_at.isoformat(),
            }
        )
        claim = Claim(
            predicate=predicate,
            subject=event_class.value,
            value=value,
            event_id=None,
            observed_at=resolved_at,
        )
        return await self._writer.record_superseding(claim, supersedes_claim_id=pending_claim_id)


__all__ = ["OutcomeLabeler"]
