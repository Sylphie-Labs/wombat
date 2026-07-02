"""Production urgency() and cognitive_load() heuristics over the user-model seam (TK-23, EP-7).

These are deterministic, model-free (NG-4) PURE functions over an item's scoring features plus
a per-event-class ``RatingParams`` (the PRODUCTION rating vocabulary that
``UserModel.ratings_for(item)`` returns — TK-41/EP-10). The TK-22 spike's private
``ScoringParams`` stand-in is GONE: ``RatingParams`` is the SOLE params type, so the gate, the
read seam, and the tuner never re-derive a private vocabulary (the drift TK-41 exists to prevent).

Composition (Q-42, contract v0.31+):

    urgency(item, params)        = clamp01(params.urgency_base + params.urgency_gain * raw_urgency)
        raw_urgency = W_TIME * time_term + W_SENDER * sender_term
    cognitive_load(item, params) = clamp01(params.load_base + params.load_gain * raw_load)
        raw_load    = W_DENSITY * density_term + W_DEPTH * depth_term

The term weights in each raw signal sum to 1, so each raw signal is itself in [0,1].

The FROZEN module-level heuristic constants below (term weights, horizon, saturations, and the
sender-priority table) are part of each function's DEFINITION, not a third input — 'solely a
function of payload + params' (AC1) is a PURITY clause, not a no-named-constants clause (Q-42).
Under IDENTITY params ``RatingParams(urgency_base=0.0, urgency_gain=1.0, load_base=0.0,
load_gain=1.0)`` both functions reproduce the spike's raw scores EXACTLY, so the TK-22 spike
tests port unchanged (behavioral-equivalence proof).

Purity (AC1): no I/O, no model call, no randomness, no presence signal, no clock read
(``seconds_to_event`` arrives PRECOMPUTED in ``item.payload`` — the functions never call
``time.time()`` or any clock), and no mutable module state.

Monotonicity (AC2): holding the item fixed except time-to-event, a near-term item (<30 min)
scores STRICTLY higher than a far item (>4 h) whenever ``urgency_gain > 0`` and the far item is
below the 1.0 clamp. A >4h item has zero time pressure, so its raw urgency is at most
``W_SENDER`` (=0.45); for every documented per-class default (and the whole TK-13 tuner clamp
band) ``urgency_base + W_SENDER * urgency_gain < 1.0``, so no clamp saturation masks the gap.
DEGENERATE CASE: ``urgency_gain == 0`` is type-legal but flattens the class score to its base (a
fully muted class), so the strict-monotonicity guarantee is specified over ``gain > 0`` only.
Analogously the guarantee assumes the far item stays below the 1.0 clamp — a base/gain
combination large enough to saturate the far item would mask the difference (the clamp-saturation
caveat).

Payload totality (AC3): a MISSING payload key defaults SILENTLY (sparse items are legitimate — a
non-timed email has no ``seconds_to_event`` and warning on absence would spam). A present-but-
INVALID value (unknown ``sender_class`` string, non-numeric density/depth) falls back to the same
quiet default AND emits a logged WARNING (a data bug must be visible — this warning IS the
hardening; the spike defaulted silently). Neither branch ever raises to the pipeline. The
``ratings_for``-failure fallback (store unreachable / malformed claim) lives on TK-42 and is
already built+verified there; these sync scoring functions never call ``ratings_for``.

Design notes anchored to the contract:

* Presence is OUT of scoring by design (Q-12 / DEC-13). Neither function reads any presence
  signal; presence is a separate gate-level hold applied AFTER scoring (TK-6).
* Sender priority is a CLASS, never an identity. De-identification (DEC-24 / NG-7) happens at
  fixture-build time; this module only ever sees the enum.
"""

from __future__ import annotations

import logging
from enum import Enum

from wombat.gate.models import GateItem
from wombat.rating.params import RatingParams

_log = logging.getLogger(__name__)


class SenderClass(Enum):
    """De-identified sender priority class. Never an email or a name (DEC-24 / NG-7)."""

    VIP = "vip"  # a person whose word the user acts on directly
    KNOWN_HUMAN = "known_human"  # a real correspondent, not high-priority
    TRANSACTIONAL = "transactional"  # receipts/statements a human sometimes must act on
    AUTOMATED = "automated"  # CI/notification/marketing noise; default-hold territory
    SELF = "self"  # the user's own calendar block / self-authored item


# Priority weight per class in [0,1]. Ordered: VIP loudest, automated quietest.
_SENDER_PRIORITY: dict[SenderClass, float] = {
    SenderClass.VIP: 1.0,
    SenderClass.KNOWN_HUMAN: 0.7,
    SenderClass.TRANSACTIONAL: 0.45,
    SenderClass.SELF: 0.4,
    SenderClass.AUTOMATED: 0.1,
}


# --- Frozen heuristic constants (part of the function DEFINITION, not an input — Q-42/AC1) ---
# urgency() term weights. They sum to 1 so raw_urgency stays in [0,1].
W_TIME = 0.55
W_SENDER = 0.45
# The horizon (seconds) beyond which time-to-event contributes nothing to urgency.
TIME_HORIZON_S = 14400.0  # 4 hours
# cognitive_load() term weights. They sum to 1 so raw_load stays in [0,1].
W_DENSITY = 0.6
W_DEPTH = 0.4
# Saturation points: density (meetings in the surrounding window) and thread depth at which the
# respective load term reaches ~1.0.
DENSITY_SATURATION = 6.0
DEPTH_SATURATION = 8.0


def _clamp01(x: float) -> float:
    """Clamp to the closed unit interval. Pure."""
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _time_pressure(seconds_to_event: float, horizon_s: float) -> float:
    """Map time-to-event to [0,1]: 1.0 at/after the deadline, decaying to 0 at the horizon.

    Linear ramp is deliberate (no exponential tuning). A past-due or happening-now item (<=0
    seconds) is maximally time-pressured.
    """
    if seconds_to_event <= 0.0:
        return 1.0
    if seconds_to_event >= horizon_s:
        return 0.0
    return 1.0 - (seconds_to_event / horizon_s)


def _as_float(value: object, *, key: str, item_id: str, default: float) -> float:
    """Coerce a present payload value to float, defaulting+WARNING when it is invalid (AC3).

    A MISSING key never reaches here (callers apply the silent default first); this handles only
    the present-but-INVALID branch, so a bad value is visible in the logs rather than silent.
    """
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        _log.warning(
            "scoring: invalid %r=%r on item %s; using default %s",
            key,
            value,
            item_id,
            default,
        )
        return default


def urgency(item: GateItem, params: RatingParams) -> float:
    """Personalized urgency score in [0,1] from time-to-event and sender priority class.

    ``clamp01(params.urgency_base + params.urgency_gain * raw_urgency)`` where
    ``raw_urgency = W_TIME*time_term + W_SENDER*sender_term`` (Q-42). Reads ONLY ``item.payload``
    features and ``params``. Pure: no presence, no I/O, no model call, no clock, no randomness.

    Expected payload keys (missing -> silent quiet default; invalid -> default + warning):
      * ``is_timed`` (bool): whether the item has a real deadline/start. Default False.
      * ``seconds_to_event`` (float): PRECOMPUTED signed seconds until the event (negative = past
        due). Default ``TIME_HORIZON_S`` (no time pressure).
      * ``sender_class`` (str): a ``SenderClass`` value. Default ``automated`` (quiet).
    """
    is_timed = bool(item.payload.get("is_timed", False))
    if is_timed:
        if "seconds_to_event" in item.payload:
            seconds_to_event = _as_float(
                item.payload["seconds_to_event"],
                key="seconds_to_event",
                item_id=item.item_id,
                default=TIME_HORIZON_S,
            )
        else:
            seconds_to_event = TIME_HORIZON_S
        time_term = _time_pressure(seconds_to_event, TIME_HORIZON_S)
    else:
        time_term = 0.0

    sender_term = _SENDER_PRIORITY[_resolve_sender(item)]

    raw_urgency = W_TIME * time_term + W_SENDER * sender_term
    return _clamp01(params.urgency_base + params.urgency_gain * raw_urgency)


def cognitive_load(item: GateItem, params: RatingParams) -> float:
    """Personalized per-item cognitive-load score in [0,1] from meeting density and thread depth.

    ``clamp01(params.load_base + params.load_gain * raw_load)`` where
    ``raw_load = W_DENSITY*density_term + W_DEPTH*depth_term`` (Q-42). This is the PER-ITEM load
    contribution; the cumulative-load aggregator over the pending set is a separate production
    concern (TK-25), out of scope here. Pure — no I/O, no model, no clock, no randomness.

    Expected payload keys (missing -> silent default 0.0; invalid -> default + warning):
      * ``meeting_density`` (float): count of overlapping/adjacent meetings in the window.
      * ``thread_depth`` (int): number of messages already in the thread.
    """
    if "meeting_density" in item.payload:
        density = _as_float(
            item.payload["meeting_density"],
            key="meeting_density",
            item_id=item.item_id,
            default=0.0,
        )
    else:
        density = 0.0

    if "thread_depth" in item.payload:
        depth = _as_float(
            item.payload["thread_depth"],
            key="thread_depth",
            item_id=item.item_id,
            default=0.0,
        )
    else:
        depth = 0.0

    density_term = _clamp01(density / DENSITY_SATURATION)
    depth_term = _clamp01(depth / DEPTH_SATURATION)

    raw_load = W_DENSITY * density_term + W_DEPTH * depth_term
    return _clamp01(params.load_base + params.load_gain * raw_load)


def _resolve_sender(item: GateItem) -> SenderClass:
    """Resolve the de-identified sender class from the payload (AC3).

    AUTOMATED is the quiet default so an unclassified item leans toward hold (quiet-by-default,
    vision gate). A MISSING key defaults silently; a present-but-INVALID value defaults AND warns.
    """
    if "sender_class" not in item.payload:
        return SenderClass.AUTOMATED
    raw = item.payload["sender_class"]
    try:
        return SenderClass(str(raw))
    except ValueError:
        _log.warning(
            "scoring: invalid sender_class=%r on item %s; using default %s",
            raw,
            item.item_id,
            SenderClass.AUTOMATED.value,
        )
        return SenderClass.AUTOMATED
