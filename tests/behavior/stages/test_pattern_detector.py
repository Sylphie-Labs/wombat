"""TK-113 — PatternDetectorStage acceptance criteria (EP-22, Q-99b/f/g).

In-memory substrate for AC1/AC2/AC3(a)/AC3(b)/AC4, ZERO network/model: ``entity_kg`` is cog-worx's
``InMemoryEntityKG``, written through a REAL ``ObservationWriter`` (mirrors ``tests/behavior/
stages/test_write_window_summaries.py``'s own idiom). ``enqueue`` is a small recording fake
(``_RecordingEnqueue``) standing in for the injected ``Callable[[QueueItem], EnqueueResult]`` seam
— the genuine ``WombatQueue.enqueue`` bind lives in this module's own pg-gated test
(``WOMBAT_TEST_PG_DSN``, AC3(c)). ``kb`` is the REAL packaged seed KB (``load_psychology_kb()``),
never a hand-rolled fixture — AC1/AC2 exercise the real KB entries.

  AC1: a productivity_window claim whose metrics trip the KB's ``rapid_context_switching`` entry
      -> ``run()`` enqueues exactly ONE ``pattern_reflection`` ``QueueItem`` (recorded by the fake);
      a same-night re-fire against a fake returning ``ALREADY_QUEUED`` still only ever produced ONE
      ``QUEUED`` result across both runs; the item's ``idempotency_key`` equals
      ``idempotency_key("wombat.reflection", date)`` computed independently.
  AC2: no nudge-worthy pattern (no claim at all, and a claim whose metrics trip nothing) -> zero
      enqueue calls, the stage still ``Transition``s to ``dream_run``.
  AC3: (a) structural — ``PatternDetectorStage.__init__`` carries no gate/pending/journal
      collaborator, only ``entity_kg``/``kb``/``enqueue``/``user_id``/``tz``; (b) the enqueued
      payload is driven through the REAL ``gate_item_from_queue_item`` + ``UserModel.
      resolve_event_class`` -> ``ItemKind.REFLECTION``/``EventClass.REFLECTION`` (rated like any
      other item, no gate bypass); (c) pg-gated — the REAL ``WombatQueue.enqueue`` bind: enqueue
      once ``QUEUED``, re-enqueue (same night) ``ALREADY_QUEUED``.
  AC4: the enqueued payload's key set is EXACTLY ``{item_kind, event_class, kind, pattern_id,
      window_ref, date}`` — no motive field, no clinical label, no "why" key.

Also covers the never-block posture (mirrors every other dream stage's own AC): a ``claims_about``
read/parse failure, and a ``QueueFullError`` from ``enqueue``, are both caught, logged LOUD, and
the stage still transitions onward.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import psycopg
import pytest
from cogworx.knowledge.scopes import ScopeRegistry
from cogworx.loop.result import Transition
from cogworx.testing.doubles import InMemoryEntityKG

from tests.support.stage_context_fake import StageContextFake
from wombat.behavior.stages.pattern_detector import PatternDetectorStage
from wombat.domain.daily_ledger import wombat_today
from wombat.domain.item_identity import idempotency_key
from wombat.gate.gate import gate_item_from_queue_item
from wombat.gate.models import ItemKind
from wombat.kb.loader import load_psychology_kb
from wombat.queue import EnqueueResult, QueueFullError, QueueItem
from wombat.queue import ensure_schema as ensure_queue_schema
from wombat.rating.params import EventClass
from wombat.user_model.claims import Claim, ClaimPredicate
from wombat.user_model.observation_writer import ObservationWriter
from wombat.user_model.user_model import UserModel

_USER_ID = "pattern-detector-test-user"
_SCOPE = f"user:{_USER_ID}"
_NOW = datetime(2026, 7, 9, 3, 0, 0, tzinfo=UTC)
_TZ = ZoneInfo("UTC")
_DATE_ISO = wombat_today(_NOW, _TZ).isoformat()
_WINDOW_REF = f"productivity_window:{_DATE_ISO}"


@dataclass
class _RecordingEnqueue:
    """A recording fake for the injected ``Callable[[QueueItem], EnqueueResult]`` seam. ``result``
    (default ``QUEUED``) is mutable between calls so a test can simulate a same-night re-fire."""

    calls: list[QueueItem] = field(default_factory=list)
    result: EnqueueResult = EnqueueResult.QUEUED
    raises: BaseException | None = None

    def __call__(self, item: QueueItem) -> EnqueueResult:
        self.calls.append(item)
        if self.raises is not None:
            raise self.raises
        return self.result


def _matching_summaries() -> list[dict[str, object]]:
    """One WindowSummary dict tripping the seed KB's ``rapid_context_switching`` entry
    (``switch_rate > 0.6``) — nothing else in the closed metric set is even read by that entry."""
    return [
        {
            "start_utc": _NOW.isoformat(),
            "end_utc": _NOW.isoformat(),
            "event_count": 5,
            "switch_rate": 0.8,
            "outcome_mix": {},
        }
    ]


def _non_matching_summaries() -> list[dict[str, object]]:
    """Two WindowSummary dicts whose derived metrics (switch_rate=0.3, window_count=2,
    event_count=10) trip NONE of the seed KB's seven entries."""
    one = {
        "start_utc": _NOW.isoformat(),
        "end_utc": _NOW.isoformat(),
        "event_count": 5,
        "switch_rate": 0.3,
        "outcome_mix": {},
    }
    return [one, dict(one)]


async def _write_window_claim(
    writer: ObservationWriter, *, summaries: list[dict[str, object]]
) -> None:
    await writer.record(
        Claim(
            predicate=ClaimPredicate.PRODUCTIVITY_WINDOW,
            subject=_WINDOW_REF,
            value=json.dumps(summaries),
            event_id=None,
            observed_at=_NOW,
        )
    )


# ================================================================================================
# AC1: KB match -> exactly one enqueue, canonical key, never a second QUEUED for the same night
# ================================================================================================


async def test_ac1_kb_match_enqueues_exactly_one_pattern_reflection_item() -> None:
    entity_kg = InMemoryEntityKG()
    writer = ObservationWriter(
        entity_kg=entity_kg, scope_registry=ScopeRegistry(), user_id=_USER_ID
    )
    await _write_window_claim(writer, summaries=_matching_summaries())

    kb = load_psychology_kb()
    enqueue = _RecordingEnqueue()
    stage = PatternDetectorStage(
        entity_kg=entity_kg, kb=kb, enqueue=enqueue, user_id=_USER_ID, tz=_TZ
    )

    result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_run"
    assert len(enqueue.calls) == 1
    assert result.output.data == {
        "enqueued": 1,
        "pattern_id": "rapid_context_switching",
        "errors": 0,
    }

    item = enqueue.calls[0]
    assert item.idempotency_key == idempotency_key("wombat.reflection", _DATE_ISO)

    # A same-night re-fire against a fake now returning ALREADY_QUEUED — still only ONE QUEUED
    # result ever produced across both runs (the report's own "enqueued" count for the re-fire
    # is 0, never a second 1).
    enqueue.result = EnqueueResult.ALREADY_QUEUED
    result2 = await stage.run(StageContextFake(now_fn=lambda: _NOW))
    assert isinstance(result2, Transition)
    assert len(enqueue.calls) == 2
    assert result2.output.data["enqueued"] == 0
    assert result.output.data["enqueued"] + result2.output.data["enqueued"] == 1

    # Both calls carried the SAME idempotency_key — the date-keyed structural cap (Q-99f).
    assert enqueue.calls[1].idempotency_key == item.idempotency_key


# ================================================================================================
# AC2: no nudge-worthy pattern -> zero enqueues, stage still transitions
# ================================================================================================


async def test_ac2_no_claim_at_all_zero_enqueues_and_still_transitions() -> None:
    entity_kg = InMemoryEntityKG()
    kb = load_psychology_kb()
    enqueue = _RecordingEnqueue()
    stage = PatternDetectorStage(
        entity_kg=entity_kg, kb=kb, enqueue=enqueue, user_id=_USER_ID, tz=_TZ
    )

    result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_run"
    assert enqueue.calls == []
    assert result.output.data == {"enqueued": 0, "pattern_id": None, "errors": 0}


async def test_ac2_non_matching_metrics_zero_enqueues_and_still_transitions() -> None:
    entity_kg = InMemoryEntityKG()
    writer = ObservationWriter(
        entity_kg=entity_kg, scope_registry=ScopeRegistry(), user_id=_USER_ID
    )
    await _write_window_claim(writer, summaries=_non_matching_summaries())

    kb = load_psychology_kb()
    enqueue = _RecordingEnqueue()
    stage = PatternDetectorStage(
        entity_kg=entity_kg, kb=kb, enqueue=enqueue, user_id=_USER_ID, tz=_TZ
    )

    result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_run"
    assert enqueue.calls == []
    assert result.output.data == {"enqueued": 0, "pattern_id": None, "errors": 0}


# ================================================================================================
# AC3(a): structural — no gate/pending/journal collaborator
# ================================================================================================


def test_ac3a_no_gate_pending_or_journal_collaborator() -> None:
    params = set(inspect.signature(PatternDetectorStage.__init__).parameters) - {"self"}
    assert params == {"entity_kg", "kb", "enqueue", "user_id", "tz"}
    for token in ("gate", "pending", "journal"):
        assert not any(token in param for param in params)


# ================================================================================================
# AC3(b): the enqueued payload rides the REAL gate mapping + UserModel event-class resolution
# ================================================================================================


async def test_ac3b_payload_resolves_through_the_real_gate_and_user_model() -> None:
    entity_kg = InMemoryEntityKG()
    writer = ObservationWriter(
        entity_kg=entity_kg, scope_registry=ScopeRegistry(), user_id=_USER_ID
    )
    await _write_window_claim(writer, summaries=_matching_summaries())

    kb = load_psychology_kb()
    enqueue = _RecordingEnqueue()
    stage = PatternDetectorStage(
        entity_kg=entity_kg, kb=kb, enqueue=enqueue, user_id=_USER_ID, tz=_TZ
    )
    await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert len(enqueue.calls) == 1
    drained_item = QueueItem(
        idempotency_key=enqueue.calls[0].idempotency_key,
        payload=enqueue.calls[0].payload,
        item_id=1,
    )

    gate_item = gate_item_from_queue_item(drained_item)
    assert gate_item.item_kind is ItemKind.REFLECTION

    user_model = UserModel(entity_kg=InMemoryEntityKG(), user_id=_USER_ID)
    assert user_model.resolve_event_class(gate_item) is EventClass.REFLECTION


# ================================================================================================
# AC4: payload key set — no motive, no clinical label, no "why" key
# ================================================================================================


async def test_ac4_payload_key_set_carries_no_motive_or_why_key() -> None:
    entity_kg = InMemoryEntityKG()
    writer = ObservationWriter(
        entity_kg=entity_kg, scope_registry=ScopeRegistry(), user_id=_USER_ID
    )
    await _write_window_claim(writer, summaries=_matching_summaries())

    kb = load_psychology_kb()
    enqueue = _RecordingEnqueue()
    stage = PatternDetectorStage(
        entity_kg=entity_kg, kb=kb, enqueue=enqueue, user_id=_USER_ID, tz=_TZ
    )
    await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert len(enqueue.calls) == 1
    payload = enqueue.calls[0].payload
    assert set(payload.keys()) == {
        "item_kind",
        "event_class",
        "kind",
        "pattern_id",
        "window_ref",
        "date",
    }
    assert payload["item_kind"] == "reflection"
    assert payload["event_class"] == "reflection"
    assert payload["kind"] == "pattern_reflection"
    assert payload["pattern_id"] == "rapid_context_switching"
    assert payload["window_ref"] == _WINDOW_REF
    assert payload["date"] == _DATE_ISO


# ================================================================================================
# never-block: a claims_about read/parse failure, and a QueueFullError, are both caught/logged
# ================================================================================================


async def test_claims_about_raise_is_caught_logged_and_still_transitions(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    entity_kg = InMemoryEntityKG()

    async def _raising_claims_about(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated claims_about failure")

    monkeypatch.setattr(entity_kg, "claims_about", _raising_claims_about)

    kb = load_psychology_kb()
    enqueue = _RecordingEnqueue()
    stage = PatternDetectorStage(
        entity_kg=entity_kg, kb=kb, enqueue=enqueue, user_id=_USER_ID, tz=_TZ
    )

    with caplog.at_level(logging.ERROR, logger="wombat.behavior.stages.pattern_detector"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_run"  # never blocks the terminal
    assert result.output.data == {"enqueued": 0, "pattern_id": None, "errors": 1}
    assert enqueue.calls == []
    assert any(
        record.levelno == logging.ERROR and "claims_about read or claim payload parse failed"
        in record.message
        for record in caplog.records
    )


async def test_queue_full_error_is_caught_logged_and_still_transitions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    entity_kg = InMemoryEntityKG()
    writer = ObservationWriter(
        entity_kg=entity_kg, scope_registry=ScopeRegistry(), user_id=_USER_ID
    )
    await _write_window_claim(writer, summaries=_matching_summaries())

    kb = load_psychology_kb()
    enqueue = _RecordingEnqueue(raises=QueueFullError("wombat_queue is at capacity"))
    stage = PatternDetectorStage(
        entity_kg=entity_kg, kb=kb, enqueue=enqueue, user_id=_USER_ID, tz=_TZ
    )

    with caplog.at_level(logging.ERROR, logger="wombat.behavior.stages.pattern_detector"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_run"  # never blocks the terminal
    assert result.output.data == {
        "enqueued": 0,
        "pattern_id": "rapid_context_switching",
        "errors": 1,
    }
    assert len(enqueue.calls) == 1
    assert any(
        record.levelno == logging.ERROR and "enqueue failed" in record.message
        for record in caplog.records
    )


# ================================================================================================
# AC3(c): pg-gated — the REAL WombatQueue.enqueue bind
# ================================================================================================

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping the TK-113 real-WombatQueue enqueue-bind "
        "proof. Start one with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres"
    ),
)


@pytest.fixture
def clean_queue() -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_queue_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE wombat_queue")
        conn.commit()


@_requires_pg
async def test_pg_gated_real_wombat_queue_enqueue_bind(clean_queue: None) -> None:
    assert _DSN is not None
    from wombat.queue import WombatQueue

    queue = WombatQueue(_DSN, max_size=10)
    try:
        entity_kg = InMemoryEntityKG()
        writer = ObservationWriter(
            entity_kg=entity_kg, scope_registry=ScopeRegistry(), user_id=_USER_ID
        )
        await _write_window_claim(writer, summaries=_matching_summaries())

        kb = load_psychology_kb()
        stage = PatternDetectorStage(
            entity_kg=entity_kg, kb=kb, enqueue=queue.enqueue, user_id=_USER_ID, tz=_TZ
        )

        result1 = await stage.run(StageContextFake(now_fn=lambda: _NOW))
        assert isinstance(result1, Transition)
        assert result1.output.data["enqueued"] == 1

        # Same-night re-fire over the SAME real queue — structurally ALREADY_QUEUED.
        result2 = await stage.run(StageContextFake(now_fn=lambda: _NOW))
        assert isinstance(result2, Transition)
        assert result2.output.data["enqueued"] == 0
    finally:
        queue.close()
