"""DreamObserveStage — nightly deterministic LLM-free distillation of ``wombat_observations``
into ``source='behavior'`` user facts via closed templates (TK-314, EP-37, DEC-68(d)(2)).

Inserted into the ``wombat.dream`` graph immediately after ``dream_derive`` (TK-299) — the same
mechanical splice TK-299 made between ``dream_facts`` and ``dream_behavior_log``
(``pathways/dream_pathway.py``): ``dream_facts`` -> ``dream_derive`` -> ``dream_observe`` ->
``dream_screenpipe`` (TK-324's stage, this stage's new downstream neighbor, superseding
``dream_behavior_log``) -> ``dream_behavior_log``.

Keyword-injected collaborators only (``DreamDeriveStage`` precedent): ``observations`` is
``wombat.observations.ObservationStore`` (TK-310), this stage's ONLY read path; ``user_facts`` is
``wombat.user_facts.UserFactsStore`` (TK-294), this stage's ONLY write path. UNLIKE
``dream_derive``'s collaborators, ``observations`` being ``None`` is a LEGITIMATE production state
— the DEC-68(b) consent toggles default OFF, and a toggle-off boot structurally never constructs
the store — so ``None`` here logs ONE WARNING (the observe pass is skipped tonight) rather than
``dream_derive``'s ERROR posture, and the stage still transitions onward. ``user_facts`` is
constructed unconditionally by ``bootstrap.assemble_runtime``, so ``None`` THERE keeps the loud
ERROR posture (mirrors ``dream_derive`` exactly).

READ: over a fixed ``_LOOKBACK_DAYS = 21`` trailing window ending at ``ctx.clock()`` (pinned to
``observations._OBSERVATION_RETENTION_DAYS = 21`` — the ledger holds exactly this much history),
``observations.get_window("screen", start, now)`` and ``observations.get_window("mic", start,
now)`` — BOTH channels (the call-rhythm template consumes the mic rows). A raising ``get_window``
is caught PER CHANNEL and treated as zero rows for that channel (logged loud ERROR) — never blocks
the other channel's read. LEDGER VOCAB (DEC-68(a)/(c), RULED): screen rows are
``channel='screen', kind='app_segment', payload={app, title}``; mic rows are ``channel='mic',
kind='in_call', payload={}``; ``day_key`` is the tz-local (DEC-21) civil date the segment opened
on — day/weekday identity below always reads ``day_key``, never a re-derived UTC date.

DERIVE (pure code, three closed templates — DEC-68(d)(2)):

  (a) arrival rhythm — the FIRST screen segment per tz-local weekday (Mon-Fri) day is that day's
      arrival; each arrival time (in ``tz``) is rounded UP to the next half hour ("at the computer
      by ~09:00" for an 08:47 arrival — the ceiling reading of "by"). A half-hour bucket holding
      ``>= _MIN_ARRIVALS_PER_WEEK = 3`` weekday arrivals in each of ``>= _MIN_ARRIVAL_WEEKS = 2``
      distinct ISO weeks qualifies: ``"Usually at the computer by <HH:MM> on weekdays"``. At most
      ONE arrival fact per pass (the qualifying bucket with the most contributing days; earlier
      time wins a tie) under the SINGLE-SLOT stable key ``behavior:arrival:weekday`` — a shifted
      rhythm on a later night re-upserts the same slot, never accretes contradictory rows.
  (b) app residency — weekday screen segments are attributed wholly to the pinned daypart their
      tz-local ``started_at`` hour falls in (``_DAYPARTS``: morning 05-11, afternoon 12-16,
      evening 17-21; segments outside those hours are ignored) and summed per app (grouped on
      whitespace-collapsed app name; the first occurrence's own text renders). A daypart spanning
      ``>= _MIN_DAYPART_DAYS = 3`` distinct weekday days whose top app holds a plurality share
      ``>= _MIN_SHARE = 0.4`` of that daypart's total segment seconds qualifies:
      ``"Spends most weekday <daypart>s in <app>"``. Single-slot stable key per daypart:
      ``behavior:residency:<daypart>`` (same re-upsert-the-slot reasoning as (a)).
  (c) call rhythm — mic ``in_call`` segments grouped by ``day_key`` weekday (all seven days —
      a Saturday call rhythm is a real observation); a weekday with in-call segments in
      ``>= _MIN_CALL_WEEKS = 2`` distinct ISO weeks qualifies: ``"Regularly takes calls on
      <weekday>s"``. Stable key: ``behavior:calls:<weekday lowercase>``.

RAW TITLES NEVER FOSSILIZE (TK-314 pinned): no template reads ``payload["title"]`` — app display
names only. A window title may ride the live prompt per DEC-68(d)(1), but never lands in the
durable store past the ledger's own 21-day retention. CON-6 by construction: all three templates
are fixed third-person sentences filled ONLY with observed time/daypart/app/weekday values — no
branch, no free-text field, and no model call through which a motive/judgment phrase could enter.

CAP + ORDER (DEC-63 no-knob, pinned): the arrival fact (at most one) THEN every residency fact
(sorted by its own key) THEN every call fact (sorted by its own key), truncated to
``_MAX_OBSERVE_FACTS = 5``. Exceeding the cap logs ONE loud WARNING naming how many qualifying
facts were found; which facts survive is fully deterministic (same ledger, same 5 every night).

WRITE: each surviving fact is idempotently written via ``user_facts.upsert_fact(key, fact,
source="behavior")`` — the DEC-66-reserved provenance value, exactly this stage's tier. Keys are
stable/derived from the data itself, so a re-derivation re-upserts the SAME key (idempotent by
construction, AC2). A key not already present (read once via ``count``/``list_facts``, the
``dream_derive`` dedupe-read shape) is "NEW": exactly ONE INFO journal line per NEW fact (CON-4) —
a re-upserted already-known fact logs nothing. A raising dedupe read is caught loud and treated as
"every candidate is new"; a raising ``upsert_fact`` for one candidate is caught loud and skipped —
facts already upserted stay written.

NEVER BLOCKS: zero qualifying rows is the ordinary case (no error, no writes, nothing to log) —
an empty/sparse ledger is NOT an anomaly. A ``None`` store (one WARNING) or a raising store (loud
ERROR) still transitions onward (the dream posture) — this pass never touches ``ctx.journal`` and
never calls a model (NG-4 intact; DEC-68(d)(2): deliberately LLM-free, no DEC-23 budget touch).

OUT OF SCOPE (recorded v1 simplification, mirrors ``dream_derive``): no fact deletion/decay when
a rhythm lapses — the ``UserFactsStore`` cap is the only eviction path; no new store, no new
config/tunable, no render change (behavior facts flow through the EXISTING TK-296
``known_user_context`` block automatically); no webcam/mic-VAD-derived templates until their
phases land.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import StageResult, Transition
from cogworx.loop.stage import StageContext

from wombat.observations import ObservationStore
from wombat.user_facts import UserFactsStore

logger = logging.getLogger(__name__)

# DreamObserveStage's committed output kind (TK-314) — a contentless, system-provenance count
# artifact mirroring dream_derive.py's DREAM_DERIVE_REPORT_KIND idiom: no fact text rides this
# artifact, only a count — the durable record is the wombat_user_facts rows the stage upserted.
DREAM_OBSERVE_REPORT_KIND = "wombat.dream_observe_report"

# The trailing read window — pinned to observations._OBSERVATION_RETENTION_DAYS (21): the ledger
# holds exactly this much history, so a longer lookback would read nothing extra. Not a tunable
# (DEC-63 no-knob precedent).
_LOOKBACK_DAYS = 21

# Pinned per-pass fact cap (TK-314 named constant; DEC-63 no-knob precedent).
_MAX_OBSERVE_FACTS = 5

# Pinned plurality-share bar for the app-residency template (TK-314 named constant).
_MIN_SHARE = 0.4

# Pinned arrival-rhythm bars: >= 3 weekday-consistent first-segments/week across >= 2 weeks
# (TK-314's own wording, read literally).
_MIN_ARRIVALS_PER_WEEK = 3
_MIN_ARRIVAL_WEEKS = 2

# Pinned residency bar: a daypart must span this many distinct weekday days before its plurality
# app can qualify (guards a one-afternoon fluke from fossilizing into a "most afternoons" fact).
_MIN_DAYPART_DAYS = 3

# Pinned call-rhythm bar: a weekday qualifies with in-call segments in this many distinct ISO
# weeks (recurrence, mirrors dream_derive's distinct-week reading).
_MIN_CALL_WEEKS = 2

# The arrival rounding grain — "by <time>" rounds UP to the next half hour (an 08:47 arrival is
# "by 09:00"); not itself a tunable.
_ARRIVAL_ROUND_MINUTES = 30

# The DEC-68(a)/(c) ledger vocabulary this stage reads (RULED).
_SCREEN_CHANNEL = "screen"
_SCREEN_KIND = "app_segment"
_MIC_CHANNEL = "mic"
_MIC_KIND = "in_call"

_WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

# Pinned tz-local daypart bounds (start hour inclusive, end hour exclusive) — a closed set;
# late-night segments (22:00-04:59) belong to no daypart and are ignored by template (b).
_DAYPARTS: tuple[tuple[str, int, int], ...] = (
    ("morning", 5, 12),
    ("afternoon", 12, 17),
    ("evening", 17, 22),
)


def _round_up_to_half_hour(hour: int, minute: int) -> tuple[int, int]:
    """Round ``hour:minute`` UP to the next ``_ARRIVAL_ROUND_MINUTES`` mark (exact boundaries
    stay), wrapping past midnight — the "at the computer by <time>" ceiling reading."""
    total = hour * 60 + minute
    rounded = -(-total // _ARRIVAL_ROUND_MINUTES) * _ARRIVAL_ROUND_MINUTES
    rounded %= 24 * 60
    return divmod(rounded, 60)


def _daypart_of(hour: int) -> str | None:
    for name, start_hour, end_hour in _DAYPARTS:
        if start_hour <= hour < end_hour:
            return name
    return None


def _derive_arrival_fact(rows: list[dict[str, Any]], tz: ZoneInfo) -> list[tuple[str, str]]:
    """Template (a): at most ONE ``(fact_key, fact_text)`` for the qualifying weekday-arrival
    half-hour bucket (most contributing days wins; earlier time wins a tie)."""
    first_by_day: dict[date, datetime] = {}
    for row in rows:
        if row.get("kind") != _SCREEN_KIND:
            continue
        started_at = row.get("started_at")
        day_key = row.get("day_key")
        if not isinstance(started_at, datetime) or not isinstance(day_key, date):
            continue
        if day_key.weekday() >= 5:
            continue  # weekends never feed the weekday-arrival template
        local = started_at.astimezone(tz)
        current = first_by_day.get(day_key)
        if current is None or local < current:
            first_by_day[day_key] = local

    # bucket -> ISO week -> the distinct weekday days that arrived in this bucket that week
    buckets: dict[tuple[int, int], dict[tuple[int, int], set[date]]] = {}
    for day_key, local in first_by_day.items():
        bucket = _round_up_to_half_hour(local.hour, local.minute)
        iso_year, iso_week, _ = day_key.isocalendar()
        buckets.setdefault(bucket, {}).setdefault((iso_year, iso_week), set()).add(day_key)

    best: tuple[int, tuple[int, int]] | None = None  # (total_days, (hour, minute))
    for bucket, weeks in buckets.items():
        qualifying_weeks = sum(
            1 for days in weeks.values() if len(days) >= _MIN_ARRIVALS_PER_WEEK
        )
        if qualifying_weeks < _MIN_ARRIVAL_WEEKS:
            continue
        total_days = sum(len(days) for days in weeks.values())
        # More contributing days wins; on a tie the EARLIER bucket wins (negate for max()).
        if best is None or (total_days, (-bucket[0], -bucket[1])) > (
            best[0],
            (-best[1][0], -best[1][1]),
        ):
            best = (total_days, bucket)

    if best is None:
        return []
    hour, minute = best[1]
    fact_text = f"Usually at the computer by {hour:02d}:{minute:02d} on weekdays"
    return [("behavior:arrival:weekday", fact_text)]


def _derive_residency_facts(rows: list[dict[str, Any]], tz: ZoneInfo) -> list[tuple[str, str]]:
    """Template (b): one ``(fact_key, fact_text)`` per pinned daypart whose plurality app holds
    ``>= _MIN_SHARE`` of that daypart's weekday segment seconds across ``>= _MIN_DAYPART_DAYS``
    distinct days — sorted by ``fact_key``. App DISPLAY NAMES only; ``payload['title']`` is never
    read (TK-314 pinned)."""
    seconds: dict[str, dict[str, float]] = {}
    display: dict[str, dict[str, str]] = {}
    days: dict[str, set[date]] = {}
    for row in rows:
        if row.get("kind") != _SCREEN_KIND:
            continue
        payload = row.get("payload") or {}
        app = payload.get("app")
        started_at = row.get("started_at")
        ended_at = row.get("ended_at")
        day_key = row.get("day_key")
        if (
            not app
            or not isinstance(started_at, datetime)
            or not isinstance(ended_at, datetime)
            or not isinstance(day_key, date)
        ):
            continue
        if day_key.weekday() >= 5:
            continue
        duration = (ended_at - started_at).total_seconds()
        if duration <= 0:
            continue
        daypart = _daypart_of(started_at.astimezone(tz).hour)
        if daypart is None:
            continue
        normalized_app = " ".join(str(app).split()).casefold()
        if not normalized_app:
            continue
        part_seconds = seconds.setdefault(daypart, {})
        part_seconds[normalized_app] = part_seconds.get(normalized_app, 0.0) + duration
        display.setdefault(daypart, {}).setdefault(normalized_app, str(app).strip())
        days.setdefault(daypart, set()).add(day_key)

    facts: list[tuple[str, str]] = []
    for daypart, part_seconds in seconds.items():
        if len(days.get(daypart, set())) < _MIN_DAYPART_DAYS:
            continue
        total = sum(part_seconds.values())
        if total <= 0:
            continue
        # Deterministic plurality pick: most seconds first, lexicographic app name breaks ties.
        top_app, top_seconds = sorted(part_seconds.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        if top_seconds / total < _MIN_SHARE:
            continue
        fact_key = f"behavior:residency:{daypart}"
        fact_text = f"Spends most weekday {daypart}s in {display[daypart][top_app]}"
        facts.append((fact_key, fact_text))

    facts.sort(key=lambda item: item[0])
    return facts


def _derive_call_facts(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Template (c): one ``(fact_key, fact_text)`` per weekday with mic ``in_call`` segments in
    ``>= _MIN_CALL_WEEKS`` distinct ISO weeks — sorted by ``fact_key``."""
    weeks_by_weekday: dict[int, set[tuple[int, int]]] = {}
    for row in rows:
        if row.get("kind") != _MIC_KIND:
            continue
        day_key = row.get("day_key")
        if not isinstance(day_key, date):
            continue
        iso_year, iso_week, _ = day_key.isocalendar()
        weeks_by_weekday.setdefault(day_key.weekday(), set()).add((iso_year, iso_week))

    facts: list[tuple[str, str]] = []
    for weekday, weeks in weeks_by_weekday.items():
        if len(weeks) < _MIN_CALL_WEEKS:
            continue
        weekday_name = _WEEKDAY_NAMES[weekday]
        fact_key = f"behavior:calls:{weekday_name.lower()}"
        fact_text = f"Regularly takes calls on {weekday_name}s"
        facts.append((fact_key, fact_text))

    facts.sort(key=lambda item: item[0])
    return facts


def _existing_fact_keys(user_facts: UserFactsStore) -> set[str]:
    """Every ``fact_key`` already in ``user_facts`` — read ONCE per run (``count`` then
    ``list_facts(count)``), mirrors ``dream_derive._existing_fact_keys`` exactly."""
    total = user_facts.count()
    if total == 0:
        return set()
    return {row["fact_key"] for row in user_facts.list_facts(total)}


class DreamObserveStage:
    """Nightly deterministic distillation of the ``wombat_observations`` ledger into
    ``source='behavior'`` user facts — closed templates, PURE CODE, NO LLM (TK-314, EP-37,
    DEC-68(d)(2)). See the module docstring for the full read/derive/cap/write contract."""

    name: str = "dream_observe"
    transitions: tuple[str, ...] = ("dream_screenpipe",)

    def __init__(
        self,
        *,
        observations: ObservationStore | None,
        user_facts: UserFactsStore | None,
        tz: ZoneInfo,
    ) -> None:
        self._observations = observations
        self._user_facts = user_facts
        self._tz = tz

    def _read_channel(self, channel: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        assert self._observations is not None  # run() gates the None case before calling here
        try:
            return self._observations.get_window(channel, start, end)
        except Exception:
            logger.error(
                "dream_observe: get_window(channel=%r) failed; treating as empty for tonight's "
                "pass",
                channel,
                exc_info=True,
            )
            return []

    async def run(self, ctx: StageContext) -> StageResult:
        now = ctx.clock()
        start = now - timedelta(days=_LOOKBACK_DAYS)

        if self._observations is None:
            # A legitimate toggle-off boot (DEC-68(b) consent defaults) — one WARNING, never an
            # ERROR, and tonight's observe pass simply derives nothing.
            logger.warning(
                "dream_observe: no ObservationStore (observe channels off or not constructed); "
                "skipping tonight's observe pass"
            )
            screen_rows: list[dict[str, Any]] = []
            mic_rows: list[dict[str, Any]] = []
        else:
            screen_rows = self._read_channel(_SCREEN_CHANNEL, start, now)
            mic_rows = self._read_channel(_MIC_CHANNEL, start, now)

        candidates = (
            _derive_arrival_fact(screen_rows, self._tz)
            + _derive_residency_facts(screen_rows, self._tz)
            + _derive_call_facts(mic_rows)
        )
        total_candidates = len(candidates)
        if total_candidates > _MAX_OBSERVE_FACTS:
            logger.warning(
                "dream_observe: %d qualifying fact(s) exceed the %d-per-pass cap; keeping the "
                "first %d in deterministic order",
                total_candidates,
                _MAX_OBSERVE_FACTS,
                _MAX_OBSERVE_FACTS,
            )
        candidates = candidates[:_MAX_OBSERVE_FACTS]

        new_facts = 0
        if candidates:
            if self._user_facts is None:
                logger.error(
                    "dream_observe: user_facts store is None; skipping all %d write(s) for "
                    "tonight's pass",
                    len(candidates),
                )
            else:
                try:
                    existing_keys = _existing_fact_keys(self._user_facts)
                except Exception:
                    logger.error(
                        "dream_observe: reading existing facts for dedupe failed; treating every "
                        "candidate as new",
                        exc_info=True,
                    )
                    existing_keys = set()

                for fact_key, fact_text in candidates:
                    is_new = fact_key not in existing_keys
                    try:
                        self._user_facts.upsert_fact(fact_key, fact_text, source="behavior")
                    except Exception:
                        logger.error(
                            "dream_observe: upsert_fact failed for fact_key=%s; skipping",
                            fact_key,
                            exc_info=True,
                        )
                        continue
                    if is_new:
                        new_facts += 1
                        logger.info("dream_observe: accepted new fact fact_key=%s", fact_key)

        return Transition(
            to="dream_screenpipe",
            output=Artifact(
                kind=DREAM_OBSERVE_REPORT_KIND,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
                data={"new_facts": new_facts},
            ),
        )


__all__ = ["DREAM_OBSERVE_REPORT_KIND", "DreamObserveStage"]
