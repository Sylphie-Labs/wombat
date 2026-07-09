"""wombat.user_model.outcome_inference — the PURE, off-path deterministic outcome-inference
engine (TK-50, EP-12, RISK-7/ISS-6, Q-88 ruling).

WHY: a wombat that is read-only on calendar and never auto-sends Gmail would otherwise give the
RatingTuner (TK-49/EP-14) a near-constant IGNORED-on-timeout stream — no gradient to train on.
This engine closes that gap with a small, DETERMINISTIC rule set inferring WHETHER a surfacing
was load-bearing from already-observable local state deltas — never WHY (NG-1: no motive-shaped
input, no theory of intent).

PURE ENGINE, INJECTED INPUTS (Q-88 ruling 1): every input arrives as a plain, frozen, JSON-native
argument — ``CalendarSnapshotDelta``, ``DraftFate`` (fixture-only for now; TK-79's dispatch trail
is the future live producer), ``ItemResolution``, and TK-51's ``FeedbackSignal`` (imported, not
redefined). There is NO store read, NO live wire, and NO clock read here — live collection and
invocation belong to TK-175; this module only defines the rules and folds the inputs it is
handed.

CLOSED VOCABULARY, DEFINED HERE: ``Outcome`` is a closed three-member enum
(``LOAD_BEARING``/``REGRETTED``/``IGNORED``). TK-45 later maps these onto the existing
``ClaimPredicate.OUTCOME_*`` members (this module does NOT import ``wombat.user_model.claims`` —
that would invert TK-45's dependency direction).

RULES (deterministic, closed set, module-level):
  (a) ``RULE_FLAGGED_CONFLICT_DISAPPEARED`` — a previously-flagged calendar conflict's event ids
      are all gone from the latest read-only snapshot -> ``LOAD_BEARING``.
  (b) ``RULE_DRAFT_DELETED_UNSENT`` — a drafted reply the user later deleted unsent ->
      ``REGRETTED``.
  (c) ``RULE_IGNORED_DEFAULT`` — no feedback and no inferable downstream change -> ``IGNORED``,
      the default (one reachable outcome among three, not the only one).

FOLD PRECEDENCE (FIXED, Q-88 ruling 3): explicit ``FeedbackSignal`` (useful -> ``LOAD_BEARING``,
not_useful -> ``REGRETTED``, ``source="feedback"``) BEATS any inference rule BEATS the
``IGNORED`` default. Exactly ONE ``OutcomeSignal`` is emitted per ``item_ref``.

STRUCTURAL OFF-PATH + NO-LLM (Q-66/Q-84 precedent, applied a third time): this module imports NO
drain-spine/compose/model module (no ``openai``/``httpx``/``requests``/``cogworx.model``/
``wombat.compose``/``wombat.stages``), and no drain-spine module may import this one — enforced
by ``tests/user_model/test_outcome_inference.py`` (the ``test_triage.py``
``_DRAIN_SPINE_PATHS`` pattern, both directions).

OUT OF SCOPE: writing the claim (TK-45); the interactive feedback channel + its input source
(TK-51, done); live collection, drain diversion, boot/dream wiring (TK-175); tuner math (TK-49);
any calendar/Gmail write; any motive-shaped input or rule (NG-1).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from wombat.user_model.feedback_source import FeedbackSignal

# --- the closed outcome vocabulary (defined here; TK-45 maps onto ClaimPredicate.OUTCOME_*) ----


class Outcome(StrEnum):
    """The CLOSED set of outcomes this engine may emit. Behavior/result only (NG-1: never a
    theory of WHY) — TK-45 maps these onto the existing ``ClaimPredicate.OUTCOME_*`` members;
    this module does not import ``wombat.user_model.claims``."""

    LOAD_BEARING = "load_bearing"
    REGRETTED = "regretted"
    IGNORED = "ignored"


OutcomeSource = Literal["feedback", "inferred"]

# --- provenance-bearing rule identifiers (AC1) — the closed set of inference rules -------------

RULE_FLAGGED_CONFLICT_DISAPPEARED = "flagged_conflict_disappeared"
RULE_DRAFT_DELETED_UNSENT = "draft_deleted_unsent"
RULE_IGNORED_DEFAULT = "ignored_default"
RULE_EXPLICIT_FEEDBACK_USEFUL = "explicit_feedback_useful"
RULE_EXPLICIT_FEEDBACK_NOT_USEFUL = "explicit_feedback_not_useful"


def _validate_source(value: str) -> OutcomeSource:
    """The one place a raw string is checked against the closed source vocabulary."""
    if value == "feedback":
        return "feedback"
    if value == "inferred":
        return "inferred"
    raise ValueError(f"OutcomeSignal: source must be 'feedback' or 'inferred', got {value!r}")


# --- injected input records (Q-88 ruling 1) -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class CalendarSnapshotDelta:
    """A previously-flagged calendar conflict's event ids, and whether those events still
    appear in the LATEST read-only calendar snapshot. ``item_ref`` ties this delta to the
    surfaced item whose ``ItemResolution`` it corresponds to. No store read, no clock read —
    both snapshots (flagged-at-surfacing and latest) are supplied by the caller."""

    item_ref: str
    flagged_event_ids: tuple[str, ...]
    event_ids_in_latest_snapshot: tuple[str, ...]

    def all_flagged_events_gone(self) -> bool:
        """True iff none of the previously-flagged conflicting event ids appear in the latest
        snapshot — the ``flagged-conflict-disappeared`` rule's precondition."""
        return not any(
            event_id in self.event_ids_in_latest_snapshot for event_id in self.flagged_event_ids
        )


@dataclass(frozen=True, slots=True)
class DraftFate:
    """Whether a drafted reply for ``item_ref`` was later deleted unsent (fixture-only shape for
    now; TK-79's dispatch trail is the future live producer)."""

    item_ref: str
    draft_created: bool
    deleted_unsent: bool


ItemDisposition = Literal["surfaced", "held"]


def _validate_disposition(value: str) -> ItemDisposition:
    """The one place a raw string is checked against the closed disposition vocabulary."""
    if value == "surfaced":
        return "surfaced"
    if value == "held":
        return "held"
    raise ValueError(f"ItemResolution: disposition must be 'surfaced' or 'held', got {value!r}")


@dataclass(frozen=True, slots=True)
class ItemResolution:
    """One resolved item the nightly pass considers: its gate disposition, when it resolved
    (tz-aware — resolution instants ride the record, this engine never reads the clock), and
    whether its outcome TTL has expired."""

    item_ref: str
    disposition: ItemDisposition
    resolved_at: datetime
    ttl_expired: bool

    def __post_init__(self) -> None:
        _validate_disposition(self.disposition)
        if self.resolved_at.tzinfo is None:
            raise ValueError(f"ItemResolution: resolved_at is naive (must be aware): "
                              f"{self.resolved_at!r}")


# --- output --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutcomeSignal:
    """The engine's single per-item output: an ``Outcome`` plus its provenance — ``source``
    (closed ``feedback``/``inferred``) and ``rule_name`` (the identifier of the rule that fired,
    AC1)."""

    item_ref: str
    outcome: Outcome
    source: OutcomeSource
    rule_name: str

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, Outcome):
            raise TypeError(
                f"OutcomeSignal.outcome must be an Outcome, got {type(self.outcome).__name__}: "
                f"{self.outcome!r}"
            )
        _validate_source(self.source)
        if not self.rule_name:
            raise ValueError("OutcomeSignal: rule_name must be non-empty")


# --- the engine ------------------------------------------------------------------------------


def infer_outcomes(
    resolutions: Sequence[ItemResolution],
    *,
    feedback: Sequence[FeedbackSignal] = (),
    calendar_deltas: Sequence[CalendarSnapshotDelta] = (),
    draft_fates: Sequence[DraftFate] = (),
) -> tuple[OutcomeSignal, ...]:
    """Fold every injected input into exactly one ``OutcomeSignal`` per ``item_ref`` in
    ``resolutions`` (Q-88 ruling 3). Pure function: no I/O, no clock read, no store read —
    deterministic across repeated calls with identical input.

    Precedence (FIXED): explicit feedback beats inference beats the ``IGNORED`` default.
    """
    feedback_by_item: dict[str, FeedbackSignal] = {signal.item_ref: signal for signal in feedback}
    calendar_delta_by_item: dict[str, CalendarSnapshotDelta] = {
        delta.item_ref: delta for delta in calendar_deltas
    }
    draft_fate_by_item: dict[str, DraftFate] = {fate.item_ref: fate for fate in draft_fates}

    signals: list[OutcomeSignal] = []
    for resolution in resolutions:
        signals.append(
            _infer_one(
                resolution,
                feedback_by_item.get(resolution.item_ref),
                calendar_delta_by_item.get(resolution.item_ref),
                draft_fate_by_item.get(resolution.item_ref),
            )
        )
    return tuple(signals)


def _infer_one(
    resolution: ItemResolution,
    feedback: FeedbackSignal | None,
    calendar_delta: CalendarSnapshotDelta | None,
    draft_fate: DraftFate | None,
) -> OutcomeSignal:
    """Apply the fixed fold precedence for a single item: explicit feedback beats inference
    beats the ``IGNORED`` default (Q-88 ruling 3)."""
    if feedback is not None:
        if feedback.response == "useful":
            return OutcomeSignal(
                item_ref=resolution.item_ref,
                outcome=Outcome.LOAD_BEARING,
                source="feedback",
                rule_name=RULE_EXPLICIT_FEEDBACK_USEFUL,
            )
        return OutcomeSignal(
            item_ref=resolution.item_ref,
            outcome=Outcome.REGRETTED,
            source="feedback",
            rule_name=RULE_EXPLICIT_FEEDBACK_NOT_USEFUL,
        )

    if calendar_delta is not None and calendar_delta.all_flagged_events_gone():
        return OutcomeSignal(
            item_ref=resolution.item_ref,
            outcome=Outcome.LOAD_BEARING,
            source="inferred",
            rule_name=RULE_FLAGGED_CONFLICT_DISAPPEARED,
        )

    if draft_fate is not None and draft_fate.deleted_unsent:
        return OutcomeSignal(
            item_ref=resolution.item_ref,
            outcome=Outcome.REGRETTED,
            source="inferred",
            rule_name=RULE_DRAFT_DELETED_UNSENT,
        )

    return OutcomeSignal(
        item_ref=resolution.item_ref,
        outcome=Outcome.IGNORED,
        source="inferred",
        rule_name=RULE_IGNORED_DEFAULT,
    )


__all__ = [
    "RULE_DRAFT_DELETED_UNSENT",
    "RULE_EXPLICIT_FEEDBACK_NOT_USEFUL",
    "RULE_EXPLICIT_FEEDBACK_USEFUL",
    "RULE_FLAGGED_CONFLICT_DISAPPEARED",
    "RULE_IGNORED_DEFAULT",
    "CalendarSnapshotDelta",
    "DraftFate",
    "ItemDisposition",
    "ItemResolution",
    "Outcome",
    "OutcomeSignal",
    "OutcomeSource",
    "infer_outcomes",
]
