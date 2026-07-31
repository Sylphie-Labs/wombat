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
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from wombat.integrations.screenpipe.client import ScreenpipeItem
from wombat.sources.base import SourceEvent

logger = logging.getLogger(__name__)

# DEC-63 no-knob pins — module-private, no ticket has asked for an operator-facing tunable.
_MIN_DWELL_S = 120.0
_FOCUS_BLOCK_MIN_S = 1500.0
_MAX_TITLE_CHARS = 160

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

    def key(self) -> tuple[str, str]:
        return (self.app, self.title)

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
        """
        now = self._clock()
        start = self._search_from
        self._search_from = now
        try:
            items = self._client.search(start, now)
        except Exception:  # pragma: no cover - client.search is documented to never raise
            logger.warning(
                "screenpipe source: client.search raised unexpectedly — degrading this poll "
                "to no events",
                exc_info=True,
            )
            return []

        try:
            return self._derive(items)
        except Exception:
            logger.warning(
                "screenpipe source: event derivation raised unexpectedly — degrading this "
                "poll to no events",
                exc_info=True,
            )
            return []

    def _derive(self, items: list[ScreenpipeItem]) -> list[SourceEvent]:
        events: list[SourceEvent] = []
        for item in sorted(items, key=lambda i: i.captured_at):
            self._process_one(item, events)
        return events

    def _process_one(self, item: ScreenpipeItem, events: list[SourceEvent]) -> None:
        context_key = (item.app, item.title)
        if self._current is None:
            self._current = _ContextRun(item.app, item.title, item.ref_id, item.captured_at)
            return
        if self._current.key() == context_key:
            self._current.last_seen_at = item.captured_at
            self._maybe_emit_context_switch(events)
            return
        # A different context has appeared — the run just being tracked has ended.
        self._maybe_emit_context_switch(events)
        self._maybe_emit_focus_block_end(events)
        self._current = _ContextRun(item.app, item.title, item.ref_id, item.captured_at)

    def _maybe_emit_context_switch(self, events: list[SourceEvent]) -> None:
        run = self._current
        assert run is not None
        if run.switch_emitted:
            return
        if run.dwell_s() >= _MIN_DWELL_S:
            events.append(self._build_event("context_switch", run, run.dwell_s()))
            run.switch_emitted = True

    def _maybe_emit_focus_block_end(self, events: list[SourceEvent]) -> None:
        run = self._current
        assert run is not None
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
