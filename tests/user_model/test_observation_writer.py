"""Tests for the ObservationWriter behavior/outcome write seam (TK-44, EP-10).

``asyncio_mode = "auto"`` is configured in pyproject.toml (pytest-asyncio), so async test
functions run directly — no manual ``asyncio.run()`` driving needed.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cogworx.knowledge.scopes import ScopeRegistry
from cogworx.testing.doubles import InMemoryEntityKG

from wombat.gate.models import GateItem, ItemKind
from wombat.rating.params import EventClass, RatingParams
from wombat.user_model.claims import Claim, ClaimPredicate
from wombat.user_model.observation_writer import ObservationWriter
from wombat.user_model.user_model import UserModel

_NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)


# --- AC1: record() a BEHAVIOR_OBSERVED claim -> readable via claims_about -----------------


async def test_record_writes_behavior_observed_claim_readable_via_claims_about() -> None:
    kg = InMemoryEntityKG()
    writer = ObservationWriter(entity_kg=kg, scope_registry=ScopeRegistry(), user_id="alice")
    claim = Claim(
        predicate=ClaimPredicate.BEHAVIOR_OBSERVED,
        subject="calendar_conflict",
        value='{"action": "dismissed"}',
        event_id="evt-1",
        observed_at=_NOW,
    )

    claim_id = await writer.record(claim)

    scored = await kg.claims_about("calendar_conflict", scope="user:alice")
    assert len(scored) == 1
    stored = scored[0].claim
    assert stored.id == claim_id
    assert stored.predicate == ClaimPredicate.BEHAVIOR_OBSERVED.value
    payload = json.loads(stored.payload)
    assert payload["value"] == '{"action": "dismissed"}'
    assert payload["event_id"] == "evt-1"
    assert stored.provenance.source_ref == "source:system:wombat.observation_writer"
    assert stored.provenance.recorded_at is not None


# --- AC1b (drift lock): record_rating_params() -> UserModel.ratings_for round-trips --------


async def test_record_rating_params_readable_via_user_model_ratings_for() -> None:
    kg = InMemoryEntityKG()
    writer = ObservationWriter(entity_kg=kg, scope_registry=ScopeRegistry(), user_id="alice")
    params = RatingParams(urgency_base=0.9, urgency_gain=0.8, load_base=0.2, load_gain=0.3)

    await writer.record_rating_params(EventClass.CALENDAR_CONFLICT, params)

    model = UserModel(entity_kg=kg, user_id="alice")
    item = GateItem(
        item_id="item-1",
        item_kind=ItemKind.GENERIC,
        created_at=_NOW.timestamp(),
        payload={"event_class": "calendar_conflict"},
    )

    result = await model.ratings_for(item)

    assert result == params


# --- AC2: hand-rolled string predicate -> TypeError before any I/O ------------------------


async def test_record_rejects_hand_rolled_string_predicate_before_any_write() -> None:
    kg = AsyncMock()
    writer = ObservationWriter(entity_kg=kg, scope_registry=ScopeRegistry(), user_id="alice")
    # A duck-typed stand-in with a hand-rolled string predicate: Claim.__post_init__ would
    # itself reject this, so this proves ObservationWriter's OWN defense-in-depth check.
    bad_claim = SimpleNamespace(
        predicate="behavior_observed",
        subject="calendar_conflict",
        value="{}",
        event_id=None,
        observed_at=_NOW,
    )

    with pytest.raises(TypeError):
        await writer.record(bad_claim)  # type: ignore[arg-type]

    kg.write_claim.assert_not_called()


# --- AC3: injected KG write raises -> logged and RE-RAISED, never silently dropped --------


async def test_record_logs_and_reraises_on_entity_kg_write_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    kg = AsyncMock()
    kg.write_claim.side_effect = RuntimeError("store unreachable")
    writer = ObservationWriter(entity_kg=kg, scope_registry=ScopeRegistry(), user_id="alice")
    claim = Claim(
        predicate=ClaimPredicate.BEHAVIOR_OBSERVED,
        subject="calendar_conflict",
        value="{}",
        event_id=None,
        observed_at=_NOW,
    )

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="store unreachable"):
        await writer.record(claim)

    assert any(record.levelno == logging.ERROR for record in caplog.records)


async def test_record_rating_params_logs_and_reraises_on_entity_kg_write_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    kg = AsyncMock()
    kg.write_claim.side_effect = RuntimeError("store unreachable")
    writer = ObservationWriter(entity_kg=kg, scope_registry=ScopeRegistry(), user_id="alice")

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="store unreachable"):
        await writer.record_rating_params(EventClass.CALENDAR_CONFLICT, RatingParams())

    assert any(record.levelno == logging.ERROR for record in caplog.records)
