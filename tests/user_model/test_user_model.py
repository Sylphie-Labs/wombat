"""Tests for the UserModel pure-read seam (TK-42, EP-10).

``asyncio_mode = "auto"`` is configured in pyproject.toml (pytest-asyncio), so async test
functions run directly — no manual ``asyncio.run()`` driving needed.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from cogworx.claims.provenance import Claim, Provenance
from cogworx.knowledge.evidence import make_evidence
from cogworx.knowledge.identity import claim_id_for
from cogworx.testing.doubles import InMemoryEntityKG

from wombat.gate.models import GateItem, ItemKind
from wombat.rating.params import (
    RATING_CLAIM_PREDICATE,
    EventClass,
    RatingParams,
    default_params_for,
    to_claim_payload,
)
from wombat.user_model.user_model import UserModel

_NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)


def _make_item(
    *,
    item_kind: ItemKind = ItemKind.GENERIC,
    event_class: str | None = None,
) -> GateItem:
    payload = {} if event_class is None else {"event_class": event_class}
    return GateItem(
        item_id="item-1",
        item_kind=item_kind,
        created_at=_NOW.timestamp(),
        payload=payload,
    )


async def _write_rating_claim(
    kg: InMemoryEntityKG,
    *,
    subject: str,
    scope: str,
    payload: str,
) -> None:
    """Write a rating-parameter claim directly into ``kg`` (Q-41 ruling 4 wire shape)."""
    claim_id = claim_id_for(subject, RATING_CLAIM_PREDICATE, payload, scope=scope)
    claim = Claim(
        id=claim_id,
        subject=subject,
        predicate=RATING_CLAIM_PREDICATE,
        payload=payload,
        epistemic_type="observation",
        provenance=Provenance(source="human", confidence=0.9, recorded_at=_NOW),
        valid_from=_NOW,
        ingest_time=_NOW,
        created_by="test",
        scope=scope,
    )
    evidence = make_evidence(
        type="corroboration",
        polarity="+",
        source_id="test-source",
        source_authority=0.9,
        recorded_at=_NOW,
    )
    await kg.write_claim(claim, evidence=evidence)


# --- AC1: personalized claim -> deterministic point-read ---------------------------------


async def test_ratings_for_reads_personalized_claim_for_calendar_conflict() -> None:
    kg = InMemoryEntityKG()
    custom = RatingParams(urgency_base=0.9, urgency_gain=0.8, load_base=0.2, load_gain=0.3)
    await _write_rating_claim(
        kg,
        subject=EventClass.CALENDAR_CONFLICT.value,
        scope="user:alice",
        payload=to_claim_payload(custom),
    )
    model = UserModel(entity_kg=kg, user_id="alice")
    item = _make_item(event_class="calendar_conflict")

    result = await model.ratings_for(item)

    assert result == custom


async def test_ratings_for_scopes_to_the_injected_user_id() -> None:
    # A claim written under a DIFFERENT user's scope must not leak into this user's read.
    kg = InMemoryEntityKG()
    other = RatingParams(urgency_base=0.99)
    await _write_rating_claim(
        kg,
        subject=EventClass.CALENDAR_CONFLICT.value,
        scope="user:bob",
        payload=to_claim_payload(other),
    )
    model = UserModel(entity_kg=kg, user_id="alice")
    item = _make_item(event_class="calendar_conflict")

    result = await model.ratings_for(item)

    assert result == default_params_for(EventClass.CALENDAR_CONFLICT)


# --- AC2: graceful fallback to defaults ---------------------------------------------------


async def test_ratings_for_defaults_when_no_user_scope_node_exists() -> None:
    kg = InMemoryEntityKG()  # empty store, no claims written
    model = UserModel(entity_kg=kg, user_id="alice")
    item = _make_item(event_class="draft_reply")

    result = await model.ratings_for(item)

    assert result == default_params_for(EventClass.DRAFT_REPLY)


def test_resolve_event_class_uses_payload_key_when_present() -> None:
    model = UserModel(entity_kg=AsyncMock(), user_id="alice")
    item = _make_item(item_kind=ItemKind.GENERIC, event_class="calendar_conflict")

    assert model.resolve_event_class(item) is EventClass.CALENDAR_CONFLICT


@pytest.mark.parametrize(
    ("item_kind", "expected"),
    [
        (ItemKind.BRIEF, EventClass.MORNING_BRIEF),
        (ItemKind.REFLECTION, EventClass.REFLECTION),
        (ItemKind.DRAFT, EventClass.DRAFT_REPLY),
        (ItemKind.GENERIC, EventClass.GENERIC),
    ],
)
def test_resolve_event_class_falls_back_to_item_kind_map_when_no_payload_key(
    item_kind: ItemKind, expected: EventClass
) -> None:
    model = UserModel(entity_kg=AsyncMock(), user_id="alice")
    item = _make_item(item_kind=item_kind, event_class=None)

    assert model.resolve_event_class(item) is expected


async def test_ratings_for_defaults_via_item_kind_fallback_when_no_payload_key() -> None:
    kg = InMemoryEntityKG()
    model = UserModel(entity_kg=kg, user_id="alice")
    item = _make_item(item_kind=ItemKind.BRIEF, event_class=None)

    result = await model.ratings_for(item)

    assert result == default_params_for(EventClass.MORNING_BRIEF)


def test_resolve_event_class_falls_back_and_warns_on_invalid_payload_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    model = UserModel(entity_kg=AsyncMock(), user_id="alice")
    item = _make_item(item_kind=ItemKind.REFLECTION, event_class="not_a_real_event_class")

    with caplog.at_level(logging.WARNING):
        resolved = model.resolve_event_class(item)

    assert resolved is EventClass.REFLECTION
    assert any(record.levelno == logging.WARNING for record in caplog.records)


# --- AC3: store raises / malformed payload -> defaults + logged warning, never raises -----


async def test_ratings_for_defaults_and_warns_when_store_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken_kg = AsyncMock()
    broken_kg.claims_about.side_effect = RuntimeError("store unreachable")
    model = UserModel(entity_kg=broken_kg, user_id="alice")
    item = _make_item(event_class="calendar_conflict")

    with caplog.at_level(logging.WARNING):
        result = await model.ratings_for(item)

    assert result == default_params_for(EventClass.CALENDAR_CONFLICT)
    assert any(record.levelno == logging.WARNING for record in caplog.records)


async def test_ratings_for_defaults_and_warns_on_malformed_claim_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    kg = InMemoryEntityKG()
    await _write_rating_claim(
        kg,
        subject=EventClass.CALENDAR_CONFLICT.value,
        scope="user:alice",
        payload="{not valid json",
    )
    model = UserModel(entity_kg=kg, user_id="alice")
    item = _make_item(event_class="calendar_conflict")

    with caplog.at_level(logging.WARNING):
        result = await model.ratings_for(item)

    assert result == default_params_for(EventClass.CALENDAR_CONFLICT)
    assert any(record.levelno == logging.WARNING for record in caplog.records)


async def test_ratings_for_defaults_and_warns_on_unknown_payload_version(
    caplog: pytest.LogCaptureFixture,
) -> None:
    kg = InMemoryEntityKG()
    malformed = to_claim_payload(RatingParams()).replace('"version": 1', '"version": 999')
    await _write_rating_claim(
        kg,
        subject=EventClass.CALENDAR_CONFLICT.value,
        scope="user:alice",
        payload=malformed,
    )
    model = UserModel(entity_kg=kg, user_id="alice")
    item = _make_item(event_class="calendar_conflict")

    with caplog.at_level(logging.WARNING):
        result = await model.ratings_for(item)

    assert result == default_params_for(EventClass.CALENDAR_CONFLICT)
    assert any(record.levelno == logging.WARNING for record in caplog.records)
