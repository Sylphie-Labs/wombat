"""wombat.devices.biometric_events — BiometricEventSource (TK-348, DEC-80(d)).

DEC-68(e) ruled that observations enter no queue and trigger no surfacing. DEC-80(d) grants ONE
named, bounded carve-out: exactly THREE deterministic biometric event kinds may enter the EXISTING
queue and be rated by the EXISTING gate, with default ratings that score BELOW the shipped
threshold so the DEFAULT POSTURE IS HOLD-SILENTLY. This module is a vocabulary addition, never a
gate change — widening the set to a fourth kind is a NEW decision, not an implementation choice.

POLL-SHAPED, NOT PUSH (build ruling A): mirrors ``sources.screenpipe_source.ScreenpipeEventSource``
exactly — a plain ``InputSource``-shaped class (``sources/base.py`` stays byte-untouched) whose
``poll()`` reads a trailing window from the injected ``ObservationStore``-shaped collaborator and
derives zero or more ``SourceEvent``.

READ: ``observations.get_window('biometric', start, now)`` over a trailing ``_LOOKBACK_DAYS = 21``
window (the SAME retention pin ``behavior.stages.dream_biometrics`` already reads against). Row
shape (``devices.biometric_ingest``, byte-untouched here): ``{'kind', 'payload', 'started_at',
'day_key'}``.

CLOSED VOCABULARY (build ruling C, DEC-63 no-knob — module constants, never config/operator-
tunable):

  (1) ``workout_ended`` — one per ``'workout'`` row observed in the window. ``event_key =
      'workout_ended:' + started_at.isoformat()``.
  (2) ``resting_hr_out_of_band`` — a ``'resting_hr_daily'`` row whose ``payload['bpm']`` differs
      from the trailing baseline (mean bpm across the OTHER distinct ``day_key``s in the window,
      only computed with >= ``_MIN_RESTING_HR_DAYS = 5`` of them; fewer means no event, ever) by
      more than ``_RESTING_HR_BAND_BPM = 7``. ``event_key = 'resting_hr_out_of_band:' + day_key``.
  (3) ``sleep_debt_crossed`` — the summed shortfall of ``payload['asleep_minutes']`` against the
      baseline nightly average (mean across every distinct night in the window, only computed
      with >= ``_MIN_SLEEP_NIGHTS = 5`` of them; fewer means no event, ever) across the most
      recent ``_SLEEP_DEBT_NIGHTS = 3`` distinct nights, exceeding ``_SLEEP_DEBT_THRESHOLD_MINUTES
      = 180``. ``event_key = 'sleep_debt_crossed:' + <most recent of the 3 nights>.day_key``.

No fourth kind. No watchlist, no threshold configuration surface, no new config field.

PAYLOAD (build ruling D): every event's payload carries ``'event_class': 'biometric'`` (the
payload-key override path ``user_model.resolve_event_class_for_item`` already honours — exactly
how ``screenpipe_source.py`` sets ``'screen_activity'``; ``user_model.py`` needs no edit),
``'kind'`` (one of the three strings above), and ONLY closed numeric fields drawn straight from
the source row that anchors the event (plus, for ``workout_ended``, the row's closed ``activity``
enum) — never a derived/computed field, never free text. Critically, the payload MUST NOT set
``'is_timed'`` or ``'sender_class'`` — ``gate.scoring.urgency()`` reads exactly those two keys,
and their absence is what makes the neutral-baseline score land under the threshold (setting
either would silently break default-hold).

DEBOUNCE IS TWO-LAYER (build ruling F): (a) in-memory — this source holds an ``event_key ->
anchor datetime`` map of already-emitted keys and never re-emits one within a process lifetime;
pruned each poll to the keys still inside the trailing lookback window (bounded memory; a
restart forgets, which is fine — (b) below covers a restart). (b) cross-restart — the
deterministic ``event_key`` derivations above ride the shipped ``DedupingEnqueuer``/``SeenLedger``
the registry is already handed; this module does not build a second persistence layer.

OUT OF SCOPE: no clinical alerting, no emergency detection, no health advice (NG-2); no daily
ceiling/flush-cap change (NG-3); no gate bypass/flush exemption (CON-3); no new config field.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from wombat.sources.base import SourceEvent

logger = logging.getLogger(__name__)

# DEC-63 no-knob pins — module-private, no ticket has asked for an operator-facing tunable.
# The same trailing-retention pin behavior.stages.dream_biometrics reads against.
_LOOKBACK_DAYS = 21
_RESTING_HR_BAND_BPM = 7
_MIN_RESTING_HR_DAYS = 5
_SLEEP_DEBT_NIGHTS = 3
_SLEEP_DEBT_THRESHOLD_MINUTES = 180
_MIN_SLEEP_NIGHTS = 5

# The devices.biometric_ingest ledger vocabulary this source reads (byte-untouched there).
_CHANNEL = "biometric"
_WORKOUT_KIND = "workout"
_RESTING_HR_KIND = "resting_hr_daily"
_SLEEP_KIND = "sleep_session"

# TK-348's closed three-kind output vocabulary (DEC-80(d)) — no fourth kind, ever.
_WORKOUT_ENDED_EVENT = "workout_ended"
_RESTING_HR_OUT_OF_BAND_EVENT = "resting_hr_out_of_band"
_SLEEP_DEBT_CROSSED_EVENT = "sleep_debt_crossed"

_EVENT_CLASS = "biometric"


def _utc_now() -> datetime:
    """The real-clock default (mirrors every other source's own ``_utc_now`` default)."""
    return datetime.now(UTC)


def _day_start(day: date) -> datetime:
    """A ``date`` -> UTC midnight ``datetime`` — used only to compare a day-anchored event's
    debounce key against the trailing lookback boundary (both ``datetime``, both UTC)."""
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


class ObservationsLike(Protocol):
    """The one ``ObservationStore`` method this source needs (mirrors ``screenpipe_source.
    ScreenpipeClientLike``'s minimal-seam convention) — lets tests inject a fake/scripted store;
    production always wires the real ``ObservationStore``."""

    def get_window(self, channel: str, start: datetime, end: datetime) -> list[dict[str, Any]]: ...


class BiometricEventSource:
    """Derives TK-348's closed three-kind vocabulary from an injected ``ObservationStore``-shaped
    collaborator's ``'biometric'``-channel rows. See the module docstring for the full contract."""

    id: str = "biometric_events"

    def __init__(
        self,
        *,
        observations: ObservationsLike,
        poll_interval_seconds: float,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.poll_interval_seconds = poll_interval_seconds
        self._observations = observations
        self._clock = clock
        # Build ruling F(a): in-memory debounce — event_key -> the datetime anchoring it, so a
        # later poll can prune keys that have aged out of the trailing lookback window.
        self._emitted: dict[str, datetime] = {}

    async def start(self) -> None:
        """No lifecycle setup needed — the injected store is already constructed."""
        return None

    async def stop(self) -> None:
        """No lifecycle teardown needed."""
        return None

    async def poll(self) -> list[SourceEvent]:
        """Read the trailing window and derive zero or more of the three closed event kinds.

        NEVER raises: a failing ``get_window`` degrades this poll to zero rows (logged loud)
        rather than killing this source's poll loop.
        """
        now = self._clock()
        start = now - timedelta(days=_LOOKBACK_DAYS)
        self._prune_emitted(start)
        rows = self._read_window(start, now)

        events: list[SourceEvent] = []
        self._derive_workout_ended(rows, events)
        self._derive_resting_hr_out_of_band(rows, events)
        self._derive_sleep_debt_crossed(rows, events)
        return events

    def _read_window(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        try:
            return self._observations.get_window(_CHANNEL, start, end)
        except Exception:
            logger.error(
                "biometric_events: get_window failed; treating as empty for this poll",
                exc_info=True,
            )
            return []

    def _prune_emitted(self, boundary: datetime) -> None:
        """Forget any already-emitted key whose anchor has aged out of the trailing lookback
        window — bounded memory across a long process lifetime (build ruling F(a))."""
        stale = [key for key, anchor in self._emitted.items() if anchor < boundary]
        for key in stale:
            del self._emitted[key]

    # ------------------------------------------------------------------------ (1) workout_ended

    def _derive_workout_ended(
        self, rows: list[dict[str, Any]], events: list[SourceEvent]
    ) -> None:
        for row in rows:
            if row.get("kind") != _WORKOUT_KIND:
                continue
            started_at = row.get("started_at")
            if not isinstance(started_at, datetime):
                continue
            started_at_iso = started_at.isoformat()
            event_key = f"{_WORKOUT_ENDED_EVENT}:{started_at_iso}"
            if event_key in self._emitted:
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            event_payload = {"event_class": _EVENT_CLASS, "kind": _WORKOUT_ENDED_EVENT, **payload}
            events.append(SourceEvent(event_key=event_key, payload=event_payload))
            self._emitted[event_key] = started_at

    # ---------------------------------------------------------------- (2) resting_hr_out_of_band

    def _derive_resting_hr_out_of_band(
        self, rows: list[dict[str, Any]], events: list[SourceEvent]
    ) -> None:
        resting_rows = [row for row in rows if row.get("kind") == _RESTING_HR_KIND]
        bpm_by_day: dict[date, list[int]] = {}
        for row in resting_rows:
            payload = row.get("payload") or {}
            bpm = payload.get("bpm")
            day_key = row.get("day_key")
            if isinstance(bpm, int) and isinstance(day_key, date):
                bpm_by_day.setdefault(day_key, []).append(bpm)
        distinct_days = list(bpm_by_day.keys())

        emitted_this_poll: set[date] = set()
        for row in resting_rows:
            payload = row.get("payload") or {}
            bpm = payload.get("bpm")
            day_key = row.get("day_key")
            if not isinstance(bpm, int) or not isinstance(day_key, date):
                continue
            if day_key in emitted_this_poll:
                continue  # AC2: at most one event per day_key, even if it flaps in/out of band
            event_key = f"{_RESTING_HR_OUT_OF_BAND_EVENT}:{day_key.isoformat()}"
            if event_key in self._emitted:
                emitted_this_poll.add(day_key)
                continue
            other_days = [d for d in distinct_days if d != day_key]
            if len(other_days) < _MIN_RESTING_HR_DAYS:
                continue  # fewer than the minimum -> no event, ever (per this row's day)
            other_values = [v for d in other_days for v in bpm_by_day[d]]
            baseline = sum(other_values) / len(other_values)
            if abs(bpm - baseline) > _RESTING_HR_BAND_BPM:
                events.append(
                    SourceEvent(
                        event_key=event_key,
                        payload={
                            "event_class": _EVENT_CLASS,
                            "kind": _RESTING_HR_OUT_OF_BAND_EVENT,
                            "bpm": bpm,
                        },
                    )
                )
                self._emitted[event_key] = _day_start(day_key)
                emitted_this_poll.add(day_key)

    # --------------------------------------------------------------------- (3) sleep_debt_crossed

    def _derive_sleep_debt_crossed(
        self, rows: list[dict[str, Any]], events: list[SourceEvent]
    ) -> None:
        sleep_rows = [row for row in rows if row.get("kind") == _SLEEP_KIND]
        minutes_by_day: dict[date, list[int]] = {}
        row_by_day: dict[date, dict[str, Any]] = {}
        for row in sleep_rows:
            payload = row.get("payload") or {}
            asleep_minutes = payload.get("asleep_minutes")
            day_key = row.get("day_key")
            if isinstance(asleep_minutes, int) and isinstance(day_key, date):
                minutes_by_day.setdefault(day_key, []).append(asleep_minutes)
                row_by_day[day_key] = row  # last-seen row for this night (rows are start-ordered)

        distinct_nights = list(minutes_by_day.keys())
        if len(distinct_nights) < _MIN_SLEEP_NIGHTS:
            return  # fewer than the minimum -> no event, ever

        all_minutes = [m for values in minutes_by_day.values() for m in values]
        baseline = sum(all_minutes) / len(all_minutes)

        recent_nights = sorted(distinct_nights, reverse=True)[:_SLEEP_DEBT_NIGHTS]
        shortfall_sum = 0.0
        for night in recent_nights:
            night_values = minutes_by_day[night]
            night_avg = sum(night_values) / len(night_values)
            shortfall_sum += baseline - night_avg

        if shortfall_sum <= _SLEEP_DEBT_THRESHOLD_MINUTES:
            return

        most_recent_night = max(recent_nights)
        event_key = f"{_SLEEP_DEBT_CROSSED_EVENT}:{most_recent_night.isoformat()}"
        if event_key in self._emitted:
            return
        anchor_row = row_by_day[most_recent_night]
        payload = anchor_row.get("payload")
        if not isinstance(payload, dict):
            return
        event_payload = {"event_class": _EVENT_CLASS, "kind": _SLEEP_DEBT_CROSSED_EVENT, **payload}
        events.append(SourceEvent(event_key=event_key, payload=event_payload))
        self._emitted[event_key] = _day_start(most_recent_night)


__all__ = ["BiometricEventSource", "ObservationsLike"]
