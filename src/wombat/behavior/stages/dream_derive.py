"""DreamDeriveStage — deterministic observational user-model facts, PURE CODE, NO LLM (TK-299,
EP-37, DEC-66 first ticket).

Inserted into the ``wombat.dream`` graph immediately after ``dream_facts`` (TK-297) — the same
mechanical splice TK-297 made between ``dream_persona`` and ``dream_behavior_log``
(``pathways/dream_pathway.py``): ``dream_facts`` -> ``dream_derive`` -> ``dream_behavior_log``.

Keyword-injected collaborators only (``DreamFactsStage`` precedent): ``external_items`` is
``wombat.external_store.ExternalItemStore`` (TK-244/TK-245), this stage's ONLY read path;
``user_facts`` is ``wombat.user_facts.UserFactsStore`` (TK-294), this stage's ONLY write path.
Both are typed ``| None`` solely so the AC3 degrade shape (a store that is literally absent) can be
exercised and type-checks under strict mypy — production wiring (``bootstrap.py``) constructs both
unconditionally with real instances, so ``None`` is never expected in practice; seeing it anyway is
logged as a loud ERROR (not a silent skip, unlike an ordinary unwired-boot convention elsewhere in
this codebase) because this stage's collaborators are supposed to always be present.

READ: over a fixed ``_LOOKBACK_DAYS = 28`` trailing window ending at ``ctx.clock()`` (retention is
``external_store.EXTERNAL_ITEMS_PRUNE_DAYS = 30`` — the 28-day lookback fits with a 2-day margin,
TK-299 briefing-time ruling), ``external_items.get_window("gcal", start, now)`` and
``external_items.get_window("gmail", start, now)`` — ``get_window`` is used for BOTH sources
(rather than ``get_recent``) since deriving a rhythm/frequency needs every row inside a bounded
calendar window, not just the N most recent rows. A ``None`` store, or either call raising, is
caught PER SOURCE and treated as zero rows for that source (logged loud) — never blocks the other
source's read.

DERIVE (pure code, two independent templates):

  (a) recurring meetings — group non-all-day ``gcal`` rows by (casefolded/whitespace-collapsed
      title, weekday, start time rounded to the nearest 30 minutes — this stage's concrete
      reading of "within a 30-minute tolerance", since no clustering algorithm is pinned).  A
      group spanning ``>= _MIN_RECURRENCE_WEEKS = 3`` DISTINCT ISO (year, week) pairs qualifies:
      ``"Has <title> on <weekday>s around <HH:MM>"`` (the earliest occurrence's own title text,
      un-normalized, is the rendered ``<title>``). Stable key:
      ``derived:meeting:<slug(title)>:<weekday lowercase>``.
  (b) frequent correspondents — group ``gmail`` rows by casefolded/whitespace-collapsed
      ``sender``. A group with ``>= _MIN_CORRESPONDENT_COUNT = 5`` rows in the window qualifies:
      ``"Corresponds often with <sender>"`` (the first occurrence's own sender text is rendered).
      Stable key: ``derived:correspondent:<slug(sender)>``.

CAP + ORDER (DEC-63 no-knob, pinned): all qualifying facts are combined — every meeting fact
(sorted by its own key) THEN every correspondent fact (sorted by its own key) — and truncated to
``_MAX_DERIVED_FACTS = 5``. Exceeding the cap is logged as ONE loud WARNING naming how many
qualifying facts were found; which facts survive is fully deterministic (same seed, same 5 every
night).

WRITE: each surviving fact is idempotently written via ``user_facts.upsert_fact(key, fact,
source="derived")`` — the key is ALREADY stable/derived from the data itself (unlike
``dream_facts``'s hashed-arbitrary-text keys), so a re-derivation on a later night naturally
re-upserts the SAME key with the SAME text (idempotent by construction, AC2 — no separate dedupe
store needed to avoid duplicate rows). A key not already present in ``user_facts`` (read once via
``count``/``list_facts``, mirrors ``dream_facts``'s own single dedupe-read shape) is "NEW": exactly
ONE INFO journal line per NEW fact (CON-4) — a re-upserted already-known fact logs nothing (would
otherwise repeat the same journal line every single night forever). A raising dedupe read is caught
loud and treated as "every candidate is new" (mirrors ``dream_facts``'s own fallback); a raising
``upsert_fact`` for one candidate is caught loud and skipped — the facts already upserted before the
failure stay written.

NEVER BLOCKS: zero qualifying rows is the ordinary case (no error, no writes, nothing to log) — an
empty/sparse store is NOT an anomaly. A missing (``None``) or raising store IS logged loud (see
above), but the stage STILL transitions onward (the dream posture) — this pass never touches
``ctx.journal`` and never calls a model (NG-4 intact, DEC-66: this is the deliberately
LLM-free-first observational input).

CON-6 by construction: both templates are fixed third-person sentences filled ONLY with observed
title/weekday/time/sender text — there is no branch, no free-text field, and no model call through
which a motive/judgment phrase could ever enter.

OUT OF SCOPE (recorded v1 simplification): no fact deletion/decay when a regularity lapses — the
``UserFactsStore`` cap (``_MAX_FACTS = 200``) is the only eviction path; no new store, no new
config/tunable, no render change (derived facts flow through the EXISTING TK-296
``known_user_context`` block automatically, since they land in the same ``wombat_user_facts``
table every other tier does).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import StageResult, Transition
from cogworx.loop.stage import StageContext

from wombat.external_store import ExternalItemStore
from wombat.user_facts import UserFactsStore

logger = logging.getLogger(__name__)

# DreamDeriveStage's committed output kind (TK-299) — a contentless, system-provenance count
# artifact mirroring dream_facts.py's own DREAM_FACTS_REPORT_KIND idiom: no fact text rides this
# artifact, only a count — the durable record is the wombat_user_facts rows the stage upserted.
DREAM_DERIVE_REPORT_KIND = "wombat.dream_derive_report"

# The trailing read window (TK-299 briefing-time ruling): EXTERNAL_ITEMS_PRUNE_DAYS=30 retention
# leaves a 2-day margin over the >=3-distinct-week recurrence bar below — not a tunable, a module
# constant (DEC-63 no-knob precedent).
_LOOKBACK_DAYS = 28

# Pinned recurrence/frequency bars (DEC-63 no-knob precedent) — never operator-tunable.
_MIN_RECURRENCE_WEEKS = 3
_MIN_CORRESPONDENT_COUNT = 5

# Pinned hard cap (DEC-63 no-knob precedent, mirrors dream_facts.py's _MAX_NEW_FACTS_PER_NIGHT) —
# the deterministic custody over how many facts one pass ever writes, never a setting.
_MAX_DERIVED_FACTS = 5

# This stage's concrete reading of "start time within a 30-minute tolerance": round to the nearest
# half hour before grouping — not itself a tunable (no clustering algorithm is pinned by the
# ticket; this is the simplest deterministic proxy for it).
_ROUND_MINUTES = 30

_WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

_SLUG_INVALID_RE = re.compile(r"[^a-z0-9]+")


def _slugify(casefolded_text: str) -> str:
    """A stable, URL/key-safe slug from already-casefolded text — runs of anything other than
    ``[a-z0-9]`` collapse to one hyphen, with leading/trailing hyphens stripped. Never empty
    (falls back to ``"item"``) so a punctuation-only title/sender can never yield a malformed
    key."""
    slug = _SLUG_INVALID_RE.sub("-", casefolded_text).strip("-")
    return slug or "item"


def _round_to_half_hour(hour: int, minute: int) -> tuple[int, int]:
    """Round ``hour:minute`` to the nearest ``_ROUND_MINUTES``-minute mark, wrapping past
    midnight — the bucketing proxy for the "30-minute tolerance" recurrence rule."""
    total = hour * 60 + minute
    rounded = round(total / _ROUND_MINUTES) * _ROUND_MINUTES
    rounded %= 24 * 60
    return divmod(rounded, 60)


def _derive_meeting_facts(rows: list[dict[str, Any]], tz: ZoneInfo) -> list[tuple[str, str]]:
    """Group non-all-day ``gcal`` rows by (normalized title, weekday, rounded start time) and
    return one ``(fact_key, fact_text)`` per group spanning ``>= _MIN_RECURRENCE_WEEKS`` distinct
    ISO weeks — sorted by ``fact_key`` for deterministic, stable ordering.

    ``start`` is stored UTC by the gcal poller (``integrations/gcal/poller.py``); weekday/time are
    derived AFTER converting to ``tz`` (mirrors ``voice.context_prefetch``'s sibling renderer,
    TK-290) so a Monday-evening standup in the user's own timezone is never bucketed onto the
    following UTC calendar day."""
    groups: dict[tuple[str, int, int, int], dict[str, Any]] = {}
    for row in rows:
        payload = row.get("payload") or {}
        title = payload.get("title")
        start_raw = payload.get("start")
        if not title or not start_raw or payload.get("all_day"):
            continue
        try:
            start_dt = datetime.fromisoformat(start_raw).astimezone(tz)
        except ValueError:
            continue
        normalized_title = " ".join(str(title).split()).casefold()
        if not normalized_title:
            continue
        weekday = start_dt.weekday()
        rounded_h, rounded_m = _round_to_half_hour(start_dt.hour, start_dt.minute)
        key = (normalized_title, weekday, rounded_h, rounded_m)
        iso_year, iso_week, _iso_weekday = start_dt.isocalendar()
        group = groups.setdefault(key, {"weeks": set(), "title": str(title).strip()})
        group["weeks"].add((iso_year, iso_week))

    facts: list[tuple[str, str]] = []
    for (normalized_title, weekday, rounded_h, rounded_m), group in groups.items():
        if len(group["weeks"]) < _MIN_RECURRENCE_WEEKS:
            continue
        weekday_name = _WEEKDAY_NAMES[weekday]
        fact_key = f"derived:meeting:{_slugify(normalized_title)}:{weekday_name.lower()}"
        time_str = f"{rounded_h:02d}:{rounded_m:02d}"
        fact_text = f"Has {group['title']} on {weekday_name}s around {time_str}"
        facts.append((fact_key, fact_text))

    facts.sort(key=lambda item: item[0])
    return facts


def _derive_correspondent_facts(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Group ``gmail`` rows by normalized ``sender`` and return one ``(fact_key, fact_text)`` per
    sender appearing ``>= _MIN_CORRESPONDENT_COUNT`` times in the window — sorted by ``fact_key``
    for deterministic, stable ordering."""
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = row.get("payload") or {}
        sender = payload.get("sender")
        if not sender:
            continue
        normalized_sender = " ".join(str(sender).split()).casefold()
        if not normalized_sender:
            continue
        group = groups.setdefault(normalized_sender, {"count": 0, "sender": str(sender).strip()})
        group["count"] += 1

    facts: list[tuple[str, str]] = []
    for normalized_sender, group in groups.items():
        if group["count"] < _MIN_CORRESPONDENT_COUNT:
            continue
        fact_key = f"derived:correspondent:{_slugify(normalized_sender)}"
        fact_text = f"Corresponds often with {group['sender']}"
        facts.append((fact_key, fact_text))

    facts.sort(key=lambda item: item[0])
    return facts


def _existing_fact_keys(user_facts: UserFactsStore) -> set[str]:
    """Every ``fact_key`` already in ``user_facts`` — read ONCE per run (``count`` then
    ``list_facts(count)``), mirrors ``dream_facts._existing_fact_keys`` exactly."""
    total = user_facts.count()
    if total == 0:
        return set()
    return {row["fact_key"] for row in user_facts.list_facts(total)}


class DreamDeriveStage:
    """Deterministic observational user-model facts — recurring meetings + frequent
    correspondents, PURE CODE, NO LLM (TK-299, EP-37, DEC-66). See the module docstring for the
    full read/derive/cap/write contract."""

    name: str = "dream_derive"
    transitions: tuple[str, ...] = ("dream_behavior_log",)

    def __init__(
        self,
        *,
        external_items: ExternalItemStore | None,
        user_facts: UserFactsStore | None,
        tz: ZoneInfo,
    ) -> None:
        self._external_items = external_items
        self._user_facts = user_facts
        self._tz = tz

    def _read_window(self, source: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        if self._external_items is None:
            logger.error(
                "dream_derive: external_items store is None; treating source=%r as empty for "
                "tonight's pass",
                source,
            )
            return []
        try:
            return self._external_items.get_window(source, start, end)
        except Exception:
            logger.error(
                "dream_derive: get_window(source=%r) failed; treating as empty for tonight's "
                "pass",
                source,
                exc_info=True,
            )
            return []

    async def run(self, ctx: StageContext) -> StageResult:
        now = ctx.clock()
        start = now - timedelta(days=_LOOKBACK_DAYS)

        gcal_rows = self._read_window("gcal", start, now)
        gmail_rows = self._read_window("gmail", start, now)

        candidates = _derive_meeting_facts(gcal_rows, self._tz) + _derive_correspondent_facts(
            gmail_rows
        )
        total_candidates = len(candidates)
        if total_candidates > _MAX_DERIVED_FACTS:
            logger.warning(
                "dream_derive: %d qualifying fact(s) exceed the %d-per-pass cap; keeping the "
                "first %d in deterministic order",
                total_candidates,
                _MAX_DERIVED_FACTS,
                _MAX_DERIVED_FACTS,
            )
        candidates = candidates[:_MAX_DERIVED_FACTS]

        new_facts = 0
        if candidates:
            if self._user_facts is None:
                logger.error(
                    "dream_derive: user_facts store is None; skipping all %d write(s) for "
                    "tonight's pass",
                    len(candidates),
                )
            else:
                try:
                    existing_keys = _existing_fact_keys(self._user_facts)
                except Exception:
                    logger.error(
                        "dream_derive: reading existing facts for dedupe failed; treating every "
                        "candidate as new",
                        exc_info=True,
                    )
                    existing_keys = set()

                for fact_key, fact_text in candidates:
                    is_new = fact_key not in existing_keys
                    try:
                        self._user_facts.upsert_fact(fact_key, fact_text, source="derived")
                    except Exception:
                        logger.error(
                            "dream_derive: upsert_fact failed for fact_key=%s; skipping",
                            fact_key,
                            exc_info=True,
                        )
                        continue
                    if is_new:
                        new_facts += 1
                        logger.info("dream_derive: accepted new fact fact_key=%s", fact_key)

        return Transition(
            to="dream_behavior_log",
            output=Artifact(
                kind=DREAM_DERIVE_REPORT_KIND,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
                data={"new_facts": new_facts},
            ),
        )


__all__ = ["DREAM_DERIVE_REPORT_KIND", "DreamDeriveStage"]
