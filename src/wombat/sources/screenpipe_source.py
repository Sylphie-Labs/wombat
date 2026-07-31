"""wombat.sources.screenpipe_source — ScreenpipeEventSource (TK-322, EP-37, DEC-70a/e/f).

Implements the poll-only ``InputSource`` contract (``sources/base.py``) over the injected
``ScreenpipeClient`` (``integrations.screenpipe.client``, TK-320) — registration-not-rewrite
(DEC-5): the registry (``sources/registry.py``) is completely untouched.

Each ``poll()`` queries ``client.search(start, end)`` for OCR-derived foreground-window
snapshots observed since the previous poll (``start`` advances to the previous poll's ``end``
every tick; the very first query starts at construction time, so boot never triggers a
since-forever query). Snapshots are merged, in ``captured_at`` order, into a single running
"current context" (``app``, ``title``) tracked ACROSS poll calls — a context's observed
duration is the span between the FIRST and LAST sample seen for it (poll-cadence-bounded, never
a claim of exact wall-clock precision beyond what was actually sampled).

CLOSED v1 TAXONOMY (DEC-70a/e), exactly two kinds, both deterministic:

  * ``context_switch`` — fires ONCE, the moment a (still-ongoing) context's observed dwell
    first reaches ``_MIN_DWELL_S`` (pinned 120s). A context that never reaches this dwell
    (alt-tab flapping) never fires anything — this is the structural flood guard.
  * ``focus_block_end`` — fires ONCE, when a context that had been sustained
    >= ``_FOCUS_BLOCK_MIN_S`` (pinned 1500s) is superseded by a different context. The two
    kinds are independent: a long-sustained context can (and typically does) emit BOTH — one
    ``context_switch`` shortly after it starts, and one ``focus_block_end`` when it ends.

PAYLOAD BOUNDED (DEC-70f) — exactly ``{event, app, title, started_at, duration_s,
screenpipe_ref, event_class}``. ``title`` is char-capped at ``_MAX_TITLE_CHARS`` (160).
``screenpipe_ref`` is the POINTER id of the sample that opened this context run — never OCR
text, never any other content field. ``event_class`` is always the literal ``"screen_activity"``
(TK-321's ``EventClass.SCREEN_ACTIVITY`` resolves via the existing payload-key override path,
Q-41 ruling 1 — zero gate/user_model code change). Deliberately NO ``item_kind`` key: the event
enters the queue -> gate exactly like every other GENERIC item (``gate_item_from_queue_item``'s
existing missing-key fallback), no special-casing, no bypass.

``event_key`` derives from ``(kind, app, started_at)`` (an f-string join, started_at rendered
via ``isoformat()``) so a replayed timeline yields the SAME keys every time — the registry's
``idempotency_key(source_id, event_key)`` (TK-12) plus the ``SeenLedger``/``DedupingEnqueuer``
(TK-286) dedupe path holds across a restart, exactly like every other source.

DEGRADE: ``ScreenpipeClient.search`` is documented to never raise (DEC-70i — a down/misconfigured
screenpipe degrades to ``[]`` after its own one-WARNING-per-streak). ``poll()`` ALSO wraps its own
merge/derive logic in a blanket guard so a bug in this module's own bookkeeping can never raise
into the registry's poll loop either (CRF-4's enqueue guard is a different, downstream layer) —
belt-and-suspenders, mirroring ``ASRSource``'s scan-level guard.

``client.search`` is synchronous (blocking urllib under its 2.0s timeout) and is therefore
offloaded via ``asyncio.to_thread`` rather than awaited directly — the same anti-precedent
``ASRSource`` (``sources/asr.py``) already established: a synchronous call awaited inline would
freeze the whole shared event loop for up to its timeout on every poll, starving co-tenant
coroutines (voice/ASR/chat).

The running context is keyed on ``app`` ALONE (not ``(app, title)``): ordinary window-title
churn (notification counts, unsaved markers, a clock in the tab title) must not reset dwell
tracking or block ``focus_block_end`` from ever firing. ``title`` is carried as a payload
attribute of the current run — it is updated to the most recently observed title for the
same app — never part of the run's identity.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from wombat.integrations.screenpipe.client import _MAX_RESULTS as _SEARCH_RESULT_CAP
from wombat.integrations.screenpipe.client import ScreenpipeItem
from wombat.sources.base import SourceEvent

logger = logging.getLogger(__name__)

# DEC-63 no-knob pins — module-private, no ticket has asked for an operator-facing tunable.
_MIN_DWELL_S = 120.0
_FOCUS_BLOCK_MIN_S = 1500.0
_MAX_TITLE_CHARS = 160

# ISS-37-RIDER batch-review repair (m5+m3 composition): a PERMANENTLY degraded client never
# advances ``_search_from`` on its own (the cursor-hold behavior below), so the retried window
# would otherwise grow without bound across a long outage. Bounded outage recovery — once
# ``now - _search_from`` exceeds this, the cursor is advanced to ``now - _MAX_RETRY_WINDOW_S``
# instead of holding forever, so the NEXT successful poll only ever re-scans a bounded 15-minute
# tail, never the whole outage.
_MAX_RETRY_WINDOW_S = 900.0

_EVENT_CLASS = "screen_activity"


def _utc_now() -> datetime:
    """The real-clock default (mirrors every other source's own ``_utc_now`` default)."""
    return datetime.now(UTC)


class ScreenpipeClientLike(Protocol):
    """The one ``ScreenpipeClient`` method this source needs (mirrors ``asr.Transcriber``/
    ``seen_ledger.SeenLedgerLike``'s minimal-seam convention) — lets tests inject a fake/
    scripted client; production always wires the real ``ScreenpipeClient``."""

    def search(
        self,
        start: datetime,
        end: datetime,
        *,
        app_name: str | None = None,
        limit: int | None = None,
    ) -> list[ScreenpipeItem]: ...


class _ContextRun:
    """Internal bookkeeping for the context CURRENTLY being tracked across poll calls."""

    __slots__ = ("app", "last_seen_at", "ref", "started_at", "switch_emitted", "title")

    def __init__(self, app: str, title: str, ref: str, started_at: datetime) -> None:
        self.app = app
        self.title = title
        self.ref = ref
        self.started_at = started_at
        self.last_seen_at = started_at
        self.switch_emitted = False

    def key(self) -> str:
        """Identity is ``app`` alone (title churn must never reset dwell tracking)."""
        return self.app

    def dwell_s(self) -> float:
        return (self.last_seen_at - self.started_at).total_seconds()


class ScreenpipeEventSource:
    """Derives the closed ``context_switch``/``focus_block_end`` taxonomy from an injected
    ``ScreenpipeClient``'s foreground-window snapshots (TK-322). See the module docstring for
    the full design."""

    id: str = "screenpipe"

    def __init__(
        self,
        *,
        client: ScreenpipeClientLike,
        poll_interval_seconds: float,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.poll_interval_seconds = poll_interval_seconds
        self._client = client
        self._clock = clock
        self._search_from = clock()
        self._current: _ContextRun | None = None
        self._derive_failure_warned = False

    async def start(self) -> None:
        """No lifecycle setup needed — the injected client is already constructed."""
        return None

    async def stop(self) -> None:
        """No lifecycle teardown needed."""
        return None

    async def poll(self) -> list[SourceEvent]:
        """Query for activity since the previous poll and derive zero or more events.

        NEVER raises: ``client.search`` already degrades to ``[]`` on its own (DEC-70i); this
        method's own merge/derive bookkeeping is additionally wrapped so a bug here degrades to
        ``[]`` rather than killing this source's poll loop (the registry's CRF-4 guard is a
        separate, downstream layer for the enqueue step, not a substitute for this one).

        ISS-37-RIDER m5: ``_search_from`` advances ONLY when this poll's ``search`` call was
        NOT degraded. The exception branch below is the belt-and-suspenders defensive path
        (pragma-no-cover — the real ``ScreenpipeClient.search`` never raises, DEC-70i); the
        REAL degrade path is ``search`` returning ``[]`` normally while permanently degraded or
        mid-outage. ``ScreenpipeClient`` exposes this via ``last_search_degraded`` (set on every
        call); a client that doesn't expose it (older/minimal test doubles) is treated as never
        degraded, preserving prior behavior. Either way the SAME window is retried next beat
        instead of being silently discarded forever.
        """
        now = self._clock()
        start = self._search_from
        try:
            items = await asyncio.to_thread(self._client.search, start, now)
        except Exception:  # pragma: no cover - client.search is documented to never raise
            logger.warning(
                "screenpipe source: client.search raised unexpectedly — degrading this poll "
                "to no events",
                exc_info=True,
            )
            self._clamp_retry_window(now)
            return []
        degraded = getattr(self._client, "last_search_degraded", False)
        if not degraded:
            self._search_from = now
        else:
            self._clamp_retry_window(now)
            logger.debug(
                "screenpipe source: search degraded for window %s-%s — retrying the same "
                "window next poll instead of advancing the cursor",
                start,
                now,
            )

        # ISS-37 m3: a result count at the client's cap means this window was truncated — the
        # true tail is unseen, so continuity across the boundary is broken (see `_derive`).
        truncated = len(items) == _SEARCH_RESULT_CAP
        try:
            events = self._derive(items, truncated=truncated)
        except Exception:
            self._warn_derive_failure_once()
            return []
        self._derive_failure_warned = False
        return events

    def _clamp_retry_window(self, now: datetime) -> None:
        """ISS-37-RIDER m5+m3 composition (batch-review repair): a permanently degraded client
        never advances ``_search_from`` on its own, so a persistent outage would otherwise let the
        retried window (``now - _search_from``) grow without bound. Bounded outage recovery — the
        window is held through an ordinary retry, but once the gap exceeds ``_MAX_RETRY_WINDOW_S``
        (15 min) the cursor is advanced to ``now - _MAX_RETRY_WINDOW_S`` so the NEXT successful
        poll only ever re-scans a bounded tail rather than the entire outage."""
        if (now - self._search_from).total_seconds() > _MAX_RETRY_WINDOW_S:
            self._search_from = now - timedelta(seconds=_MAX_RETRY_WINDOW_S)

    def _derive(self, items: list[ScreenpipeItem], *, truncated: bool) -> list[SourceEvent]:
        events: list[SourceEvent] = []
        for item in sorted(items, key=lambda i: i.captured_at):
            self._process_one(item, events)
        if truncated:
            # ISS-37 m3: never merge a future poll's items into this run across an unseen gap.
            # Accepted cost: a missed genuine long block, never an inflated duration_s.
            self._current = None
        return events

    def _warn_derive_failure_once(self) -> None:
        """ISS-37 m2: AT MOST one WARNING per consecutive derive-failure streak (mirrors
        ``ScreenpipeClient``'s own ``_warn_once``/``_rearm`` posture, DEC-70i) — a persistent
        bookkeeping fault at a 30s poll cadence must not log ~2880 tracebacks/day; a poll that
        derives successfully (``poll`` resets ``_derive_failure_warned``) re-arms this warning."""
        if not self._derive_failure_warned:
            logger.warning(
                "screenpipe source: event derivation raised unexpectedly — degrading this "
                "poll to no events; further consecutive failures stay silent until a "
                "successful poll re-arms this warning",
                exc_info=True,
            )
            self._derive_failure_warned = True

    def _process_one(self, item: ScreenpipeItem, events: list[SourceEvent]) -> None:
        if self._current is None:
            self._current = _ContextRun(item.app, item.title, item.ref_id, item.captured_at)
            return
        if self._current.key() == item.app:
            self._current.last_seen_at = item.captured_at
            self._current.title = item.title
            self._maybe_emit_context_switch(events)
            return
        # A different context has appeared — the run just being tracked has ended.
        self._maybe_emit_context_switch(events)
        self._maybe_emit_focus_block_end(events)
        self._current = _ContextRun(item.app, item.title, item.ref_id, item.captured_at)

    def _maybe_emit_context_switch(self, events: list[SourceEvent]) -> None:
        run = self._current
        if run is None:
            # ISS-37 m4: an explicit guard, not a bare assert — under `python -O` an assert is
            # stripped and a None run would instead raise AttributeError below, silently
            # dropping this poll's events. Skip the emit with one loud warning instead.
            logger.warning(
                "screenpipe source: _maybe_emit_context_switch called with no current run — "
                "skipping this emit (internal bookkeeping bug)"
            )
            return
        if run.switch_emitted:
            return
        if run.dwell_s() >= _MIN_DWELL_S:
            events.append(self._build_event("context_switch", run, run.dwell_s()))
            run.switch_emitted = True

    def _maybe_emit_focus_block_end(self, events: list[SourceEvent]) -> None:
        run = self._current
        if run is None:
            # ISS-37 m4: see _maybe_emit_context_switch above.
            logger.warning(
                "screenpipe source: _maybe_emit_focus_block_end called with no current run — "
                "skipping this emit (internal bookkeeping bug)"
            )
            return
        duration = run.dwell_s()
        if duration >= _FOCUS_BLOCK_MIN_S:
            events.append(self._build_event("focus_block_end", run, duration))

    @staticmethod
    def _build_event(kind: str, run: _ContextRun, duration_s: float) -> SourceEvent:
        started_at_iso = run.started_at.isoformat()
        payload = {
            "event": kind,
            "app": run.app,
            "title": run.title[:_MAX_TITLE_CHARS],
            "started_at": started_at_iso,
            "duration_s": duration_s,
            "screenpipe_ref": run.ref,
            "event_class": _EVENT_CLASS,
        }
        event_key = f"{kind}:{run.app}:{started_at_iso}"
        return SourceEvent(event_key=event_key, payload=payload)


__all__ = ["ScreenpipeClientLike", "ScreenpipeEventSource"]
