"""tests/sources/test_screenpipe_source.py — ScreenpipeEventSource acceptance criteria
(TK-322, EP-37, DEC-70a/e/f).

  AC(a): a scripted fake client feeding a context timeline (alt-tab flapping, one sustained
      switch, one 30-minute block ending) -> exactly ONE ``context_switch`` (post-dwell) and
      ONE ``focus_block_end``, bounded payloads matching the pinned shape, title capped at 160,
      no OCR text field present: ``test_ac_a_*``.
  AC(b): the same timeline replayed on a FRESH source instance -> stable ``event_key``s; the
      ``SeenLedger``/``DedupingEnqueuer`` dedupe path re-enqueues nothing (throwaway-pg):
      ``test_ac_b_*``.
  AC(c): (covered in ``test_bootstrap.py`` — wiring lives there, per RULING r2's files_in_scope
      split.)
  AC(d): a genuinely degraded client (a real ``ScreenpipeClient`` pointed at a port nothing is
      listening on) -> ``[]`` every beat, zero raises: ``test_ac_d_*``.
  AC(e): one derived event driven end-to-end through a real ``WombatQueue`` (throwaway pg) and
      the real gate scoring path -> resolves ``EventClass.SCREEN_ACTIVITY``, decision HOLD, zero
      model calls: ``test_ac_e_*``.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

import pytest

from wombat.domain.item_identity import idempotency_key as derive_key
from wombat.gate.gate import gate_item_from_queue_item
from wombat.gate.models import ScoredItem
from wombat.gate.scoring import urgency
from wombat.gate.trigger import is_surfacing_worthy
from wombat.integrations.screenpipe.client import _MAX_RESULTS as _CLIENT_MAX_RESULTS
from wombat.integrations.screenpipe.client import ScreenpipeClient, ScreenpipeItem
from wombat.params import load_operating_params
from wombat.queue import EnqueueResult, QueueItem, WombatQueue
from wombat.queue import ensure_schema as ensure_queue_schema
from wombat.rating.params import EventClass, default_params_for
from wombat.sources.base import SourceEvent
from wombat.sources.registry import SourceRegistry
from wombat.sources.screenpipe_source import ScreenpipeEventSource
from wombat.sources.seen_ledger import DedupingEnqueuer, SeenLedger
from wombat.sources.seen_ledger import ensure_schema as ensure_seen_events_schema
from wombat.user_model.user_model import resolve_event_class_for_item

_BASE = datetime(2026, 7, 31, 8, 0, tzinfo=UTC)

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping TK-322's pg-armed dedupe/end-to-end-gate "
        "tests. Start a throwaway Postgres with:\n"
        "  docker run --rm -d -p 5441:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5441/postgres"
    ),
)


class _FakeClock:
    """A mutable, advanceable fake clock — mirrors the ``poll_interval_seconds``-driven fake
    clocks other source test modules use, but exposes ``.advance``/``.set`` for readability."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def set(self, at: datetime) -> None:
        self.now = at


class _WindowedFakeClient:
    """A fake ``ScreenpipeClient``-shaped double: ``search`` filters a fixed master list of
    ``ScreenpipeItem`` by ``[start, end)`` on ``captured_at`` — the same real windowing
    semantics production relies on, so driving ``poll()`` across an advancing fake clock
    reproduces the "since the last poll" merge exactly."""

    def __init__(self, items: list[ScreenpipeItem]) -> None:
        self._items = items

    def search(
        self,
        start: datetime,
        end: datetime,
        *,
        app_name: str | None = None,
        limit: int | None = None,
    ) -> list[ScreenpipeItem]:
        return [item for item in self._items if start <= item.captured_at < end]


class _StaticFakeClient:
    """A fake ``ScreenpipeClient``-shaped double whose ``search`` ignores the requested window
    and always returns the SAME fixed item list — used by regression tests that need every poll
    to see fresh items regardless of the advancing ``_search_from`` cursor."""

    def __init__(self, items: list[ScreenpipeItem]) -> None:
        self._items = items

    def search(
        self,
        start: datetime,
        end: datetime,
        *,
        app_name: str | None = None,
        limit: int | None = None,
    ) -> list[ScreenpipeItem]:
        return list(self._items)


class _FlakyOnceClient:
    """A fake client whose ``search`` raises on its FIRST call only, then serves ``items``
    windowed exactly like ``_WindowedFakeClient`` — records every ``(start, end)`` it was
    called with, for asserting on retried windows (ISS-37 m5)."""

    def __init__(self, items: list[ScreenpipeItem]) -> None:
        self._items = items
        self._raised = False
        self.calls: list[tuple[datetime, datetime]] = []

    def search(
        self,
        start: datetime,
        end: datetime,
        *,
        app_name: str | None = None,
        limit: int | None = None,
    ) -> list[ScreenpipeItem]:
        self.calls.append((start, end))
        if not self._raised:
            self._raised = True
            raise RuntimeError("simulated transient client.search failure")
        return [item for item in self._items if start <= item.captured_at < end]


class _DegradedOnceClient:
    """A fake client that reproduces the REAL ``ScreenpipeClient.search`` degrade shape
    (DEC-70i) — degraded on its FIRST call, returning ``[]`` WITHOUT raising and setting
    ``last_search_degraded = True`` exactly like the real client does while an outage is
    ongoing; then recovers and serves ``items`` windowed like ``_WindowedFakeClient``, with
    ``last_search_degraded`` cleared back to ``False``. Records every ``(start, end)`` window it
    was called with (ISS-37-RIDER m5)."""

    def __init__(self, items: list[ScreenpipeItem]) -> None:
        self._items = items
        self._recovered = False
        self.calls: list[tuple[datetime, datetime]] = []
        self.last_search_degraded = False

    def search(
        self,
        start: datetime,
        end: datetime,
        *,
        app_name: str | None = None,
        limit: int | None = None,
    ) -> list[ScreenpipeItem]:
        self.calls.append((start, end))
        if not self._recovered:
            self._recovered = True
            self.last_search_degraded = True
            return []
        self.last_search_degraded = False
        return [item for item in self._items if start <= item.captured_at < end]


def _item(app: str, title: str, ref: str, offset_s: float) -> ScreenpipeItem:
    return ScreenpipeItem(
        app=app,
        title=title,
        text_snippet="",  # never read by the source; present only to satisfy the dataclass
        captured_at=_BASE + timedelta(seconds=offset_s),
        ref_id=ref,
    )


def _scripted_timeline() -> list[ScreenpipeItem]:
    """Alt-tab flapping (never sustains 120s) -> one sustained switch (confirmed at t=160,
    130s dwell) -> the SAME context ending at t=1531 after a total 1500s dwell (the pinned
    focus-block floor, exactly) -> a fresh, unconfirmed trailing context."""
    return [
        _item("chrome", "Tab A", "ref-flap-1", 0),
        _item("slack", "DMs", "ref-flap-2", 5),
        _item("chrome", "Tab A", "ref-flap-3", 10),
        _item("slack", "DMs", "ref-flap-4", 15),
        _item("chrome", "Tab A", "ref-flap-5", 20),
        _item("editor", "main.py — myproject", "ref-editor-start", 30),
        _item("editor", "main.py — myproject", "ref-editor-2", 90),
        _item("editor", "main.py — myproject", "ref-editor-confirm", 160),
        _item("editor", "main.py — myproject", "ref-editor-mid", 1000),
        _item("editor", "main.py — myproject", "ref-editor-last", 1530),
        _item("mail", "Inbox", "ref-mail-1", 1531),
    ]


async def _drive_timeline(
    source: ScreenpipeEventSource, clock: _FakeClock
) -> list[dict[str, object]]:
    """Poll ``source`` across two windows that split the scripted timeline right after the
    dwell-confirming sample (t=160) — proving the merge survives a poll boundary mid-context,
    not just a single giant window."""
    events: list[dict[str, object]] = []
    clock.set(_BASE + timedelta(seconds=161))
    events += [e.payload for e in await source.poll()]
    clock.set(_BASE + timedelta(seconds=1532))
    events += [e.payload for e in await source.poll()]
    return events


# --------------------------------------------------------------------------------------- AC(a)


async def test_ac_a_flapping_never_fires_and_the_sustained_block_fires_exactly_once() -> None:
    clock = _FakeClock(_BASE + timedelta(seconds=-1))
    client = _WindowedFakeClient(_scripted_timeline())
    source = ScreenpipeEventSource(client=client, poll_interval_seconds=30.0, clock=clock)

    payloads = await _drive_timeline(source, clock)

    kinds = [p["event"] for p in payloads]
    assert kinds == ["context_switch", "focus_block_end"]


async def test_ac_a_context_switch_payload_shape_is_bounded_and_dwell_pinned() -> None:
    clock = _FakeClock(_BASE + timedelta(seconds=-1))
    client = _WindowedFakeClient(_scripted_timeline())
    source = ScreenpipeEventSource(client=client, poll_interval_seconds=30.0, clock=clock)

    payloads = await _drive_timeline(source, clock)
    switch = next(p for p in payloads if p["event"] == "context_switch")

    assert switch == {
        "event": "context_switch",
        "app": "editor",
        "title": "main.py — myproject",
        "started_at": (_BASE + timedelta(seconds=30)).isoformat(),
        "duration_s": 130.0,
        "screenpipe_ref": "ref-editor-start",
        "event_class": "screen_activity",
    }
    assert "text" not in switch and "text_snippet" not in switch and "ocr" not in switch


async def test_ac_a_focus_block_end_payload_shape_is_bounded_at_the_pinned_floor() -> None:
    clock = _FakeClock(_BASE + timedelta(seconds=-1))
    client = _WindowedFakeClient(_scripted_timeline())
    source = ScreenpipeEventSource(client=client, poll_interval_seconds=30.0, clock=clock)

    payloads = await _drive_timeline(source, clock)
    block_end = next(p for p in payloads if p["event"] == "focus_block_end")

    assert block_end == {
        "event": "focus_block_end",
        "app": "editor",
        "title": "main.py — myproject",
        "started_at": (_BASE + timedelta(seconds=30)).isoformat(),
        "duration_s": 1500.0,
        "screenpipe_ref": "ref-editor-start",
        "event_class": "screen_activity",
    }


async def test_ac_a_title_is_capped_at_160_chars() -> None:
    long_title = "x" * 500
    clock = _FakeClock(_BASE + timedelta(seconds=-1))
    items = [
        _item("editor", long_title, "ref-a", 0),
        _item("editor", long_title, "ref-a", 130),  # confirms the 120s dwell
    ]
    client = _WindowedFakeClient(items)
    source = ScreenpipeEventSource(client=client, poll_interval_seconds=30.0, clock=clock)

    clock.set(_BASE + timedelta(seconds=200))
    payloads = [e.payload for e in await source.poll()]

    switch = next(p for p in payloads if p["event"] == "context_switch")
    assert len(switch["title"]) == 160
    assert switch["title"] == long_title[:160]


async def test_ac_a_title_churn_within_the_same_app_does_not_reset_dwell() -> None:
    """Regression: the run's identity is ``app`` alone. A title that changes on every sample
    (notification count, unsaved marker, clock in the tab title) must not prevent
    ``context_switch``/``focus_block_end`` from firing, and the LATEST title observed is what
    the payload carries."""
    clock = _FakeClock(_BASE + timedelta(seconds=-1))
    churning = [
        _item("chrome", f"Tab ({n})", f"ref-{n}", offset)
        for n, offset in enumerate(range(0, 1501, 30))
    ]
    items = [*churning, _item("slack", "DMs", "ref-away", 1501)]
    client = _WindowedFakeClient(items)
    source = ScreenpipeEventSource(client=client, poll_interval_seconds=30.0, clock=clock)

    clock.set(_BASE + timedelta(seconds=1502))
    payloads = [e.payload for e in await source.poll()]

    kinds = [p["event"] for p in payloads]
    assert "context_switch" in kinds
    assert "focus_block_end" in kinds
    block_end = next(p for p in payloads if p["event"] == "focus_block_end")
    assert block_end["app"] == "chrome"
    assert block_end["title"] == churning[-1].title[:160]
    assert block_end["duration_s"] == 1500.0


# ------------------------------------------------------------------------- ISS-37 regressions


async def test_iss37_m2_derive_failure_warns_once_per_streak_and_success_rearms(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persistent bug in this module's own derive bookkeeping must log AT MOST one WARNING
    per consecutive failure streak (not one per poll — ~2880/day at 30s cadence under a
    persistent fault), and a poll that derives successfully re-arms it."""
    caplog.set_level(logging.WARNING)
    clock = _FakeClock(_BASE)
    client = _StaticFakeClient([_item("chrome", "Tab", "ref-1", 0)])
    source = ScreenpipeEventSource(client=client, poll_interval_seconds=30.0, clock=clock)

    call_count = 0
    real_process_one = source._process_one

    def _flaky_process_one(item: ScreenpipeItem, events: list[SourceEvent]) -> None:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise RuntimeError("simulated derive bookkeeping bug")
        real_process_one(item, events)

    monkeypatch.setattr(source, "_process_one", _flaky_process_one)

    clock.set(_BASE + timedelta(seconds=1))
    assert await source.poll() == []
    clock.set(_BASE + timedelta(seconds=2))
    assert await source.poll() == []
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1

    # Third poll: process_one now succeeds (call_count == 3) -> re-arms the warning.
    caplog.clear()
    clock.set(_BASE + timedelta(seconds=3))
    await source.poll()
    assert caplog.records == []

    # Fourth poll: a fresh failure warns again since the prior success re-armed it.
    call_count = 0
    caplog.clear()
    clock.set(_BASE + timedelta(seconds=4))
    assert await source.poll() == []
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


async def test_iss37_m3_truncated_window_breaks_context_run_continuity() -> None:
    """A poll whose result count hits the client's cap means the window was truncated — the
    run being tracked must not carry into the next poll, so two blocks of the SAME app split
    by an unseen gap are never merged into one inflated ``duration_s``."""
    clock = _FakeClock(_BASE + timedelta(seconds=-1))
    truncating_poll_items = [
        _item("editor", "main.py", f"ref-cap-{i}", i) for i in range(_CLIENT_MAX_RESULTS)
    ]
    later_items = [
        _item("editor", "main.py", "ref-later-1", 5000),
        _item("editor", "main.py", "ref-later-2", 5130),
    ]
    client = _WindowedFakeClient([*truncating_poll_items, *later_items])
    source = ScreenpipeEventSource(client=client, poll_interval_seconds=30.0, clock=clock)

    clock.set(_BASE + timedelta(seconds=50))
    truncating_events = await source.poll()
    assert truncating_events == []  # dwell (49s) is under the 120s floor within this window

    clock.set(_BASE + timedelta(seconds=5200))
    payloads = [e.payload for e in await source.poll()]

    switch = next(p for p in payloads if p["event"] == "context_switch")
    # Had continuity NOT been broken, started_at would be the very first item (offset 0) and
    # duration_s would be ~5130s. Breaking it means the second poll starts a fresh run.
    assert switch["started_at"] == (_BASE + timedelta(seconds=5000)).isoformat()
    assert switch["duration_s"] == 130.0


async def test_iss37_m4_none_current_run_skips_emit_with_one_warning_not_a_crash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Calling the emit helpers with no current run must warn and skip rather than raise — the
    previous bare ``assert`` is stripped under ``python -O``, turning a None run into a silent
    ``AttributeError`` (a no-event poll with zero diagnostic trace)."""
    caplog.set_level(logging.WARNING)
    clock = _FakeClock(_BASE)
    source = ScreenpipeEventSource(
        client=_StaticFakeClient([]), poll_interval_seconds=30.0, clock=clock
    )
    assert source._current is None

    events: list[SourceEvent] = []
    source._maybe_emit_context_switch(events)
    source._maybe_emit_focus_block_end(events)

    assert events == []
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


async def test_iss37_m5_search_cursor_only_advances_after_a_successful_search() -> None:
    """If ``client.search`` itself raises (the defensive, pragma-no-cover branch), the poll
    cursor must NOT advance — the next poll retries the SAME window instead of silently losing
    it forever (a screenpipe outage must never cost the missed window)."""
    clock = _FakeClock(_BASE)
    client = _FlakyOnceClient([_item("editor", "main.py", "ref-1", 5)])
    source = ScreenpipeEventSource(client=client, poll_interval_seconds=30.0, clock=clock)

    clock.set(_BASE + timedelta(seconds=10))
    assert await source.poll() == []
    assert client.calls == [(_BASE, _BASE + timedelta(seconds=10))]

    clock.set(_BASE + timedelta(seconds=20))
    await source.poll()
    # The retried window's start is UNCHANGED from the first, failed attempt.
    assert client.calls[1] == (_BASE, _BASE + timedelta(seconds=20))


async def test_iss37_rider_m5_search_cursor_holds_through_a_non_raising_degrade() -> None:
    """The REAL degrade path (DEC-70i): ``client.search`` never raises — it returns ``[]`` and
    signals ``last_search_degraded``. The cursor must hold through THIS shape too, not just the
    defensive raise branch, or a real screenpipe outage silently discards every window it covers."""
    clock = _FakeClock(_BASE)
    client = _DegradedOnceClient([_item("editor", "main.py", "ref-1", 5)])
    source = ScreenpipeEventSource(client=client, poll_interval_seconds=30.0, clock=clock)

    clock.set(_BASE + timedelta(seconds=10))
    assert await source.poll() == []
    assert client.calls == [(_BASE, _BASE + timedelta(seconds=10))]
    assert client.last_search_degraded is True

    clock.set(_BASE + timedelta(seconds=20))
    await source.poll()
    # The retried window's start is UNCHANGED from the first, degraded attempt — the window
    # was never lost.
    assert client.calls[1] == (_BASE, _BASE + timedelta(seconds=20))
    assert client.last_search_degraded is False


# --------------------------------------------------------------------------------------- AC(b)


def _make_queue_item(payload: dict[str, object], source_id: str = "screenpipe") -> QueueItem:
    event_key = f"{payload['event']}:{payload['app']}:{payload['started_at']}"
    return QueueItem(idempotency_key=derive_key(source_id, event_key), payload=payload)


@_requires_pg
def test_ac_b_replayed_timeline_yields_stable_keys_and_dedupe_re_enqueues_nothing(
) -> None:
    assert _DSN is not None
    import psycopg

    with psycopg.connect(_DSN) as conn:
        ensure_seen_events_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE wombat_seen_events")
        conn.commit()

    class _RecordingEnqueuer:
        def __init__(self) -> None:
            self.items: list[QueueItem] = []

        def enqueue(self, item: QueueItem) -> EnqueueResult:
            self.items.append(item)
            return EnqueueResult.QUEUED

    ledger = SeenLedger(_DSN)
    try:
        inner = _RecordingEnqueuer()
        deduping = DedupingEnqueuer(inner, ledger)

        # Run 1: derive over a fresh source instance, enqueue every event.
        run1_payloads = _sync_derive_all()
        for payload in run1_payloads:
            result = deduping.enqueue(_make_queue_item(payload))
            assert result == EnqueueResult.QUEUED
        assert len(inner.items) == 2  # context_switch + focus_block_end

        # Run 2: a FRESH source instance replaying the IDENTICAL timeline (simulating a
        # restart) must derive the SAME event_keys and dedupe to a structural no-op.
        run2_payloads = _sync_derive_all()
        assert [p["event"] for p in run2_payloads] == [p["event"] for p in run1_payloads]
        for payload in run2_payloads:
            result = deduping.enqueue(_make_queue_item(payload))
            assert result == EnqueueResult.ALREADY_QUEUED
        assert len(inner.items) == 2  # unchanged — nothing new landed
    finally:
        ledger.close()


def _sync_derive_all() -> list[dict[str, object]]:
    """Drive a FRESH ``ScreenpipeEventSource`` over the scripted timeline, synchronously (a
    thin ``asyncio.run`` wrapper around ``poll()``, which awaits its client call via
    ``asyncio.to_thread``)."""
    import asyncio

    async def _run() -> list[dict[str, object]]:
        clock = _FakeClock(_BASE + timedelta(seconds=-1))
        client = _WindowedFakeClient(_scripted_timeline())
        source = ScreenpipeEventSource(client=client, poll_interval_seconds=30.0, clock=clock)
        return await _drive_timeline(source, clock)

    return asyncio.run(_run())


# --------------------------------------------------------------------------------------- AC(d)


async def test_ac_d_a_genuinely_degraded_client_yields_no_events_and_never_raises() -> None:
    # A real ScreenpipeClient pointed at a port nothing listens on — its OWN documented
    # degrade contract (DEC-70i) makes search() return [] rather than raise, on every call.
    degraded_client = ScreenpipeClient("http://127.0.0.1:59999")
    clock = _FakeClock(_BASE)
    source = ScreenpipeEventSource(client=degraded_client, poll_interval_seconds=0.01, clock=clock)

    for tick in range(3):
        clock.set(_BASE + timedelta(seconds=tick + 1))
        events = await source.poll()
        assert events == []


async def test_ac_d_degraded_client_keeps_the_registered_poll_loop_alive() -> None:
    import asyncio

    class _FakeEnqueuer:
        def enqueue(self, item: QueueItem) -> EnqueueResult:
            return EnqueueResult.QUEUED

    degraded_client = ScreenpipeClient("http://127.0.0.1:59999")
    source = ScreenpipeEventSource(client=degraded_client, poll_interval_seconds=0.01)
    registry = SourceRegistry(_FakeEnqueuer())
    registry.register(source)

    await registry.start()
    try:
        await asyncio.sleep(0.05)
        assert "screenpipe" not in registry.degraded_sources
    finally:
        await registry.stop()


# --------------------------------------------------------------------------------------- AC(e)


@_requires_pg
def test_ac_e_one_derived_event_scores_screen_activity_and_holds_at_the_real_gate() -> None:
    assert _DSN is not None
    import psycopg

    with psycopg.connect(_DSN) as conn:
        ensure_queue_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE wombat_queue")
        conn.commit()

    payload = {
        "event": "context_switch",
        "app": "editor",
        "title": "main.py — myproject",
        "started_at": (_BASE + timedelta(seconds=30)).isoformat(),
        "duration_s": 130.0,
        "screenpipe_ref": "ref-editor-start",
        "event_class": "screen_activity",
    }
    event_key = f"{payload['event']}:{payload['app']}:{payload['started_at']}"
    queue_item = QueueItem(
        idempotency_key=derive_key("screenpipe", event_key), payload=payload
    )

    queue = WombatQueue(_DSN, max_size=1000)
    try:
        queue.enqueue(queue_item)
        drained = queue.drain()
        assert len(drained) == 1

        gate_item = gate_item_from_queue_item(drained[0])
        # Zero model calls anywhere below — this is the SAME deterministic,
        # model-free scoring path TK-321 already proved (NG-4).
        event_class = resolve_event_class_for_item(gate_item)
        assert event_class is EventClass.SCREEN_ACTIVITY

        params = default_params_for(event_class)
        scored_urgency = urgency(gate_item, params)
        scored = ScoredItem(
            item_id=gate_item.item_id,
            item_kind=gate_item.item_kind,
            urgency=scored_urgency,
            load=0.0,
        )
        urgency_threshold = load_operating_params().urgency_threshold

        assert not is_surfacing_worthy(scored, urgency_threshold)  # HOLD
    finally:
        queue.close()
