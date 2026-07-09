"""TK-28 — stale-item decay, the midnight rollover observation, and the kill-consistency
proof (EP-9, Q-13 split pass b, Q-73).

Un-gated (AC1, AC3i/iii/iv, AC4): fakes/in-memory only, no Postgres — mirrors ``test_trigger.py``
and the TK-24 kill pattern in ``test_pending_set.py`` (``_KillSwitchJournal`` replicated locally
per Q-73's ruling).

Gated (Q-46, AC2 + AC3ii): the real ``DailyLedger``/``CeilingLedger`` atomic-upsert exactly-once
proof and the ledger-side restart proof need a real throwaway Postgres, behind
``WOMBAT_TEST_PG_DSN``:

    docker run --rm -d -p 5522:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5522/postgres
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import psycopg
import pytest

from wombat.domain.daily_ledger import DailyLedger, DailyLedgerRow, ensure_schema, wombat_today
from wombat.gate.ceiling import CeilingLedger
from wombat.gate.decay import DayRollover, LedgerReset, decay_stale
from wombat.gate.models import DecayEvent, GateAction, GateItem, ItemKind, ScoredItem
from wombat.gate.pending_set import (
    InMemoryPendingJournal,
    JournalRecord,
    PendingSet,
)
from wombat.gate.pipeline import Gate
from wombat.gate.trigger import CeilingHit
from wombat.rating.params import EventClass, RatingParams

# --- Shared fakes (mirror test_trigger.py's / test_pending_set.py's local doubles) -----------


def _scored(item_id: str, urgency: float = 0.5, load: float = 0.1) -> ScoredItem:
    return ScoredItem(item_id=item_id, item_kind=ItemKind.GENERIC, urgency=urgency, load=load)


def _gate_item(
    item_id: str, *, sender_class: str = "automated", **payload_extra: object
) -> GateItem:
    payload: dict[str, object] = {"is_timed": False, "sender_class": sender_class}
    payload.update(payload_extra)
    return GateItem(item_id=item_id, item_kind=ItemKind.GENERIC, created_at=0.0, payload=payload)


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


@dataclass
class _CappedCeiling:
    """A real cap (not just allow/deny toggled by hand) — used by the AC4 wake-burst test."""

    cap: int
    _count: int = field(default=0, init=False)

    def allow(self, event_class: EventClass) -> bool:
        return self._count < self.cap

    def record(self, event_class: EventClass) -> None:
        self._count += 1


@dataclass
class _EventRecorder:
    events: list[object] = field(default_factory=list)

    def __call__(self, event: object) -> None:
        self.events.append(event)


class _NoOpRollover:
    """A ``DayRolloverProtocol`` double that never fires — used by tests exercising decay only."""

    def check(self) -> LedgerReset | None:
        return None


@dataclass
class _FakeRollover:
    """A no-pg double satisfying the ``check() -> LedgerReset | None`` Protocol (Q-73: 'pipeline/
    burst tests inject fakes'). Mirrors ``DayRollover``'s exactly-once contract — an in-memory
    last-seen day plus a per-day counter standing in for the durable atomic upsert — over an
    injected ``now`` (aware-datetime) callable, so the AC4 wake-burst test can drive the SAME
    exactly-once shape without a live Postgres.
    """

    now: Callable[[], datetime]
    tz: ZoneInfo
    _last_seen: date | None = field(default=None, init=False)
    _counts: dict[date, int] = field(default_factory=dict, init=False)

    def check(self) -> LedgerReset | None:
        today = wombat_today(self.now(), self.tz)
        if today == self._last_seen:
            return None
        self._last_seen = today
        self._counts[today] = self._counts.get(today, 0) + 1
        if self._counts[today] == 1:
            return LedgerReset(wombat_date=today)
        return None


@dataclass
class _FlakyDailyLedger:
    """A ``DailyLedger`` double whose ``increment()`` raises on its first call, then succeeds
    (TK-169, CR-4). Stands in for a transient pg error on the first ``DayRollover.check()`` of a
    new wombat-day, so tests can prove ``_last_seen`` is stamped only AFTER the durable increment
    lands -- never before.
    """

    today_value: date
    calls: int = field(default=0, init=False)
    _value: int = field(default=0, init=False)

    def today(self) -> date:
        return self.today_value

    def increment(self, ledger_name: str, amount: int = 1) -> DailyLedgerRow:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("simulated transient pg error")
        self._value += amount
        return DailyLedgerRow(
            ledger_name=ledger_name, wombat_date=self.today_value, value=self._value
        )


@dataclass(slots=True)
class _KillSwitchJournal:
    """Replicated locally from ``tests/gate/test_pending_set.py`` (TK-24 precedent, Q-73 ruling):
    wraps a real journal to inject a kill at a specific append. Counts only appends made THROUGH
    this wrapper — appends made before it was swapped in are not counted.
    """

    inner: InMemoryPendingJournal
    raise_after: int | None = None
    raise_before_append: bool = False
    _count: int = field(default=0, init=False)

    def append(self, record: JournalRecord) -> None:
        if self.raise_before_append:
            raise RuntimeError("simulated kill before append lands")
        self.inner.append(record)
        self._count += 1
        if self.raise_after is not None and self._count == self.raise_after:
            raise RuntimeError("simulated kill right after append lands")

    def replay(self) -> tuple[JournalRecord, ...]:
        return tuple(self.inner.replay())


# ================================================================================================
# AC1: an item older than decay_ttl_seconds is removed by decay(); a DecayEvent is emitted; the
# item is NOT surfaced.
# ================================================================================================


def test_ac1_decay_stale_is_pure_removes_only_the_item_over_ttl() -> None:
    journal = InMemoryPendingJournal()
    pending = PendingSet(journal=journal, max_pending=10)
    pending.add(_scored("stale"), added_at=0.0)
    pending.add(_scored("fresh"), added_at=90.0)

    events = decay_stale(pending, now=100.0, decay_ttl_seconds=50.0)

    assert events == (DecayEvent(item_id="stale", age_seconds=100.0),)
    assert {item.item_id for item in pending.list()} == {"fresh"}


def test_ac1_decay_stale_boundary_is_strictly_greater_than_ttl() -> None:
    """Age exactly equal to the ttl does NOT decay (strictly greater, Q-73)."""
    journal = InMemoryPendingJournal()
    pending = PendingSet(journal=journal, max_pending=10)
    pending.add(_scored("at-boundary"), added_at=50.0)

    events = decay_stale(pending, now=100.0, decay_ttl_seconds=50.0)

    assert events == ()
    assert len(pending) == 1


def test_ac1_decay_removes_through_the_journaled_remove_path_not_a_bare_pop() -> None:
    """The removal is journaled (RISK-5): replaying the journal after a decay never resurrects
    the decayed item."""
    journal = InMemoryPendingJournal()
    pending = PendingSet(journal=journal, max_pending=10)
    pending.add(_scored("stale"), added_at=0.0)

    decay_stale(pending, now=1000.0, decay_ttl_seconds=1.0)

    rebuilt = PendingSet.rebuild_from_journal(journal, max_pending=10)
    assert rebuilt.list() == []


async def test_ac1_gate_decay_removes_stale_item_emits_decay_event_and_it_is_never_surfaced() -> (
    None
):
    rating_params = RatingParams(urgency_base=0.9, urgency_gain=0.0, load_base=0.0, load_gain=0.0)
    pending_set = PendingSet(journal=InMemoryPendingJournal(), max_pending=10)
    pending_set.add(_scored("stale", urgency=0.9), added_at=0.0)
    recorder = _EventRecorder()
    gate = Gate(
        user_model=_FakeUserModel(rating_params=rating_params),
        pending_set=pending_set,
        ceiling=_FakeCeiling(allowed=True),
        urgency_threshold=0.1,
        load_flush_threshold=10.0,
        flush_min_age_seconds=100.0,
        decay_ttl_seconds=50.0,
        day_rollover=_NoOpRollover(),
        clock=lambda: 100.0,
        on_event=recorder,
    )

    decision = await gate.pipeline([])  # a heartbeat tick — no new item, decay still runs

    assert decision.action is GateAction.HOLD
    decay_events = [e for e in recorder.events if isinstance(e, DecayEvent)]
    assert decay_events == [DecayEvent(item_id="stale", age_seconds=100.0)]
    assert len(pending_set) == 0
    assert decision.items == ()  # never surfaced


async def test_gate_decay_method_returns_the_same_events_it_emits() -> None:
    rating_params = RatingParams(urgency_base=0.5, urgency_gain=0.0, load_base=0.0, load_gain=0.0)
    pending_set = PendingSet(journal=InMemoryPendingJournal(), max_pending=10)
    pending_set.add(_scored("stale"), added_at=0.0)
    recorder = _EventRecorder()
    gate = Gate(
        user_model=_FakeUserModel(rating_params=rating_params),
        pending_set=pending_set,
        ceiling=_FakeCeiling(allowed=True),
        urgency_threshold=0.9,
        load_flush_threshold=10.0,
        flush_min_age_seconds=100.0,
        decay_ttl_seconds=1.0,
        day_rollover=_NoOpRollover(),
        clock=lambda: 1000.0,
        on_event=recorder,
    )

    returned = gate.decay()

    assert returned == (DecayEvent(item_id="stale", age_seconds=1000.0),)
    assert recorder.events == list(returned)


# ================================================================================================
# TK-169 (CR-4): DayRollover must stamp ``_last_seen`` only AFTER the durable increment succeeds.
# A transient increment() failure on the first check() of a new wombat-day must not short-circuit
# every later check() that day at the ``today == self._last_seen`` guard -- the next check() is
# the retry. Un-gated: a local double stands in for the real DailyLedger.
# ================================================================================================


def test_tk169_last_seen_stamped_only_after_increment_succeeds_next_check_retries() -> None:
    today = date(2026, 7, 8)
    ledger = _FlakyDailyLedger(today_value=today)
    rollover = DayRollover(daily_ledger=ledger)  # type: ignore[arg-type]  # duck-typed double

    with pytest.raises(RuntimeError):
        rollover.check()

    assert ledger.calls == 1  # the failed increment was attempted...

    # ...but _last_seen was NOT stamped: the very next check() (still the same day) must be a
    # real retry -- not a silent no-op swallowed by the `today == self._last_seen` guard.
    result = rollover.check()

    assert ledger.calls == 2  # retried, not skipped
    assert isinstance(result, LedgerReset)
    assert result.wombat_date == today

    # a THIRD check() the same day is quiet -- the in-memory fast path now works normally, and
    # the retry above did not cause a double-emit.
    assert rollover.check() is None
    assert ledger.calls == 2  # short-circuited by _last_seen; no further increment call


# ================================================================================================
# AC2 (DSN-gated, real upsert): crossing the wombat-day boundary resets per-class counts to 0
# (structural) and emits LedgerReset exactly once — slept-through-boundary fires once; a
# mid-day restart does not double-reset.
# ================================================================================================

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping the TK-28 real-Postgres exactly-once/"
        "kill-consistency tests. Start one with:\n"
        "  docker run --rm -d -p 5522:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5522/postgres"
    ),
)


@pytest.fixture
def clean_table() -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE daily_ledger")
        conn.commit()


@_requires_pg
def test_ac2_ceiling_counts_reset_structurally_on_a_new_wombat_date(clean_table: None) -> None:
    assert _DSN is not None
    tz = ZoneInfo("UTC")
    instant = [datetime(2026, 7, 2, 23, 0, tzinfo=UTC)]
    daily_ledger = DailyLedger(_DSN, tz=tz, clock=lambda: instant[0])
    try:
        ceiling = CeilingLedger(daily_ledger=daily_ledger, per_class_daily_ceiling=2)
        event_class = EventClass.GENERIC
        ceiling.record(event_class)
        ceiling.record(event_class)
        assert ceiling.allow(event_class) is False  # at ceiling on day 1

        instant[0] = datetime(2026, 7, 3, 0, 5, tzinfo=UTC)  # cross into the new wombat-day
        assert ceiling.allow(event_class) is True  # fresh row -> count is 0 again
    finally:
        daily_ledger.close()


@_requires_pg
def test_ac2_slept_through_boundary_fires_reset_exactly_once_then_stays_quiet(
    clean_table: None,
) -> None:
    assert _DSN is not None
    tz = ZoneInfo("UTC")
    instant = [datetime(2026, 7, 2, 12, 0, tzinfo=UTC)]
    daily_ledger = DailyLedger(_DSN, tz=tz, clock=lambda: instant[0])
    try:
        rollover = DayRollover(daily_ledger=daily_ledger)

        first = rollover.check()
        assert isinstance(first, LedgerReset)
        assert first.wombat_date == date(2026, 7, 2)

        # ticking again the SAME day -> quiet (in-memory fast path, no re-emit).
        assert rollover.check() is None

        # sleep across midnight into the next wombat-day.
        instant[0] = datetime(2026, 7, 3, 0, 5, tzinfo=UTC)
        second = rollover.check()
        assert isinstance(second, LedgerReset)
        assert second.wombat_date == date(2026, 7, 3)

        # ticking again on the new day -> quiet.
        assert rollover.check() is None
    finally:
        daily_ledger.close()


@_requires_pg
def test_ac2_mid_day_restart_does_not_double_reset(clean_table: None) -> None:
    assert _DSN is not None
    tz = ZoneInfo("UTC")
    fixed_instant = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)
    daily_ledger = DailyLedger(_DSN, tz=tz, clock=lambda: fixed_instant)
    try:
        first_process_rollover = DayRollover(daily_ledger=daily_ledger)
        first = first_process_rollover.check()
        assert isinstance(first, LedgerReset)

        # Simulate a mid-day restart: a FRESH DayRollover (in-memory last-seen lost), same
        # durable daily_ledger row already at value=1 for today.
        restarted_rollover = DayRollover(daily_ledger=daily_ledger)
        second = restarted_rollover.check()
        assert second is None  # already recorded for today -- no double-reset

        row = daily_ledger.current_row("rollover:gate")
        assert row.value == 2  # both increments landed durably; only the first emitted
    finally:
        daily_ledger.close()


# ================================================================================================
# AC3: kill-consistency (Q-73's four concretized invariants)
# ================================================================================================


# --- (i) pending-journal replay NEVER touches the ceiling — rebuild leaves the day count
#         bit-identical (DSN-gated: the ceiling side is real pg) --------------------------------


@_requires_pg
def test_ac3i_pending_journal_rebuild_never_touches_the_ceiling(clean_table: None) -> None:
    assert _DSN is not None
    tz = ZoneInfo("UTC")
    daily_ledger = DailyLedger(_DSN, tz=tz, clock=lambda: datetime(2026, 7, 2, 12, 0, tzinfo=UTC))
    try:
        ceiling = CeilingLedger(daily_ledger=daily_ledger, per_class_daily_ceiling=5)
        event_class = EventClass.GENERIC
        ceiling.record(event_class)
        ceiling.record(event_class)
        before = daily_ledger.current_row(f"ceiling:{event_class.value}").value

        journal = InMemoryPendingJournal()
        pending = PendingSet(journal=journal, max_pending=10)
        pending.add(_scored("a"), added_at=0.0)
        pending.remove("a")
        PendingSet.rebuild_from_journal(journal, max_pending=10)  # touches nothing but itself

        after = daily_ledger.current_row(f"ceiling:{event_class.value}").value
        assert after == before  # bit-identical: the pending-journal rebuild never touched it
    finally:
        daily_ledger.close()


# --- (ii) restart never refreshes the day budget — exhaust to N, "kill", rebuild, assert
#          post-restart surfacings <= ceiling - N (DSN-gated: the ceiling side is real pg) ------


@_requires_pg
async def test_ac3ii_restart_never_refreshes_the_day_budget(clean_table: None) -> None:
    assert _DSN is not None
    tz = ZoneInfo("UTC")
    fixed_instant = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)
    daily_ledger = DailyLedger(_DSN, tz=tz, clock=lambda: fixed_instant)
    try:
        ceiling = CeilingLedger(daily_ledger=daily_ledger, per_class_daily_ceiling=2)
        rating_params = RatingParams(
            urgency_base=0.9, urgency_gain=0.0, load_base=0.0, load_gain=0.0
        )

        def _make_gate() -> Gate:
            # A fresh Gate + fresh in-memory pending_set every call, models "restart": only the
            # PG-BACKED ceiling survives across the boundary — that's the object under test.
            return Gate(
                user_model=_FakeUserModel(rating_params=rating_params),
                pending_set=PendingSet(journal=InMemoryPendingJournal(), max_pending=10),
                ceiling=ceiling,
                urgency_threshold=0.1,
                load_flush_threshold=10.0,
                flush_min_age_seconds=100.0,
                decay_ttl_seconds=float("inf"),
                day_rollover=_NoOpRollover(),
                clock=lambda: 1000.0,
            )

        gate = _make_gate()
        d1 = await gate.pipeline([_gate_item("a")])
        d2 = await gate.pipeline([_gate_item("b")])
        assert d1.action is GateAction.SURFACE_IMMEDIATE
        assert d2.action is GateAction.SURFACE_IMMEDIATE  # ceiling exhausted: N=2 of 2

        # KILL + restart: a brand new Gate instance, but the SAME durable pg-backed ceiling.
        restarted_gate = _make_gate()
        d3 = await restarted_gate.pipeline([_gate_item("c")])

        assert d3.action is GateAction.HOLD  # ceiling(2) - N(2) == 0 remaining: never refreshed
    finally:
        daily_ledger.close()


# --- (iii) surface path books ceiling only / hold path adds pending only — structurally disjoint
#           (pure, un-gated) ---------------------------------------------------------------------


async def test_ac3iii_surface_path_books_ceiling_only_hold_path_adds_pending_only() -> None:
    rating_params = RatingParams(urgency_base=0.9, urgency_gain=0.0, load_base=0.0, load_gain=0.0)
    ceiling = _FakeCeiling(allowed=True)
    pending_set = PendingSet(journal=InMemoryPendingJournal(), max_pending=10)
    gate = Gate(
        user_model=_FakeUserModel(rating_params=rating_params),
        pending_set=pending_set,
        ceiling=ceiling,
        urgency_threshold=0.1,
        load_flush_threshold=10.0,
        flush_min_age_seconds=100.0,
        decay_ttl_seconds=float("inf"),
        day_rollover=_NoOpRollover(),
        clock=lambda: 1000.0,
    )

    surfaced = await gate.pipeline([_gate_item("surface-me")])
    assert surfaced.action is GateAction.SURFACE_IMMEDIATE
    assert ceiling.recorded == [EventClass.GENERIC]  # surface path booked the ceiling
    assert len(pending_set) == 0  # ...and never touched pending

    ceiling.allowed = False  # now the ceiling denies -> the SAME item class falls through to hold
    held = await gate.pipeline([_gate_item("hold-me")])
    assert held.action is GateAction.HOLD
    assert ceiling.recorded == [EventClass.GENERIC]  # unchanged: hold path never books the ceiling
    assert len(pending_set) == 1  # ...and only the hold path adds to pending
    assert pending_set.list()[0].item_id == "hold-me"


# --- (iv) decay kill points via the TK-24 _KillSwitchJournal pattern (replicated locally,
#          un-gated in-memory journal) -------------------------------------------------------------


def test_ac3iv_decay_kill_after_k_of_n_removes_converges_on_rebuild() -> None:
    seed = InMemoryPendingJournal()
    pending = PendingSet(journal=seed, max_pending=10)
    pending.add(_scored("a"), added_at=0.0)
    pending.add(_scored("b"), added_at=0.0)
    pending.add(_scored("c"), added_at=0.0)

    # Swap in the kill-switch AFTER the seed adds (mirrors test_pending_set.py's pattern): the
    # killer counts only appends made through it. raise_after=2 kills right after the SECOND
    # decay-remove (of 3) lands durably, before the third is attempted.
    killer = _KillSwitchJournal(inner=seed, raise_after=2)
    pending._journal = killer

    with pytest.raises(RuntimeError):
        decay_stale(pending, now=1000.0, decay_ttl_seconds=1.0)

    rebuilt = PendingSet.rebuild_from_journal(seed, max_pending=10)
    rebuilt_ids = {item.item_id for item in rebuilt.list()}
    # decay_stale processes items in the snapshot's (insertion) order a, b, c: the first two
    # removes (a, b) commit durably before the kill fires; c's remove is never attempted.
    assert rebuilt_ids == {"c"}  # removed items (a, b) stay removed; c is untouched, not lost

    # the survivor kept its ORIGINAL added_at (0.0) -- it was never touched by the killed pass.
    assert rebuilt.oldest_added_at() == 0.0

    # convergence: the survivor decays cleanly on the very next tick.
    next_events = decay_stale(rebuilt, now=1000.0, decay_ttl_seconds=1.0)
    assert next_events == (DecayEvent(item_id="c", age_seconds=1000.0),)
    assert len(rebuilt) == 0


# ================================================================================================
# AC4: wake-burst — ONE pipeline() call PER backlog item (the honest as-built runtime shape,
# Q-73). Un-gated: a shared fake time source + fakes for ceiling/rollover (Q-73 sanctions fakes
# for the burst test; the exactly-once PG proof itself lives in AC2/AC3ii).
# ================================================================================================


async def test_ac4_wake_burst_bounds_at_ceiling_resets_once_and_decays_pre_sleep_items() -> None:
    tz = ZoneInfo("UTC")
    clock_state = {"now": datetime(2026, 7, 2, 23, 0, tzinfo=UTC)}

    def _now() -> datetime:
        return clock_state["now"]

    def _epoch() -> float:
        return clock_state["now"].timestamp()

    pending_set = PendingSet(journal=InMemoryPendingJournal(), max_pending=50)
    # Seed pre-sleep pending items at the pre-sleep instant.
    pending_set.add(_scored("pre-sleep-1", urgency=0.05), added_at=_epoch())
    pending_set.add(_scored("pre-sleep-2", urgency=0.05), added_at=_epoch())

    rating_params = RatingParams(urgency_base=0.9, urgency_gain=0.0, load_base=0.0, load_gain=0.0)
    ceiling_cap = 2
    ceiling = _CappedCeiling(cap=ceiling_cap)
    recorder = _EventRecorder()
    rollover = _FakeRollover(now=_now, tz=tz)
    decay_ttl_seconds = 3600.0  # 1 hour

    gate = Gate(
        user_model=_FakeUserModel(rating_params=rating_params, event_class=EventClass.GENERIC),
        pending_set=pending_set,
        ceiling=ceiling,
        urgency_threshold=0.1,
        load_flush_threshold=1000.0,  # never trips the flush arm in this test
        flush_min_age_seconds=100000.0,
        decay_ttl_seconds=decay_ttl_seconds,
        day_rollover=rollover,
        clock=_epoch,
        on_event=recorder,
    )

    # The "wake": jump the shared clock past BOTH midnight and the decay ttl.
    clock_state["now"] = datetime(2026, 7, 3, 6, 0, tzinfo=UTC)

    k = 5  # K > ceiling_cap
    decisions = [await gate.pipeline([_gate_item(f"burst-{i}")]) for i in range(k)]

    surfaced = [d for d in decisions if d.action is GateAction.SURFACE_IMMEDIATE]
    held = [d for d in decisions if d.action is GateAction.HOLD]
    assert len(surfaced) == ceiling_cap  # the burst cannot exceed the ceiling
    assert len(held) == k - ceiling_cap

    ceiling_hits = [e for e in recorder.events if isinstance(e, CeilingHit)]
    assert len(ceiling_hits) == k - ceiling_cap

    ledger_resets = [e for e in recorder.events if isinstance(e, LedgerReset)]
    assert len(ledger_resets) == 1  # exactly once across the whole burst
    assert ledger_resets[0].wombat_date == date(2026, 7, 3)

    decay_events = [e for e in recorder.events if isinstance(e, DecayEvent)]
    assert {e.item_id for e in decay_events} == {"pre-sleep-1", "pre-sleep-2"}
    assert len(decay_events) == 2  # emitted exactly once each, on the first call

    # pre-sleep items appear in NO decision anywhere across the burst.
    all_decided_ids = {scored.item_id for d in decisions for scored in d.items}
    assert "pre-sleep-1" not in all_decided_ids
    assert "pre-sleep-2" not in all_decided_ids
