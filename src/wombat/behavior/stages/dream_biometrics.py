"""DreamBiometricsStage — Tier 1 (TK-346, EP-41): nightly deterministic LLM-free distillation of
``wombat_observations``' ``channel='biometric'`` rows into ``source='behavior'`` user facts.

This is the shipped ``dream_observe`` pattern (TK-314) POINTED AT A SECOND CHANNEL — zero new
mechanism. LLM-FREE is the point: deterministic templates over closed numeric segments can never
state motive (CON-6 by construction), and the model never sees a single biometric row.

Inserted into the ``wombat.dream`` graph immediately after ``dream_screenpipe`` (TK-324) — RULING
R6, the only splice that touches neither ``dream_observe`` nor ``dream_facts`` (both protected):
``dream_screenpipe`` -> ``dream_biometrics`` -> ``dream_behavior_log``.

Keyword-injected collaborators only (``DreamObserveStage`` precedent): ``observations`` is
``wombat.observations.ObservationStore`` (TK-310/TK-341), this stage's ONLY read path — ``None``
is a LEGITIMATE production state (the DEC-78(d) ``wombat_observe_biometrics`` consent toggle
defaults off, and a toggle-off boot structurally never constructs the store); ``user_facts`` is
``wombat.user_facts.UserFactsStore`` (TK-294), this stage's ONLY write path.

READ: over a fixed ``_LOOKBACK_DAYS = 21`` trailing window ending at ``ctx.clock()`` (the SAME
retention pin ``dream_observe`` reads against — ``observations._OBSERVATION_RETENTION_DAYS``),
``observations.get_window("biometric", start, now)``. A raising ``get_window`` is caught loud and
treated as zero rows for tonight's pass (never blocks the run). LEDGER VOCAB (``devices.
biometric_ingest``, byte-untouched here): rows are ``channel='biometric'``, ``kind`` one of the
closed §3.1 set, ``payload`` the matching closed schema; only ``sleep_session``/
``resting_hr_daily`` feed this ticket's two templates — ``workout``/``hrv_daily``/``steps_hourly``
rows are read (they ride the SAME channel) but no template consumes them (TK-346's complexity
budget: two templates, not five).

DERIVE (pure code, two closed templates):

  (a) sleep duration — averages ``payload["asleep_minutes"]`` across every ``sleep_session`` row
      whose ``day_key`` is one of ``>= _MIN_SLEEP_NIGHTS = 5`` distinct nights in the window,
      rounded to the nearest ``_SLEEP_ROUND_MINUTES = 15``: ``"Usually gets about <H>h <MM>m of
      sleep per night"``. Stable key: ``biometric:sleep:duration``.
  (b) resting heart rate — averages ``payload["bpm"]`` across every ``resting_hr_daily`` row whose
      ``day_key`` is one of ``>= _MIN_RESTING_HR_DAYS = 5`` distinct days in the window, rounded to
      the nearest integer bpm: ``"Resting heart rate is usually around <N> bpm"``. Stable key:
      ``biometric:resting_hr:baseline``.

Both templates are fixed third-person sentences filled ONLY with an observed rounded average — no
branch, no free-text field, and no model call through which a motive/judgment/clinical phrase could
enter (CON-6 by construction, NG-2's no-clinical-function bar). ``_FACT_TEMPLATES`` below is the
CLOSED template vocabulary a CON-6 motive screen runs over directly (proof over the template set
itself, not sampled instances).

CAP + ORDER (DEC-63 no-knob precedent, mirrors ``dream_observe``'s own cap idiom exactly): the
sleep fact (at most one) THEN the resting-HR fact (at most one), truncated to
``_MAX_BIOMETRIC_FACTS = 5`` — the SAME cap value and truncation/logging mechanism ``dream_observe``
uses, restated here since these two templates can never together exceed it (headroom for a later
tier, not itself a tunable).

WRITE: each surviving fact is idempotently written via ``user_facts.upsert_fact(key, fact,
source="behavior")`` (DEC-70h's provenance-by-origin ruling, restated for this tier: the
distillation mechanism never sets provenance, the observational tier does). Keys are
stable/derived from the data's own shape, so a re-derivation re-upserts the SAME key (idempotent by
construction). A key not already present (read once via ``count``/``list_facts``, the
``dream_observe`` dedupe-read shape) is "NEW": exactly ONE INFO journal line per NEW fact — a
re-upserted already-known fact logs nothing.

NEVER BLOCKS: no rows in the window (whether because ``observations`` is ``None`` — the consent
toggle off — or because a real store's ``get_window`` simply returned nothing) logs exactly ONE
skip line and writes nothing; absent data is never an error. A raising dedupe read is caught loud
and treated as "every candidate is new"; a raising ``upsert_fact`` for one candidate is caught loud
and skipped — facts already upserted stay written. This pass never touches ``ctx.journal`` and
never calls a model (NG-4 intact; no DEC-23 budget touch).

OUT OF SCOPE (TK-346 non-goals, recorded): no grounding line and no queue events (TK-347/TK-348 own
those); no clinical/diagnostic/health-advice framing in any template (NG-2); no trend analysis
beyond the pinned 21-day window (Q-132 tracks the longer-trend question); no fact deletion/decay
(the ``UserFactsStore`` cap is the only eviction path); no new store, no new config/tunable, no
render change (behavior facts flow through the EXISTING ``known_user_context`` block automatically,
the same as every other ``source='behavior'`` fact); no templates over ``workout``/``hrv_daily``/
``steps_hourly`` (a later tier's concern, not this one's).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import StageResult, Transition
from cogworx.loop.stage import StageContext

from wombat.observations import ObservationStore
from wombat.user_facts import UserFactsStore

logger = logging.getLogger(__name__)

# DreamBiometricsStage's committed output kind (TK-346) — a contentless, system-provenance count
# artifact mirroring dream_observe.py's DREAM_OBSERVE_REPORT_KIND idiom: no fact text rides this
# artifact, only a count — the durable record is the wombat_user_facts rows the stage upserted.
DREAM_BIOMETRICS_REPORT_KIND = "wombat.dream_biometrics_report"

# The trailing read window — pinned to observations._OBSERVATION_RETENTION_DAYS (21), the SAME
# retention dream_observe reads against. Not a tunable (DEC-63 no-knob precedent).
_LOOKBACK_DAYS = 21

# Pinned per-pass fact cap — the SAME value and mechanism dream_observe._MAX_OBSERVE_FACTS uses
# (DEC-63 no-knob precedent); this ticket's two templates can never together exceed it.
_MAX_BIOMETRIC_FACTS = 5

# The devices.biometric_ingest ledger vocabulary this stage reads (byte-untouched there).
_BIOMETRIC_CHANNEL = "biometric"
_SLEEP_KIND = "sleep_session"
_RESTING_HR_KIND = "resting_hr_daily"

# Pinned regularity bars: a template needs signal from at least this many distinct days before it
# qualifies as "usually" (guards a one-night/one-day fluke from fossilizing into a durable fact).
_MIN_SLEEP_NIGHTS = 5
_MIN_RESTING_HR_DAYS = 5

# The sleep-duration rounding grain (mirrors dream_observe's arrival-rounding-grain convention);
# not itself a tunable.
_SLEEP_ROUND_MINUTES = 15

# The CLOSED template vocabulary (TK-346 AC3): a CON-6 motive screen runs over these two fixed
# format strings directly, never over sampled/filled instances — proof over the vocabulary itself.
_SLEEP_FACT_TEMPLATE = "Usually gets about {hours}h {minutes:02d}m of sleep per night"
_RESTING_HR_FACT_TEMPLATE = "Resting heart rate is usually around {bpm} bpm"
_FACT_TEMPLATES: tuple[str, ...] = (_SLEEP_FACT_TEMPLATE, _RESTING_HR_FACT_TEMPLATE)


def _derive_sleep_fact(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Template (a): at most ONE ``(fact_key, fact_text)`` for the average ``asleep_minutes``
    across every qualifying ``sleep_session`` night, rounded to ``_SLEEP_ROUND_MINUTES``."""
    minutes_by_day: dict[date, list[int]] = {}
    for row in rows:
        if row.get("kind") != _SLEEP_KIND:
            continue
        payload = row.get("payload") or {}
        asleep_minutes = payload.get("asleep_minutes")
        day_key = row.get("day_key")
        if not isinstance(asleep_minutes, int) or not isinstance(day_key, date):
            continue
        minutes_by_day.setdefault(day_key, []).append(asleep_minutes)

    if len(minutes_by_day) < _MIN_SLEEP_NIGHTS:
        return []

    all_minutes = [minutes for values in minutes_by_day.values() for minutes in values]
    average = sum(all_minutes) / len(all_minutes)
    rounded = round(average / _SLEEP_ROUND_MINUTES) * _SLEEP_ROUND_MINUTES
    hours, minutes = divmod(int(rounded), 60)
    fact_text = _SLEEP_FACT_TEMPLATE.format(hours=hours, minutes=minutes)
    return [("biometric:sleep:duration", fact_text)]


def _derive_resting_hr_fact(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Template (b): at most ONE ``(fact_key, fact_text)`` for the average ``bpm`` across every
    qualifying ``resting_hr_daily`` day, rounded to the nearest integer bpm."""
    bpm_by_day: dict[date, list[int]] = {}
    for row in rows:
        if row.get("kind") != _RESTING_HR_KIND:
            continue
        payload = row.get("payload") or {}
        bpm = payload.get("bpm")
        day_key = row.get("day_key")
        if not isinstance(bpm, int) or not isinstance(day_key, date):
            continue
        bpm_by_day.setdefault(day_key, []).append(bpm)

    if len(bpm_by_day) < _MIN_RESTING_HR_DAYS:
        return []

    all_bpm = [bpm for values in bpm_by_day.values() for bpm in values]
    average_bpm = round(sum(all_bpm) / len(all_bpm))
    fact_text = _RESTING_HR_FACT_TEMPLATE.format(bpm=average_bpm)
    return [("biometric:resting_hr:baseline", fact_text)]


def _existing_fact_keys(user_facts: UserFactsStore) -> set[str]:
    """Every ``fact_key`` already in ``user_facts`` — read ONCE per run (``count`` then
    ``list_facts(count)``), mirrors ``dream_observe._existing_fact_keys`` exactly."""
    total = user_facts.count()
    if total == 0:
        return set()
    return {row["fact_key"] for row in user_facts.list_facts(total)}


class DreamBiometricsStage:
    """Nightly deterministic distillation of ``wombat_observations``' ``channel='biometric'`` rows
    into ``source='behavior'`` user facts — closed templates, PURE CODE, NO LLM (TK-346, EP-41).
    See the module docstring for the full read/derive/cap/write contract."""

    name: str = "dream_biometrics"
    transitions: tuple[str, ...] = ("dream_behavior_log",)

    def __init__(
        self,
        *,
        observations: ObservationStore | None,
        user_facts: UserFactsStore | None,
    ) -> None:
        self._observations = observations
        self._user_facts = user_facts

    def _read_channel(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        assert self._observations is not None  # run() gates the None case before calling here
        try:
            return self._observations.get_window(_BIOMETRIC_CHANNEL, start, end)
        except Exception:
            logger.error(
                "dream_biometrics: get_window failed; treating as empty for tonight's pass",
                exc_info=True,
            )
            return []

    async def run(self, ctx: StageContext) -> StageResult:
        now = ctx.clock()
        start = now - timedelta(days=_LOOKBACK_DAYS)

        if self._observations is None:
            # A legitimate toggle-off boot (the wombat_observe_biometrics consent default) — one
            # skip line, never an error, and tonight's biometrics pass simply derives nothing.
            logger.warning(
                "dream_biometrics: no ObservationStore (biometrics off or not constructed); "
                "skipping tonight's biometrics pass"
            )
            rows: list[dict[str, Any]] = []
        else:
            rows = self._read_channel(start, now)
            if not rows:
                logger.info(
                    "dream_biometrics: zero biometric rows in the lookback window; skipping "
                    "tonight's biometrics pass"
                )

        candidates = _derive_sleep_fact(rows) + _derive_resting_hr_fact(rows)
        total_candidates = len(candidates)
        if total_candidates > _MAX_BIOMETRIC_FACTS:
            logger.warning(
                "dream_biometrics: %d qualifying fact(s) exceed the %d-per-pass cap; keeping the "
                "first %d in deterministic order",
                total_candidates,
                _MAX_BIOMETRIC_FACTS,
                _MAX_BIOMETRIC_FACTS,
            )
        candidates = candidates[:_MAX_BIOMETRIC_FACTS]

        new_facts = 0
        if candidates:
            if self._user_facts is None:
                logger.error(
                    "dream_biometrics: user_facts store is None; skipping all %d write(s) for "
                    "tonight's pass",
                    len(candidates),
                )
            else:
                try:
                    existing_keys = _existing_fact_keys(self._user_facts)
                except Exception:
                    logger.error(
                        "dream_biometrics: reading existing facts for dedupe failed; treating "
                        "every candidate as new",
                        exc_info=True,
                    )
                    existing_keys = set()

                for fact_key, fact_text in candidates:
                    is_new = fact_key not in existing_keys
                    try:
                        self._user_facts.upsert_fact(fact_key, fact_text, source="behavior")
                    except Exception:
                        logger.error(
                            "dream_biometrics: upsert_fact failed for fact_key=%s; skipping",
                            fact_key,
                            exc_info=True,
                        )
                        continue
                    if is_new:
                        new_facts += 1
                        logger.info("dream_biometrics: accepted new fact fact_key=%s", fact_key)

        return Transition(
            to="dream_behavior_log",
            output=Artifact(
                kind=DREAM_BIOMETRICS_REPORT_KIND,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
                data={"new_facts": new_facts},
            ),
        )


__all__ = ["DREAM_BIOMETRICS_REPORT_KIND", "DreamBiometricsStage"]
