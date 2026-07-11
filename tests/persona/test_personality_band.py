"""TK-215 — the personality_band gate seam (DEC-37(a)/Q-107(a), EP-33).

Covers the two ACs anchored to real persona/param plumbing (rather than the pure-function/
Gate-fake ACs in ``tests/gate/test_trigger.py``):

  AC1  a params YAML missing the personality_band block fails LOUD, naming the block, at
       ``load_operating_params`` (a tmp_path copy of the packaged file with the block stripped).
  AC4  a ``LivePersona.set`` proactivity change between two scored items changes the SECOND
       item's surfacing decision with no restart -- proven via a ``threshold_fn`` closure over
       a REAL ``LivePersona``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import pytest
import yaml

from wombat.gate.decay import LedgerReset
from wombat.gate.models import GateAction, GateItem, ItemKind
from wombat.gate.pending_set import InMemoryPendingJournal, PendingSet
from wombat.gate.pipeline import Gate
from wombat.gate.trigger import effective_urgency_threshold
from wombat.params import OperatingParamsError, load_operating_params
from wombat.persona.live import LivePersona
from wombat.persona.matrix import DEFAULT_MATRIX, PersonaMatrix, Proactivity
from wombat.rating.params import EventClass, RatingParams


class _NoOpRollover:
    """A ``DayRolloverProtocol`` double that never fires (TK-28, Q-73) -- out of scope here."""

    def check(self) -> LedgerReset | None:
        return None


def _item(item_id: str) -> GateItem:
    return GateItem(
        item_id=item_id,
        item_kind=ItemKind.GENERIC,
        created_at=0.0,
        payload={"is_timed": False, "sender_class": "vip"},
    )


@dataclass
class _FakeUserModel:
    rating_params: RatingParams
    event_class: EventClass = EventClass.GENERIC

    def resolve_event_class(self, item: GateItem) -> EventClass:
        return self.event_class

    async def ratings_for(self, item: GateItem) -> RatingParams:
        return self.rating_params


@dataclass
class _FakeCeiling:
    allowed: bool = True
    recorded: list[EventClass] = field(default_factory=list)

    def allow(self, event_class: EventClass) -> bool:
        return self.allowed

    def record(self, event_class: EventClass) -> None:
        self.recorded.append(event_class)


# ------------------------------------------------------------------------------------- AC1


def test_load_operating_params_personality_band_missing_raises(tmp_path: Path) -> None:
    """A tmp_path copy of the packaged YAML with personality_band stripped fails LOUD, naming
    the missing block (AC1). Check: ``pytest -k personality_band_missing``."""
    packaged = Path(str(resources.files("wombat").joinpath("wombat_params.yaml")))
    mapping = yaml.safe_load(packaged.read_text(encoding="utf-8"))
    assert "personality_band" in mapping  # sanity: the packaged file DOES carry the block
    del mapping["personality_band"]

    stripped = tmp_path / "wombat_params.yaml"
    stripped.write_text(yaml.safe_dump(mapping), encoding="utf-8")

    with pytest.raises(OperatingParamsError, match="personality_band"):
        load_operating_params(stripped)


# ------------------------------------------------------------------------------------- AC4


async def test_ac4_live_persona_proactivity_flip_changes_the_next_scored_item_no_restart() -> None:
    """A ``LivePersona.set`` proactivity change lands on the very next scored item -- no
    restart, no new Gate (AC4). ``threshold_fn`` closes over ``live_persona.matrix
    .proactivity`` + the shipped ``personality_band`` and is evaluated fresh per item."""
    band = load_operating_params().personality_band  # shipped: floor=0.60, cap=0.95
    base_threshold = 0.75  # eff(BALANCED) == 0.75 exactly; eff(MINIMAL) == 0.85 (clamped-safe)

    live_persona = LivePersona(DEFAULT_MATRIX, "Steward")  # store-less (TK-243), fully in-memory
    assert live_persona.matrix.proactivity is Proactivity.BALANCED  # DEFAULT_MATRIX

    # A CONSTANT urgency=0.80 for every item (gain=0.0) -- the only thing that changes between
    # the two pipeline() calls below is the live proactivity level.
    rating_params = RatingParams(urgency_base=0.80, urgency_gain=0.0, load_base=0.0, load_gain=0.0)
    pending_set = PendingSet(journal=InMemoryPendingJournal(), max_pending=10)
    gate = Gate(
        user_model=_FakeUserModel(rating_params=rating_params),
        pending_set=pending_set,
        ceiling=_FakeCeiling(allowed=True),
        urgency_threshold=base_threshold,
        load_flush_threshold=10.0,
        flush_min_age_seconds=100.0,
        decay_ttl_seconds=float("inf"),
        day_rollover=_NoOpRollover(),
        clock=lambda: 1000.0,
        threshold_fn=lambda: effective_urgency_threshold(
            base_threshold, live_persona.matrix.proactivity, band
        ),
    )

    # BALANCED: eff == 0.75 exactly -> urgency 0.80 > 0.75 -> worthy -> SURFACE_IMMEDIATE.
    first = await gate.pipeline([_item("first")])
    assert first.action is GateAction.SURFACE_IMMEDIATE

    # Flip proactivity via the SAME LivePersona instance -- no restart, no new Gate.
    live_persona.set(
        PersonaMatrix(
            brevity=DEFAULT_MATRIX.brevity,
            warmth=DEFAULT_MATRIX.warmth,
            directness=DEFAULT_MATRIX.directness,
            humor=DEFAULT_MATRIX.humor,
            proactivity=Proactivity.MINIMAL,
        )
    )

    # MINIMAL: eff == 0.75 + 0.10 == 0.85 -> urgency 0.80 is NOT > 0.85 -> HOLD.
    second = await gate.pipeline([_item("second")])
    assert second.action is GateAction.HOLD
