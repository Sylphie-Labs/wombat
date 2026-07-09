"""TK-112 — WriteWindowSummariesStage acceptance criteria (EP-21, Q-99e).

In-memory substrate, ZERO network/model: ``entity_kg`` is cog-worx's ``InMemoryEntityKG``, written
through a REAL ``ObservationWriter`` (mirrors ``tests/pathways/test_dream_behavior_log_stage.py``'s
own idiom). The Postgres-backed ``BehaviorEventLog`` side is stood in for by a REAL instance over
an unreachable DSN (lazy — never actually connects) with its ``events_between`` method
monkeypatched to a recording/raising double — the genuine pg round-trip lives in this module's own
``test_pg_gated_...`` test (``WOMBAT_TEST_PG_DSN``); the rest of this module is about
``WriteWindowSummariesStage``'s own read/detect/write-seam logic.

  AC2 (retrievable by date): a fixture the detector splits into multiple windows -> ``run()``
      writes ONE ``productivity_window:<date>`` claim whose double-JSON-encoded value round-trips
      ``detect_productivity_windows``' own output (via ``window_summary_to_dict``); the ACTIVE
      claim is retrievable via a direct ``claims_about`` point read (no log scan).
  AC3 (empty log): no events -> the detector returns ``[]``; the stage STILL transitions to
      ``dream_run``; zero ``writer.record`` calls (skip-on-empty).
  (never-block): a ``BehaviorEventLog.events_between`` failure is caught, logged LOUD, and the
      stage STILL transitions onward — mirrors ``DreamBehaviorLogStage``'s own AC5 posture.
  AC4 (NG-3, structural): an AST identifier scan over ``write_window_summaries.py`` finds no
      render/surface/dashboard-implying identifier anywhere.
"""

from __future__ import annotations

import ast
import inspect
import json
import logging
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest
from cogworx.knowledge.scopes import ScopeRegistry
from cogworx.loop.result import Transition
from cogworx.testing.doubles import InMemoryEntityKG

from tests.support.stage_context_fake import StageContextFake
from wombat.behavior.event_log import BehaviorEventLog, BehaviorEventRow
from wombat.behavior.event_log import ensure_schema as ensure_behavior_event_log_schema
from wombat.behavior.stages.write_window_summaries import WriteWindowSummariesStage
from wombat.behavior.window_detector import detect_productivity_windows, window_summary_to_dict
from wombat.domain.daily_ledger import wombat_today
from wombat.user_model.claims import ClaimPredicate
from wombat.user_model.observation_writer import ObservationWriter

_USER_ID = "window-stage-test-user"
_SCOPE = f"user:{_USER_ID}"
_NOW = datetime(2026, 7, 9, 9, 0, 0, tzinfo=UTC)
_UNREACHABLE_DSN = "postgresql://nonexistent-host-should-never-be-dialed:1/db"

# Tokens that would imply a surfacing/visualization concern has crept into this write-only stage
# (NG-3). Checked against identifiers, not raw text (see window_detector's own scan test).
_SURFACE_TOKENS = ("render", "surface", "dashboard")


def _fake_store(
    monkeypatch: pytest.MonkeyPatch,
    *,
    events: Sequence[BehaviorEventRow] = (),
    raises: BaseException | None = None,
) -> tuple[BehaviorEventLog, list[tuple[datetime, datetime]]]:
    """A REAL ``BehaviorEventLog`` over an unreachable DSN (lazy — never connects) with
    ``events_between`` monkeypatched to either return a canned corpus or raise."""
    calls: list[tuple[datetime, datetime]] = []

    def _events_between(
        self: BehaviorEventLog, start: datetime, end: datetime
    ) -> Sequence[BehaviorEventRow]:
        calls.append((start, end))
        if raises is not None:
            raise raises
        return tuple(events)

    monkeypatch.setattr(BehaviorEventLog, "events_between", _events_between)
    return BehaviorEventLog(_UNREACHABLE_DSN), calls


def _row(
    *, key: str, event_type: str, timestamp_utc: datetime, outcome_label: str
) -> BehaviorEventRow:
    return BehaviorEventRow(
        idempotency_key=key,
        event_type=event_type,
        source_id="test-source",
        timestamp_utc=timestamp_utc,
        outcome_label=outcome_label,
        duration_seconds=None,
    )


def _fixture_events() -> list[BehaviorEventRow]:
    base = _NOW - timedelta(days=1)
    return [
        _row(
            key="a",
            event_type="draft_reply",
            timestamp_utc=base,
            outcome_label="outcome_ignored",
        ),
        _row(
            key="b",
            event_type="draft_reply",
            timestamp_utc=base + timedelta(minutes=10),
            outcome_label="outcome_load_bearing",
        ),
        _row(
            key="c",
            event_type="calendar_conflict",
            timestamp_utc=base + timedelta(hours=2),
            outcome_label="outcome_ignored",
        ),
    ]


# ================================================================================================
# AC2: retrievable by date, round-trips the detector's own output, no log scan
# ================================================================================================


async def test_ac2_writes_one_claim_whose_value_round_trips_the_detected_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity_kg = InMemoryEntityKG()
    writer = ObservationWriter(
        entity_kg=entity_kg, scope_registry=ScopeRegistry(), user_id=_USER_ID
    )
    fixture = _fixture_events()
    store, calls = _fake_store(monkeypatch, events=fixture)

    stage = WriteWindowSummariesStage(store=store, writer=writer, tz=ZoneInfo("UTC"))
    result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    expected_windows = detect_productivity_windows(fixture)
    assert isinstance(result, Transition)
    assert result.to == "dream_run"
    assert result.output.data == {"windows": len(expected_windows), "errors": 0}

    # events_between was called with the fixed 14-day lookback.
    assert calls == [(_NOW - timedelta(days=14), _NOW)]

    subject = f"productivity_window:{wombat_today(_NOW, ZoneInfo('UTC')).isoformat()}"
    scored = await entity_kg.claims_about(subject, scope=_SCOPE)
    active = [scored_claim.claim for scored_claim in scored if scored_claim.claim.valid_to is None]
    assert len(active) == 1
    assert active[0].predicate == ClaimPredicate.PRODUCTIVITY_WINDOW.value

    envelope = json.loads(active[0].payload)
    value = json.loads(envelope["value"])
    assert value == [window_summary_to_dict(window) for window in expected_windows]


# ================================================================================================
# AC3: empty log -> zero writes, still transitions
# ================================================================================================


async def test_ac3_empty_event_log_writes_no_claim_and_still_transitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity_kg = InMemoryEntityKG()
    writer = ObservationWriter(
        entity_kg=entity_kg, scope_registry=ScopeRegistry(), user_id=_USER_ID
    )
    record_calls: list[object] = []
    original_record = writer.record

    async def _spy_record(claim: object) -> str:
        record_calls.append(claim)
        return await original_record(claim)  # type: ignore[arg-type]

    monkeypatch.setattr(writer, "record", _spy_record)

    store, calls = _fake_store(monkeypatch, events=())
    stage = WriteWindowSummariesStage(store=store, writer=writer, tz=ZoneInfo("UTC"))
    result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_run"
    assert result.output.data == {"windows": 0, "errors": 0}
    assert record_calls == []
    assert calls == [(_NOW - timedelta(days=14), _NOW)]


# ================================================================================================
# never-block: a read/write failure is caught, logged, and the stage still transitions
# ================================================================================================


async def test_events_between_raise_is_caught_logged_and_still_transitions(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    entity_kg = InMemoryEntityKG()
    writer = ObservationWriter(
        entity_kg=entity_kg, scope_registry=ScopeRegistry(), user_id=_USER_ID
    )
    store, calls = _fake_store(
        monkeypatch, raises=RuntimeError("simulated events_between failure")
    )

    stage = WriteWindowSummariesStage(store=store, writer=writer, tz=ZoneInfo("UTC"))
    with caplog.at_level(
        logging.ERROR, logger="wombat.behavior.stages.write_window_summaries"
    ):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_run"  # STILL transitions — one bad night never blocks the terminal
    assert result.output.data == {"windows": 0, "errors": 1}
    assert calls  # events_between was in fact called
    assert any(
        record.levelno == logging.ERROR and "detector or write failed" in record.message
        for record in caplog.records
    )
    subject = f"productivity_window:{wombat_today(_NOW, ZoneInfo('UTC')).isoformat()}"
    assert await entity_kg.claims_about(subject, scope=_SCOPE) == ()


# ================================================================================================
# AC4: structural no-dashboard/surface/render guard
# ================================================================================================


def test_ac4_no_dashboard_surface_render_identifier() -> None:
    import wombat.behavior.stages.write_window_summaries as stage_module

    tree = ast.parse(inspect.getsource(stage_module))

    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            identifiers.add(node.name)
        elif isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.arg):
            identifiers.add(node.arg)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)

    for identifier in identifiers:
        for token in _SURFACE_TOKENS:
            assert token not in identifier.lower(), (
                f"identifier {identifier!r} contains surface-implying token {token!r}"
            )


# ================================================================================================
# pg-gated: the REAL BehaviorEventLog.events_between read path
# ================================================================================================

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping the TK-112 real-Postgres read-path proof. "
        "Start one with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres"
    ),
)


@pytest.fixture
def clean_table() -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_behavior_event_log_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE wombat_behavior_events")
        conn.commit()


@_requires_pg
async def test_pg_gated_real_events_between_read_path(clean_table: None) -> None:
    assert _DSN is not None
    store = BehaviorEventLog(_DSN)
    base = _NOW - timedelta(days=1)
    try:
        store.upsert(
            idempotency_key="pg-k1",
            event_type="draft_reply",
            source_id="gmail",
            timestamp_utc=base,
            outcome_label="outcome_load_bearing",
        )
        store.upsert(
            idempotency_key="pg-k2",
            event_type="draft_reply",
            source_id="gmail",
            timestamp_utc=base + timedelta(minutes=10),
            outcome_label="outcome_ignored",
        )

        entity_kg = InMemoryEntityKG()
        writer = ObservationWriter(
            entity_kg=entity_kg, scope_registry=ScopeRegistry(), user_id=_USER_ID
        )
        stage = WriteWindowSummariesStage(store=store, writer=writer, tz=ZoneInfo("UTC"))
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

        assert isinstance(result, Transition)
        assert result.to == "dream_run"
        assert result.output.data == {"windows": 1, "errors": 0}

        subject = f"productivity_window:{wombat_today(_NOW, ZoneInfo('UTC')).isoformat()}"
        scored = await entity_kg.claims_about(subject, scope=_SCOPE)
        active = [
            scored_claim.claim for scored_claim in scored if scored_claim.claim.valid_to is None
        ]
        assert len(active) == 1
    finally:
        store.close()
