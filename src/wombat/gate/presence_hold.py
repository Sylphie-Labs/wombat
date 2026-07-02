"""The canonical presence-hold predicate (TK-11, hardened from the TK-4 spike, Q-54).

PURE, load-bearing: the ONE definition TK-6's ``stub_evaluate`` and the production gate
(TK-27) both call. No clock read, no I/O — every bound is an explicit, keyword-required
argument (NO baked defaults; the spike's defaults were the drift risk this hardening
retires — the documented defaults live in the versioned ``wombat_params.yaml``, TK-13).

TWO-LAYER STALENESS (Q-49 reconciled): ``wombat.sources.presence.make_presence_provider``
is the PRIMARY runtime guard — it degrades a stale snapshot to unknown/confidence 0.0 at
provision time, before the gate ever sees it. This predicate keeps its OWN staleness check
against an explicit ``now`` as DEFENSE IN DEPTH: the silence guarantee must hold even if a
provider bug lets a stale snapshot through un-degraded.
"""

from __future__ import annotations

from wombat.sources.presence import PresenceSnapshot, PresenceState


def presence_hold(
    snapshot: PresenceSnapshot | None,
    now: float,
    *,
    staleness_ceiling_s: float,
    confidence_floor: float,
) -> bool:
    """Return ``True`` (HOLD / do-not-interrupt) unless it is safe to surface.

    HOLD (returns ``True``) if ANY of:
      * ``snapshot is None``
      * ``snapshot.state is PresenceState.UNKNOWN``
      * ``snapshot.is_stale(now, staleness_ceiling_s)`` (Layer 2 defense-in-depth)
      * ``snapshot.confidence < confidence_floor``
      * ``snapshot.state is not PresenceState.ACTIVE`` (covers IDLE and AWAY identically)

    Only a fresh, confident, ACTIVE snapshot returns ``False`` (surface permitted).
    """
    if snapshot is None:
        return True
    if snapshot.state is PresenceState.UNKNOWN:
        return True
    if snapshot.is_stale(now, staleness_ceiling_s):
        return True
    if snapshot.confidence < confidence_floor:
        return True
    return snapshot.state is not PresenceState.ACTIVE


__all__ = ["presence_hold"]
