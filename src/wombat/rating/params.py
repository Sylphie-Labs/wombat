"""Versioned rating-parameter vocabulary (TK-41, EP-10).

This module is the ONE shared contract for per-event-class rating parameters that
``UserModel.ratings_for()`` returns and the ``RatingTuner`` writes. The gate, the read
seam, and the tuner all speak this vocabulary so none of them re-derives a private set.

Frame: deterministic, model-free (NG-4). This is a *vocabulary leaf* — pure typed
dataclasses with documented defaults and **no I/O** (no Neo4j, no Postgres, no cog-worx
dependency). Misspelled or missing fields are caught by Python/mypy, never silently
defaulted (AC2): the dataclass has no ``**kwargs`` and every field is explicitly typed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from enum import Enum

# Schema version of the rating-parameter vocabulary. Bump on any field add/remove/rename
# so a persisted param node can be reconciled against the code's expectation.
RATING_PARAMS_VERSION = 1


class EventClass(Enum):
    """Closed set of per-event-class identities the rating vocabulary is keyed by.

    The tuner sharpens, and the gate reads, parameters *per event class*. A new event
    class is a deliberate vocabulary change (and a version bump), not a free-form string.
    """

    CALENDAR_CONFLICT = "calendar_conflict"
    MORNING_BRIEF = "morning_brief"
    REFLECTION = "reflection"
    DRAFT_REPLY = "draft_reply"
    GENERIC = "generic"


@dataclass(frozen=True, slots=True)
class RatingParams:
    """Personalized Urgency/Load parameters for one event class.

    Every field is documented and has a default, so ``RatingParams()`` yields the neutral
    baseline. All fields are explicitly typed; constructing with an unknown field name is a
    ``TypeError`` and mypy error (AC2 — no silent defaults).

    Fields:
      ``version``        schema version this instance was built against (RATING_PARAMS_VERSION).
      ``urgency_base``   baseline urgency contribution for the class, in [0.0, 1.0].
      ``urgency_gain``   multiplier applied to the urgency signal, in [0.0, 1.0]; the tuner
                         sharpens this toward LOAD_BEARING outcomes.
      ``load_base``      baseline cognitive-load cost for surfacing the class, in [0.0, 1.0].
      ``load_gain``      multiplier applied to the load signal, in [0.0, 1.0]; the tuner
                         raises this toward IGNORED/REGRETTED outcomes to mute the class.

    These are operating constants, not free knobs: the spike (TK-48) proves the bounds the
    tuner clamps against; TK-13 persists the agreed floor/ceiling.
    """

    version: int = RATING_PARAMS_VERSION
    urgency_base: float = 0.5
    urgency_gain: float = 0.5
    load_base: float = 0.5
    load_gain: float = 0.5

    def with_updates(
        self,
        *,
        urgency_base: float | None = None,
        urgency_gain: float | None = None,
        load_base: float | None = None,
        load_gain: float | None = None,
    ) -> RatingParams:
        """Return a copy with the given fields replaced (frozen-safe, keyword-only).

        Helper for the tuner: produce a new ``RatingParams`` from an old one without
        mutating it. ``version`` is preserved. Pure — no I/O, no side effects.
        """
        return replace(
            self,
            urgency_base=self.urgency_base if urgency_base is None else urgency_base,
            urgency_gain=self.urgency_gain if urgency_gain is None else urgency_gain,
            load_base=self.load_base if load_base is None else load_base,
            load_gain=self.load_gain if load_gain is None else load_gain,
        )


# Documented per-class defaults. A request for a known event class returns a fully-typed
# RatingParams with these values (AC1). Classes absent here fall back to the neutral
# baseline via ``default_params_for``.
_DEFAULTS: dict[EventClass, RatingParams] = {
    EventClass.CALENDAR_CONFLICT: RatingParams(
        urgency_base=0.7, urgency_gain=0.6, load_base=0.4, load_gain=0.5
    ),
    EventClass.MORNING_BRIEF: RatingParams(
        urgency_base=0.5, urgency_gain=0.5, load_base=0.3, load_gain=0.5
    ),
    EventClass.REFLECTION: RatingParams(
        urgency_base=0.3, urgency_gain=0.4, load_base=0.5, load_gain=0.5
    ),
    EventClass.DRAFT_REPLY: RatingParams(
        urgency_base=0.5, urgency_gain=0.5, load_base=0.6, load_gain=0.5
    ),
    EventClass.GENERIC: RatingParams(),
}


def default_params_for(event_class: EventClass) -> RatingParams:
    """Return the documented default ``RatingParams`` for a known event class (AC1).

    Deterministic, model-free, no I/O. Unknown-to-the-defaults-map classes return the
    neutral baseline ``RatingParams()`` so a caller always receives a fully-typed object.
    """
    return _DEFAULTS.get(event_class, RatingParams())


# --- Provisional claim-payload wire contract (TK-42, Q-41 ruling 4) -----------------------
#
# Homed here (not in user_model.py) so the read seam (TK-42) and future writers (TK-44/TK-49)
# share ONE vocabulary for how a ``RatingParams`` is serialized onto a cog-worx claim payload,
# instead of each independently inventing a shape. Claim subject/entity = the ``EventClass``
# value; predicate = ``RATING_CLAIM_PREDICATE``; the payload is this module's JSON wire shape.

RATING_CLAIM_PREDICATE = "rating_params"
"""The claim predicate under which personalized ``RatingParams`` are stored (Q-41 ruling 4)."""


def to_claim_payload(params: RatingParams) -> str:
    """Serialize a ``RatingParams`` to the JSON wire-shape stored on a claim payload.

    Q-41 ruling 4: ``{version, urgency_base, urgency_gain, load_base, load_gain}``. Pure, no I/O.
    """
    return json.dumps(
        {
            "version": params.version,
            "urgency_base": params.urgency_base,
            "urgency_gain": params.urgency_gain,
            "load_base": params.load_base,
            "load_gain": params.load_gain,
        }
    )


def params_from_claim_payload(payload: str) -> RatingParams:
    """Deserialize a claim payload written by :func:`to_claim_payload` back into ``RatingParams``.

    Q-41 ruling 4: malformed JSON or an unknown ``version`` MUST surface (here, as a
    ``ValueError``) rather than being silently coerced, so the read seam
    (``UserModel.ratings_for``) can catch it and fall back to documented defaults + a logged
    warning instead of returning a garbage/half-populated ``RatingParams``.
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed rating-claim payload (not valid JSON): {payload!r}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"malformed rating-claim payload (not a JSON object): {payload!r}")
    version = data.get("version")
    if version != RATING_PARAMS_VERSION:
        raise ValueError(
            f"unknown rating-claim payload version {version!r} "
            f"(expected {RATING_PARAMS_VERSION}): {payload!r}"
        )
    try:
        return RatingParams(
            version=version,
            urgency_base=float(data["urgency_base"]),
            urgency_gain=float(data["urgency_gain"]),
            load_base=float(data["load_base"]),
            load_gain=float(data["load_gain"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"malformed rating-claim payload fields: {payload!r}") from exc
