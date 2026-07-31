"""TK-310 — wombat_observations ledger + ObservationStore + ScreenActivityCollector acceptance
criteria (DEC-68(a)/(c)).

DB tests require a REAL Postgres and are gated on ``WOMBAT_TEST_PG_DSN`` (the same convention as
``tests/unit/test_chat_turns.py`` / ``tests/unit/test_user_facts.py``): absent it, tests are
skipped LOUDLY. NEVER point this at a live database.

  AC (store) ``ensure_all_schemas`` carries exactly TWELVE entries; ``ensure_schema`` is pinned-
      shape and idempotent; ``append_segment``/``get_window``/``prune_older_than`` round-trip.
  AC1 beats A -> A -> B yield exactly two closed segments with correct spans; titles truncated to
      ``_MAX_TITLE_CHARS``.
  AC2 a failed read (``None`` beat) skips that beat, logs AT MOST ONE WARNING per consecutive
      failure streak, never raises, and the collector keeps working afterward.
  AC5 a segment shorter than ``_MIN_SEGMENT_S`` is dropped — never appended to the store.
  AC6 a store raise while appending is caught, logged loudly once, and later segments still
      record normally.
  AC (structural) ``observations`` imports nothing from ``wombat.bootstrap``/``wombat.runtime``.
"""

from __future__ import annotations

import ast
import inspect
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest

from wombat import observations, schema_preflight
from wombat.observations import CurrentActivity, ObservationStore, ensure_schema
from wombat.observe_screen import ScreenActivityCollector, ScreenBeat

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping observations DB tests that require a real "
        "throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres"
    ),
)


@pytest.fixture
def fresh_table() -> None:
    """Drop ``wombat_observations``, simulating a brand-new empty Postgres."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS wombat_observations CASCADE")
        conn.commit()


def _columns(dsn: str) -> dict[str, str]:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'wombat_observations'"
        )
        return {row[0]: row[1] for row in cur.fetchall()}


# --------------------------------------------------------------------------------- store AC


@_requires_pg
def test_ensure_schema_creates_pinned_shape_and_is_idempotent(fresh_table: None) -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        ensure_schema(conn)  # must not raise, must not change anything

    cols = _columns(_DSN)
    assert cols["id"] == "bigint"
    assert cols["channel"] == "text"
    assert cols["kind"] == "text"
    assert cols["started_at"] == "timestamp with time zone"
    assert cols["ended_at"] == "timestamp with time zone"
    assert cols["payload"] == "jsonb"
    assert cols["day_key"] == "date"


def test_ensure_all_schemas_carries_exactly_twelve_entries() -> None:
    source = inspect.getsource(schema_preflight.ensure_all_schemas)
    calls = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith("ensure_") and line.strip().endswith("_schema(conn)")
    ]
    assert len(calls) == 12  # TK-310 added the twelfth entry (wombat_observations)
    assert "ensure_observations_schema(conn)" in source


@_requires_pg
def test_append_segment_get_window_prune_older_than_round_trip(fresh_table: None) -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        conn.commit()

    store = ObservationStore(_DSN)
    try:
        now = datetime.now(UTC)
        t0 = now - timedelta(hours=3)
        t1 = t0 + timedelta(minutes=5)
        t2 = now - timedelta(hours=1)
        t3 = t2 + timedelta(minutes=2)

        store.append_segment(
            "screen", "app_segment", t2, t3, {"app": "notepad.exe", "title": "notes"}, t2.date()
        )
        store.append_segment(
            "screen", "app_segment", t0, t1, {"app": "chrome.exe", "title": "tab"}, t0.date()
        )

        rows = store.get_window("screen", now - timedelta(hours=4), now)
        assert [row["payload"]["app"] for row in rows] == ["chrome.exe", "notepad.exe"]
        assert rows[0]["started_at"] == t0
        assert rows[0]["day_key"] == t0.date()

        # A different channel never surfaces in a "screen" window read.
        store.append_segment(
            "webcam", "presence", t2, t3, {"present": True}, t2.date()
        )
        rows_screen_only = store.get_window("screen", now - timedelta(hours=4), now)
        assert len(rows_screen_only) == 2

        # prune_older_than: only rows older than the horizon are removed.
        old_started = now - timedelta(days=observations._OBSERVATION_RETENTION_DAYS + 1)
        store.append_segment(
            "screen",
            "app_segment",
            old_started,
            old_started + timedelta(minutes=1),
            {"app": "old.exe", "title": "ancient"},
            old_started.date(),
        )
        deleted = store.prune_older_than(observations._OBSERVATION_RETENTION_DAYS)
        assert deleted == 1
        remaining_apps = {
            row["payload"]["app"]
            for row in store.get_window("screen", now - timedelta(days=60), now)
        }
        assert "old.exe" not in remaining_apps
        assert "chrome.exe" in remaining_apps
    finally:
        store.close()


def test_construction_does_zero_io() -> None:
    """Constructing an ObservationStore over a bogus DSN must not connect (Q-46: lazy)."""
    store = ObservationStore("postgresql://nonexistent-host-should-never-be-dialed:1/db")
    assert store._conn is None


def test_observations_imports_nothing_from_bootstrap_or_runtime() -> None:
    source = Path(observations.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
            imported_modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    assert not any("bootstrap" in mod for mod in imported_modules)
    assert not any(mod == "runtime" or mod.endswith(".runtime") for mod in imported_modules)


# --------------------------------------------------------------------------- collector ACs


@dataclass
class _FakeObservationStore:
    """A duck-typed ``ObservationSink`` fake — never touches Postgres."""

    raise_on_call: int | None = None
    segments: list[dict[str, Any]] = field(default_factory=list)
    _calls: int = 0

    def append_segment(
        self,
        channel: str,
        kind: str,
        started_at: datetime,
        ended_at: datetime,
        payload: dict[str, Any],
        day_key: date,
    ) -> None:
        self._calls += 1
        if self.raise_on_call == self._calls:
            raise RuntimeError("simulated store failure")
        self.segments.append(
            {
                "channel": channel,
                "kind": kind,
                "started_at": started_at,
                "ended_at": ended_at,
                "payload": payload,
                "day_key": day_key,
            }
        )


def _collector(store: _FakeObservationStore) -> ScreenActivityCollector:
    return ScreenActivityCollector(
        store=store,
        current_activity=CurrentActivity(),
        tz=ZoneInfo("UTC"),
        clock=lambda: datetime(2026, 7, 30, 9, 0, 0, tzinfo=UTC),
    )


# --------------------------------------------------------------------------------------- AC1


def test_ac1_beats_a_a_b_yield_two_segments_with_correct_spans_and_truncated_titles() -> None:
    store = _FakeObservationStore()
    collector = _collector(store)
    t0 = datetime(2026, 7, 30, 9, 0, 0, tzinfo=UTC)
    long_title = "x" * 200  # exceeds _MAX_TITLE_CHARS (120)

    beat_a = ScreenBeat(app="chrome.exe", title=long_title)
    beat_b = ScreenBeat(app="notepad.exe", title="notes")

    collector.process_beat(beat_a, now=t0)  # opens A
    collector.process_beat(beat_a, now=t0 + timedelta(seconds=40))  # same segment — no-op
    collector.process_beat(beat_b, now=t0 + timedelta(seconds=80))  # closes A (80s), opens B
    collector.close(now=t0 + timedelta(seconds=120))  # closes B (40s)

    assert len(store.segments) == 2
    seg_a, seg_b = store.segments
    assert seg_a["payload"]["app"] == "chrome.exe"
    assert seg_a["payload"]["title"] == long_title[:120]
    assert len(seg_a["payload"]["title"]) == 120
    assert seg_a["started_at"] == t0
    assert seg_a["ended_at"] == t0 + timedelta(seconds=80)

    assert seg_b["payload"]["app"] == "notepad.exe"
    assert seg_b["payload"]["title"] == "notes"
    assert seg_b["started_at"] == t0 + timedelta(seconds=80)
    assert seg_b["ended_at"] == t0 + timedelta(seconds=120)


# --------------------------------------------------------------------------------------- AC2


def test_ac2_failed_read_skips_beat_warns_once_per_streak_and_loop_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _FakeObservationStore()
    collector = _collector(store)
    t0 = datetime(2026, 7, 30, 9, 0, 0, tzinfo=UTC)
    beat_a = ScreenBeat(app="chrome.exe", title="tab")

    collector.process_beat(beat_a, now=t0)  # opens A

    with caplog.at_level(logging.WARNING, logger="wombat.observe_screen"):
        # Three consecutive failed beats: closes A (10s span, dropped as sub-minimum), then two
        # more failures with nothing open — only ONE warning for the whole streak.
        collector.process_beat(None, now=t0 + timedelta(seconds=10))
        collector.process_beat(None, now=t0 + timedelta(seconds=20))
        collector.process_beat(None, now=t0 + timedelta(seconds=30))

    assert len(caplog.records) == 1
    assert len(store.segments) == 0  # the 10s A segment never met _MIN_SEGMENT_S

    caplog.clear()
    # The loop survives: a fresh successful beat resumes normal coalescing/closing.
    collector.process_beat(beat_a, now=t0 + timedelta(seconds=40))
    collector.process_beat(None, now=t0 + timedelta(seconds=100))  # closes it (60s, kept)

    assert len(store.segments) == 1
    assert store.segments[0]["payload"]["app"] == "chrome.exe"

    # A brand-new failure streak after a success logs exactly one MORE warning (never one total).
    with caplog.at_level(logging.WARNING, logger="wombat.observe_screen"):
        collector.process_beat(None, now=t0 + timedelta(seconds=110))
        collector.process_beat(None, now=t0 + timedelta(seconds=120))
    assert len(caplog.records) == 1


# --------------------------------------------------------------------------------------- AC5


def test_ac5_segment_under_min_seconds_is_dropped_not_stored() -> None:
    store = _FakeObservationStore()
    collector = _collector(store)
    t0 = datetime(2026, 7, 30, 9, 0, 0, tzinfo=UTC)
    beat_a = ScreenBeat(app="chrome.exe", title="tab")
    beat_b = ScreenBeat(app="notepad.exe", title="notes")

    collector.process_beat(beat_a, now=t0)
    collector.process_beat(beat_b, now=t0 + timedelta(seconds=29))  # closes A after 29s -> dropped
    collector.close(now=t0 + timedelta(seconds=90))  # closes B after 61s -> kept

    assert len(store.segments) == 1
    assert store.segments[0]["payload"]["app"] == "notepad.exe"
    assert store.segments[0]["started_at"] == t0 + timedelta(seconds=29)


# --------------------------------------------------------------------------------------- AC6


def test_ac6_store_raise_logs_loudly_and_later_segments_still_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _FakeObservationStore(raise_on_call=1)
    collector = _collector(store)
    t0 = datetime(2026, 7, 30, 9, 0, 0, tzinfo=UTC)
    beat_a = ScreenBeat(app="chrome.exe", title="tab")
    beat_b = ScreenBeat(app="notepad.exe", title="notes")
    beat_c = ScreenBeat(app="slack.exe", title="general")

    collector.process_beat(beat_a, now=t0)
    with caplog.at_level(logging.WARNING, logger="wombat.observe_screen"):
        # Closing A (40s span, meets minimum) is the FIRST append_segment call -> raises.
        collector.process_beat(beat_b, now=t0 + timedelta(seconds=40))

    assert any("raised" in record.message.lower() for record in caplog.records)
    assert len(store.segments) == 0  # the raising append never landed

    # Later: closing B (the SECOND append_segment call) succeeds normally.
    collector.process_beat(beat_c, now=t0 + timedelta(seconds=90))

    assert len(store.segments) == 1
    assert store.segments[0]["payload"]["app"] == "notepad.exe"
