"""wombat.user_model.claims — the closed, motive-free claim-predicate vocabulary for the PA
empirical user model (TK-43, CON-6/NG-1).

THE SCHEMA WALL: ``ClaimPredicate`` is a CLOSED ``StrEnum`` of behavior/outcome predicates
ONLY. There is no ``MOTIVE_*``, ``INTENT_*``, or "why" member, and there never can be one added
casually — the vocabulary is a deliberate, reviewed edit to this module, not a free-form string
a caller can invent. ``Claim.predicate`` is typed as the enum (not ``str``), so a hand-rolled
string predicate is a mypy error at the call site AND (``__post_init__``) a ``TypeError`` at
runtime — the wall holds even against code that ignores type-checking.

ONE VOCABULARY, NO DRIFT: ``ClaimPredicate.RATING_PARAMS`` is the SAME wire value as the
as-built ``wombat.rating.params.RATING_CLAIM_PREDICATE`` (TK-41/Q-41 ruling 4). This module does
not import ``rating.params`` (would invert TK-41's dependency direction); instead
``tests/user_model/test_claims.py`` asserts the two literal values never drift apart.

``ClaimPredicate.PRODUCTIVITY_WINDOW`` (TK-112, EP-21, Q-99c) is a deliberate, reviewed addition
to this closed vocabulary — a pure behavior aggregate (a nightly list of detected productivity
windows over the TK-111 event log), never a motive/why signal. It carries the same schema-wall
guarantees as every other member: adding it here is the one place this vocabulary widens; no
caller can invent it as a raw string.

FRAME: pure Python, no I/O, no cog-worx import (this is wombat's own vocabulary leaf, not a read
or write seam — TK-44 owns the writer). Deliberately out of scope here: no Neo4j/EntityKG I/O,
no outcome-labeling logic (EP-12), no supersede semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ClaimPredicate(StrEnum):
    """The CLOSED set of claim predicates the PA empirical user model may write or read.

    Behavior and outcome ONLY (CON-6/NG-1: never motive) — this enum is the schema wall.
    Adding a member here is a deliberate vocabulary change, not something a caller can invent
    by passing an arbitrary string.
    """

    BEHAVIOR_OBSERVED = "behavior_observed"
    OUTCOME_PENDING = "outcome_pending"
    OUTCOME_LOAD_BEARING = "outcome_load_bearing"
    OUTCOME_REGRETTED = "outcome_regretted"
    OUTCOME_IGNORED = "outcome_ignored"
    RATING_PARAMS = "rating_params"
    PRODUCTIVITY_WINDOW = "productivity_window"


@dataclass(frozen=True, slots=True)
class Claim:
    """One motive-free claim about a subject's behavior or outcome.

    Fields:
      ``predicate``     the schema wall — a ``ClaimPredicate`` member (never a raw ``str``).
      ``subject``       the entity the claim is about: an ``EventClass`` value or a TK-12
                         ``ItemRef`` idempotency-key string.
      ``value``         JSON-native payload string (Q-49 convention).
      ``event_id``      the originating event id, or ``None`` if the claim isn't tied to one.
      ``observed_at``   tz-AWARE timestamp of observation; a naive value is rejected.

    ``__post_init__`` enforces the schema wall at runtime (``TypeError`` for a non-
    ``ClaimPredicate`` ``predicate``) so a hand-rolled string cannot slip past a caller that
    ignores mypy, and rejects a naive ``observed_at`` (``ValueError``), matching the
    codebase-wide aware-clock convention.
    """

    predicate: ClaimPredicate
    subject: str
    value: str
    event_id: str | None
    observed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.predicate, ClaimPredicate):
            raise TypeError(
                f"Claim.predicate must be a ClaimPredicate, got {type(self.predicate).__name__}: "
                f"{self.predicate!r}"
            )
        if self.observed_at.tzinfo is None:
            raise ValueError(f"Claim: observed_at is naive (must be aware): {self.observed_at!r}")
