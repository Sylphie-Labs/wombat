"""Tests for OutcomeLabeler — OUTCOME_* claims via the widened ObservationWriter (TK-45, EP-12).

  AC1 (pending): ``stamp_pending`` writes exactly one OUTCOME_PENDING claim, subject=event_class
      value, retrievable via ``claims_about``, value JSON carries item_ref/disposition/
      resolved_at; no terminal predicate exists. ``test_ac1_...pending...``.
  AC2 (supersede): ``label_terminal`` invalidates the PENDING claim and the terminal claim is
      active — newest-active read yields the terminal claim. ``test_ac2_...supersede...``.
  AC3 (mapping): all three Outcome members map to the correct ClaimPredicate; source/rule_name
      land in value JSON. ``test_ac3_...mapping...``.
  AC4 (typeerror): a non-Outcome label value raises TypeError BEFORE any I/O (spy writer records
      zero calls). ``test_ac4_...typeerror...``.
  AC5 (queryable): a written claim is queryable by event-class (claims_about subject) AND carries
      item_ref + event_id. ``test_ac5_...queryable...``.

``asyncio_mode = "auto"`` is configured in pyproject.toml (pytest-asyncio), so async test
functions run directly — no manual ``asyncio.run()`` driving needed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cogworx.knowledge.scopes import ScopeRegistry
from cogworx.testing.doubles import InMemoryEntityKG

from wombat.rating.params import EventClass
from wombat.user_model.claims import ClaimPredicate
from wombat.user_model.observation_writer import ObservationWriter
from wombat.user_model.outcome_inference import Outcome, OutcomeSignal
from wombat.user_model.outcome_labeler import OutcomeLabeler

_NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)
_SCOPE = "user:alice"


def _labeler(kg: InMemoryEntityKG) -> OutcomeLabeler:
    writer = ObservationWriter(entity_kg=kg, scope_registry=ScopeRegistry(), user_id="alice")
    return OutcomeLabeler(writer=writer)


# --- AC1: stamp_pending -----------------------------------------------------------------


async def test_ac1_stamp_pending_writes_exactly_one_outcome_pending_claim() -> None:
    kg = InMemoryEntityKG()
    labeler = _labeler(kg)

    claim_id = await labeler.stamp_pending(
        item_ref="item-1",
        event_class=EventClass.CALENDAR_CONFLICT,
        disposition="surfaced",
        resolved_at=_NOW,
        event_id="evt-1",
    )

    scored = await kg.claims_about(EventClass.CALENDAR_CONFLICT.value, scope=_SCOPE)
    assert len(scored) == 1
    stored = scored[0].claim
    assert stored.id == claim_id
    assert stored.subject == EventClass.CALENDAR_CONFLICT.value
    assert stored.predicate == ClaimPredicate.OUTCOME_PENDING.value

    envelope = json.loads(stored.payload)
    value = json.loads(envelope["value"])
    assert value["item_ref"] == "item-1"
    assert value["disposition"] == "surfaced"
    assert value["resolved_at"] == _NOW.isoformat()

    # No terminal predicate exists yet.
    terminal_predicates = {
        ClaimPredicate.OUTCOME_LOAD_BEARING.value,
        ClaimPredicate.OUTCOME_REGRETTED.value,
        ClaimPredicate.OUTCOME_IGNORED.value,
    }
    assert stored.predicate not in terminal_predicates


# --- AC2: label_terminal supersedes -----------------------------------------------------


async def test_ac2_label_terminal_supersedes_pending_claim() -> None:
    kg = InMemoryEntityKG()
    labeler = _labeler(kg)

    pending_id = await labeler.stamp_pending(
        item_ref="item-1",
        event_class=EventClass.CALENDAR_CONFLICT,
        disposition="surfaced",
        resolved_at=_NOW,
        event_id="evt-1",
    )
    signal = OutcomeSignal(
        item_ref="item-1", outcome=Outcome.LOAD_BEARING, source="inferred", rule_name="rule-x"
    )

    terminal_id = await labeler.label_terminal(
        pending_claim_id=pending_id,
        event_class=EventClass.CALENDAR_CONFLICT,
        signal=signal,
        resolved_at=_NOW,
    )

    # The PENDING claim is invalidated (no longer active).
    pending_claim = await kg.get_claim(pending_id)
    assert pending_claim is not None
    assert pending_claim.valid_to is not None

    # The terminal claim is active.
    terminal_claim = await kg.get_claim(terminal_id)
    assert terminal_claim is not None
    assert terminal_claim.valid_to is None
    assert terminal_claim.predicate == ClaimPredicate.OUTCOME_LOAD_BEARING.value

    # Newest-active read: exactly one active claim about this event class, and it's the terminal.
    scored = await kg.claims_about(EventClass.CALENDAR_CONFLICT.value, scope=_SCOPE)
    active = [s.claim for s in scored if s.claim.valid_to is None]
    assert len(active) == 1
    assert active[0].id == terminal_id


# --- AC3: mapping + provenance -----------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "expected_predicate"),
    [
        (Outcome.LOAD_BEARING, ClaimPredicate.OUTCOME_LOAD_BEARING),
        (Outcome.REGRETTED, ClaimPredicate.OUTCOME_REGRETTED),
        (Outcome.IGNORED, ClaimPredicate.OUTCOME_IGNORED),
    ],
)
async def test_ac3_mapping_outcome_to_correct_predicate(
    outcome: Outcome, expected_predicate: ClaimPredicate
) -> None:
    kg = InMemoryEntityKG()
    labeler = _labeler(kg)
    pending_id = await labeler.stamp_pending(
        item_ref="item-1",
        event_class=EventClass.DRAFT_REPLY,
        disposition="surfaced",
        resolved_at=_NOW,
    )
    signal = OutcomeSignal(
        item_ref="item-1", outcome=outcome, source="feedback", rule_name="explicit_feedback"
    )

    terminal_id = await labeler.label_terminal(
        pending_claim_id=pending_id,
        event_class=EventClass.DRAFT_REPLY,
        signal=signal,
        resolved_at=_NOW,
    )

    terminal_claim = await kg.get_claim(terminal_id)
    assert terminal_claim is not None
    assert terminal_claim.predicate == expected_predicate.value

    envelope = json.loads(terminal_claim.payload)
    value = json.loads(envelope["value"])
    assert value["outcome"] == outcome.value
    assert value["source"] == "feedback"
    assert value["rule_name"] == "explicit_feedback"


# --- AC4: non-Outcome label value -> TypeError before any I/O --------------------------


async def test_ac4_typeerror_before_any_write_on_non_outcome_signal() -> None:
    writer = AsyncMock(spec=ObservationWriter)
    labeler = OutcomeLabeler(writer=writer)
    # A duck-typed stand-in with a hand-rolled outcome value: OutcomeSignal.__post_init__ would
    # itself reject this, so this proves OutcomeLabeler's OWN defense-in-depth check.
    bad_signal = SimpleNamespace(
        item_ref="item-1", outcome="bogus", source="inferred", rule_name="rule-x"
    )

    with pytest.raises(TypeError):
        await labeler.label_terminal(
            pending_claim_id="some-claim-id",
            event_class=EventClass.CALENDAR_CONFLICT,
            signal=bad_signal,  # type: ignore[arg-type]
            resolved_at=_NOW,
        )

    writer.record_superseding.assert_not_called()
    writer.record.assert_not_called()


# --- AC5: queryable by event-class AND carries item_ref + event_id ---------------------


async def test_ac5_written_claim_queryable_by_event_class_with_item_ref_and_event_id() -> None:
    kg = InMemoryEntityKG()
    labeler = _labeler(kg)

    await labeler.stamp_pending(
        item_ref="item-42",
        event_class=EventClass.MORNING_BRIEF,
        disposition="held",
        resolved_at=_NOW,
        event_id="evt-42",
    )

    scored = await kg.claims_about(EventClass.MORNING_BRIEF.value, scope=_SCOPE)
    assert len(scored) == 1
    stored = scored[0].claim
    assert stored.subject == EventClass.MORNING_BRIEF.value

    envelope = json.loads(stored.payload)
    assert envelope["event_id"] == "evt-42"
    value = json.loads(envelope["value"])
    assert value["item_ref"] == "item-42"
