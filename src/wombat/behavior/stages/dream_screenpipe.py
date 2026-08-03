"""DreamScreenpipeStage — nightly bounded deterministic projection of the screenpipe record
through ONE budget-guarded model call, under the FULL ``dream_facts`` custody, writing
``source='behavior'`` user facts (TK-324, EP-37, DEC-70h).

Inserted into the ``wombat.dream`` graph immediately after ``dream_observe`` (TK-314) — the same
mechanical splice TK-314 made between ``dream_derive`` and ``dream_behavior_log``
(``pathways/dream_pathway.py``): ``dream_observe`` -> ``dream_screenpipe`` ->
``dream_behavior_log``.

Keyword-injected collaborators only (``DreamFactsStage``/``DreamObserveStage`` precedent):
``client`` is the composition-root ``wombat.integrations.screenpipe.client.ScreenpipeClient``
(TK-320) — ``None`` iff ``config.wombat_observe_screenpipe`` is false (a LEGITIMATE, structurally
inert boot state, mirrors ``DreamObserveStage``'s toggle-off shape); ``model`` is the SAME
budget-guarded ``Model`` every other dream-consolidation call site uses (DEC-23 — this stage NEVER
constructs a model or a second guard); ``user_facts`` is ``wombat.user_facts.UserFactsStore``
(TK-294), this stage's ONLY write path; ``tz`` is the configured local timezone (DEC-21), needed to
bucket screenpipe captures into local days/dayparts.

BEHAVIOR (RULING R-C, binding): ``client is None`` -> immediate onward ``Transition``, ZERO
client/model contact (structural inertness) — no log line, this is the ordinary toggle-off state.

Else, READ: a trailing ``_LOOKBACK_DAYS = 21`` window via ONE ``client.search`` call per local day
(at most 21 calls; ``ScreenpipeClient`` itself caps 50 results/search and never raises — a down or
misconfigured screenpipe degrades every call to ``[]`` after its own single per-streak WARNING,
DEC-70i — this stage adds no extra degrade handling on top of that). Each call is offloaded via
``asyncio.to_thread`` (ISS-37-RIDER batch-review repair — the same anti-precedent
``ScreenpipeEventSource.poll`` already established in this arc): ``client.search`` is a
synchronous, blocking ``urllib`` call under its own ~2.0s timeout, and this stage runs directly
inside the shared event loop's async ``run()`` — up to 21 sequential blocking calls would
otherwise freeze drain/ASR/chat/timers for up to ~42s during the nightly dream run.

FOLD (pure code, no model — DEC-70f): the raw ``ScreenpipeItem`` list is deterministically folded
into a bounded projection — top apps by capture-count residency, the top recurring window/document
title per top app, and day-part regularities (the ``dream_observe`` daypart windows, restated here)
— never ``text_snippet`` (the OCR body): only ``app``/``title`` display text ever reaches the
projection or the model. Pinned caps (DEC-63 no-knob): at most ``_MAX_PROJECTION_LINES = 20``
lines, at most ``_MAX_PROJECTION_CHARS = 1200`` total characters, each line capped at
``_MAX_PROJECTION_LINE_CHARS = 120`` — a deterministic prefix truncation (same fold, same lines,
every night), logged as one loud WARNING when it bites. Zero projection lines (an empty/sparse
21-day window) means ZERO model calls — the ordinary quiet-timeline case, ``run()`` still
transitions cleanly.

EXTRACT (DEC-23 admission — the ONE model call this stage ever makes, the ``DreamFactsStage`` seam
pattern): the system instruction asks for durable facts about the user's habits/routines the
projection SUPPORTS — one per line, third person — and embeds
``wombat.persona.expression.guard_suffix(Mouth.REFLECTION)`` VERBATIM (never a re-typed copy), the
SAME CON-6 never-clinical/never-motive bar ``DreamFactsStage`` carries. The user message is the
capped projection text, joined by newlines — the model NEVER sees a raw OCR dump. A raising
``model.complete`` degrades to an empty proposal (no facts land that night), logged loud.

DETERMINISTIC POST-FILTER (CUSTODY VERBATIM from ``dream_facts`` — the SAME
``_fact_key``/``_parse_candidates``/``_existing_fact_keys`` idiom, restated in this module rather
than imported, mirroring every sibling dream stage's own self-contained-helpers convention): parse
one-fact-per-line; drop any line over ``_MAX_FACT_LINE_CHARS`` (200) characters; drop any line whose
casefolded text contains a forbidden clinical/motive-inference token (the SAME screen
``dream_facts`` carries, restated verbatim); ``fact_key`` is a stable ``sha256`` hexdigest of the
casefolded/whitespace-collapsed fact text; a key already present in ``user_facts`` is skipped
(dedupe, read once); accepted candidates are capped at ``_MAX_NEW_FACTS_PER_NIGHT = 5`` — every
drop is logged loud and by reason. Each surviving fact is written via ``user_facts.upsert_fact(key,
fact, source="behavior")`` (DEC-70h's provenance ruling: the observational tier, regardless of
which distillation mechanism produced the fact) plus ONE INFO journal line per accepted fact
(CON-4).

NEVER BLOCKS: a raising dedupe read is caught loud and treated as "every candidate is new"; a
raising ``upsert_fact`` for one candidate is caught loud and skipped — the facts already upserted
before the failure stay written. ``run()`` ALWAYS ``Transition``s onward to ``dream_behavior_log``,
emitting a contentless system-provenance count ``Artifact`` (mirrors ``DREAM_TUNE_REPORT_KIND``'s
own idiom) — no fact text or projection text ever rides this artifact, only a count.

OUT OF SCOPE (TK-324 non-goals): no duplication of ``dream_observe``'s LLM-free templates (it
stands untouched); no fact deletion/decay; no audio-derived anything (DEF-16); no second model
call, no new store/table, no raw-text persistence, no render change (facts flow through the
existing ``known_user_context`` block automatically).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import StageResult, Transition
from cogworx.loop.stage import StageContext
from cogworx.model.base import ChatMessage, Model

from wombat.integrations.screenpipe.client import ScreenpipeItem
from wombat.persona.builder import Mouth
from wombat.persona.expression import guard_suffix
from wombat.user_facts import UserFactsStore

logger = logging.getLogger(__name__)

# DreamScreenpipeStage's committed output kind (TK-324) — a contentless, system-provenance count
# artifact mirroring dream_facts.py's own DREAM_FACTS_REPORT_KIND idiom: no fact/projection text
# rides this artifact, only a count — the durable record is the wombat_user_facts rows upserted.
DREAM_SCREENPIPE_REPORT_KIND = "wombat.dream_screenpipe_report"

# The trailing read window (RULING R-C, TK-324) — one client.search call per local day, at most
# this many calls a night. Not a tunable (DEC-63 no-knob precedent).
_LOOKBACK_DAYS = 21

# Pinned projection-fold shape (DEC-63 no-knob precedent) — never operator-tunable.
_MIN_RECURRING_TITLE_COUNT = 2
_MIN_DAYPART_COUNT = 3
_MIN_DAYPART_SHARE = 0.4

# Pinned projection caps (RULING R-C, DEC-70f) — the deterministic bound over what the model is
# ever shown; asserted directly by TK-324's acceptance tests.
_MAX_PROJECTION_LINES = 20
_MAX_PROJECTION_CHARS = 1200
_MAX_PROJECTION_LINE_CHARS = 120

# The dream_observe daypart windows, restated here (start hour inclusive, end hour exclusive) —
# this module's own copy, per the established per-stage self-contained-constants convention;
# dream_observe itself is untouched (TK-324 non-goal).
_DAYPARTS: tuple[tuple[str, int, int], ...] = (
    ("morning", 5, 12),
    ("afternoon", 12, 17),
    ("evening", 17, 22),
)

# Pinned hard cap (DEC-63 no-knob precedent, restated verbatim from dream_facts.py) — the
# deterministic custody over the model's proposal, never a setting.
_MAX_NEW_FACTS_PER_NIGHT = 5

# A candidate line longer than this is dropped loudly rather than truncated — restated verbatim
# from dream_facts.py (CUSTODY VERBATIM, TK-324 ruling).
_MAX_FACT_LINE_CHARS = 200

# dream_facts.py's own clinical/motive-inference term screen, restated verbatim here (CUSTODY
# VERBATIM, TK-324 ruling) — this stage's own extraction instruction also demands THIRD-PERSON
# output, so both phrasings are screened exactly as dream_facts screens them.
_FORBIDDEN_FACT_TOKENS: frozenset[str] = frozenset(
    {
        "clinical",
        "diagnosis",
        "disorder",
        "symptom",
        "therapy",
        "indicates a pattern",
        "you seem to",
        "seems to",
        "you tend to",
        "tends to",
        "because you",
        "because they",
        "due to your",
        "due to their",
    }
)

# The fixed extraction instruction — a fact request over the bounded projection, plus the
# reflection mouth's own immutable guard suffix, imported verbatim (never re-typed) so the CON-6
# bar can never drift out of sync with dream_facts's own copy (mirrors that module's ruling r3).
_EXTRACTION_INSTRUCTION = (
    "Below is a bounded summary of the user's recent screen activity: top apps by frequency, "
    "recurring window/document titles per app, and time-of-day usage patterns. Extract any "
    "durable facts about the user's own habits or routines that this summary supports. Write "
    "each fact as ONE line, in the third person (e.g. 'The user usually has email open in the "
    "morning.'), describing only what the summary shows. Output ONLY the fact lines and nothing "
    "else — if nothing durable is supported, output nothing. "
) + guard_suffix(Mouth.REFLECTION)


class ScreenpipeSearchClient(Protocol):
    """The one ``ScreenpipeClient`` method this stage needs (mirrors
    ``sources.screenpipe_source.ScreenpipeClientLike``'s minimal-seam convention, restated here
    rather than imported so this stage stays self-contained) — lets tests inject a fake/scripted
    client; production always wires the real ``ScreenpipeClient`` (``bootstrap.py``)."""

    def search(
        self,
        start: datetime,
        end: datetime,
        *,
        app_name: str | None = None,
        limit: int | None = None,
    ) -> list[ScreenpipeItem]: ...


def _normalize(text: str) -> str:
    """Whitespace-collapse + casefold — the SAME normalization ``dream_derive``/``dream_observe``
    apply before grouping app/title text."""
    return " ".join(text.split()).casefold()


def _sanitize_display(text: str) -> str:
    """Collapse every whitespace run (spaces, tabs, AND newlines) to a single space, then
    neutralize every literal ``;`` to ``,`` — the ONE shared egress-sanitization helper applied to
    any raw screenpipe ``app``/``title`` text before it ever reaches the projection or the model
    (post-batch-review repair, round 3). ``run()`` joins projection lines with ``"\\n"``
    (``_build_projection``'s docstring, DEC-70f), so an untrusted title carrying an interior
    newline would otherwise forge extra prompt lines — a 3-item projection rendering as 4 lines —
    that could pass this stage's own custody filters (``_parse_candidates``) straight into a
    durable ``source='behavior'`` fact. Applied BEFORE the existing per-line
    ``_MAX_PROJECTION_LINE_CHARS`` clamp (``candidate_lines`` below), never after."""
    return " ".join(text.split()).replace(";", ",")


def _daypart_of(hour: int) -> str | None:
    for name, start_hour, end_hour in _DAYPARTS:
        if start_hour <= hour < end_hour:
            return name
    return None


def _build_projection(items: list[ScreenpipeItem], tz: ZoneInfo) -> list[str]:
    """Fold raw ``ScreenpipeItem`` rows into a bounded, deterministic projection (RULING R-C):
    top apps by capture-count residency, the top recurring title per top app, and day-part
    regularities — never ``text_snippet`` (DEC-70f). Truncated to ``_MAX_PROJECTION_LINES``/
    ``_MAX_PROJECTION_CHARS``/``_MAX_PROJECTION_LINE_CHARS`` as a stable prefix (same fold, same
    lines survive, every night) — exceeding either cap logs ONE loud WARNING."""
    app_counts: dict[str, int] = {}
    app_display: dict[str, str] = {}
    title_counts: dict[tuple[str, str], int] = {}
    title_display: dict[tuple[str, str], str] = {}
    daypart_counts: dict[str, dict[str, int]] = {}

    for item in items:
        app_norm = _normalize(item.app)
        if not app_norm:
            continue
        app_counts[app_norm] = app_counts.get(app_norm, 0) + 1
        app_display.setdefault(app_norm, _sanitize_display(item.app))

        title_norm = _normalize(item.title)
        if title_norm:
            title_key = (app_norm, title_norm)
            title_counts[title_key] = title_counts.get(title_key, 0) + 1
            title_display.setdefault(title_key, _sanitize_display(item.title))

        daypart = _daypart_of(item.captured_at.astimezone(tz).hour)
        if daypart is not None:
            part_counts = daypart_counts.setdefault(daypart, {})
            part_counts[app_norm] = part_counts.get(app_norm, 0) + 1

    # No per-section top-N here BY DESIGN — the pinned _MAX_PROJECTION_LINES/_MAX_PROJECTION_CHARS
    # caps below are the ONE deterministic bound (a stable prefix truncation over every app), not
    # a second, redundant restriction. ``ranked_apps`` fixes the ONE deterministic order (most
    # captures first, app name breaks ties) every section below reuses.
    ranked_apps = sorted(app_counts.items(), key=lambda kv: (-kv[1], kv[0]))

    residency_lines = [
        f"Top app: {app_display[app_norm]} ({count} capture{'s' if count != 1 else ''})"
        for app_norm, count in ranked_apps
    ]

    title_lines: list[str] = []
    for app_norm, _count in ranked_apps:
        app_titles = [
            (title_norm, cnt)
            for (a, title_norm), cnt in title_counts.items()
            if a == app_norm
        ]
        if not app_titles:
            continue
        top_title_norm, top_title_count = max(app_titles, key=lambda kv: (kv[1], kv[0]))
        if top_title_count < _MIN_RECURRING_TITLE_COUNT:
            continue
        title_text = title_display[(app_norm, top_title_norm)]
        title_lines.append(
            f"{app_display[app_norm]} recurring title: {title_text} ({top_title_count}x)"
        )

    daypart_lines: list[str] = []
    for daypart_name, _start_hour, _end_hour in _DAYPARTS:
        daypart_app_counts = daypart_counts.get(daypart_name)
        if not daypart_app_counts:
            continue
        total = sum(daypart_app_counts.values())
        if total < _MIN_DAYPART_COUNT:
            continue
        top_app_norm, top_count = sorted(
            daypart_app_counts.items(), key=lambda kv: (-kv[1], kv[0])
        )[0]
        if top_count / total < _MIN_DAYPART_SHARE:
            continue
        daypart_lines.append(f"{daypart_name.capitalize()}s mostly {app_display[top_app_norm]}")

    candidate_lines = [
        line[:_MAX_PROJECTION_LINE_CHARS] for line in residency_lines + title_lines + daypart_lines
    ]

    result: list[str] = []
    total_chars = 0
    for line in candidate_lines:
        if len(result) >= _MAX_PROJECTION_LINES:
            break
        prospective_chars = total_chars + len(line) + (1 if result else 0)
        if prospective_chars > _MAX_PROJECTION_CHARS:
            break
        result.append(line)
        total_chars = prospective_chars

    dropped = len(candidate_lines) - len(result)
    if dropped > 0:
        logger.warning(
            "dream_screenpipe: %d projection line(s) dropped to respect the %d-line/%d-char "
            "caps; kept the first %d in deterministic order",
            dropped,
            _MAX_PROJECTION_LINES,
            _MAX_PROJECTION_CHARS,
            len(result),
        )
    return result


def _fact_key(text: str) -> str:
    """A stable ``sha256`` hexdigest of the casefolded, whitespace-collapsed fact text — restated
    verbatim from ``dream_facts._fact_key`` (CUSTODY VERBATIM, TK-324 ruling)."""
    normalized = " ".join(text.split()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _parse_candidates(raw_text: str) -> list[str]:
    """One-fact-per-line parse, dropping blank lines, over-long lines, and forbidden-token lines
    — restated verbatim from ``dream_facts._parse_candidates`` (CUSTODY VERBATIM, TK-324 ruling).
    Each drop is logged loud and by reason; order-preserving; the cap is enforced by the caller."""
    candidates: list[str] = []
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if len(line) > _MAX_FACT_LINE_CHARS:
            logger.warning(
                "dream_screenpipe: dropping over-long candidate line (%d chars): %r",
                len(line),
                line,
            )
            continue
        casefolded = line.casefold()
        hit = next((token for token in _FORBIDDEN_FACT_TOKENS if token in casefolded), None)
        if hit is not None:
            logger.warning(
                "dream_screenpipe: dropping forbidden-token candidate line (token=%r): %r",
                hit,
                line,
            )
            continue
        candidates.append(line)
    return candidates


def _existing_fact_keys(user_facts: UserFactsStore) -> set[str]:
    """Every ``fact_key`` already in ``user_facts`` — read ONCE per run, restated verbatim from
    ``dream_facts._existing_fact_keys`` (CUSTODY VERBATIM, TK-324 ruling)."""
    total = user_facts.count()
    if total == 0:
        return set()
    return {row["fact_key"] for row in user_facts.list_facts(total)}


class DreamScreenpipeStage:
    """The nightly bounded screenpipe-record projection pass (TK-324, EP-37, DEC-70h). See the
    module docstring for the full read/fold/extract/filter/write contract."""

    name: str = "dream_screenpipe"
    transitions: tuple[str, ...] = ("dream_biometrics",)

    def __init__(
        self,
        *,
        client: ScreenpipeSearchClient | None,
        model: Model,
        user_facts: UserFactsStore,
        tz: ZoneInfo,
    ) -> None:
        self._client = client
        self._model = model
        self._user_facts = user_facts
        self._tz = tz

    async def _collect_items(self, now: datetime) -> list[ScreenpipeItem]:
        """ISS-37-RIDER batch-review repair: each ``client.search`` call is a blocking
        ``urllib`` call under its own ~2.0s timeout (``ScreenpipeClient``) — offloaded via
        ``asyncio.to_thread`` (the SAME anti-precedent ``ScreenpipeEventSource.poll`` already
        established in this arc) so up to 21 sequential searches never freeze the shared event
        loop (drain/ASR/chat/timers) for up to ~42s during the nightly dream run."""
        assert self._client is not None  # run() gates the None case before calling here
        items: list[ScreenpipeItem] = []
        today_local = now.astimezone(self._tz).date()
        for day_offset in range(_LOOKBACK_DAYS):
            day = today_local - timedelta(days=day_offset)
            day_start = datetime.combine(day, time.min, tzinfo=self._tz)
            day_end = day_start + timedelta(days=1)
            items.extend(await asyncio.to_thread(self._client.search, day_start, day_end))
        return items

    async def run(self, ctx: StageContext) -> StageResult:
        now = ctx.clock()
        new_facts = 0

        if self._client is not None:
            items = await self._collect_items(now)
            projection_lines = _build_projection(items, self._tz)

            if projection_lines:
                projection_text = "\n".join(projection_lines)
                try:
                    response = await self._model.complete(
                        messages=[
                            ChatMessage(role="system", content=_EXTRACTION_INSTRUCTION),
                            ChatMessage(role="user", content=projection_text),
                        ]
                    )
                    raw_text = response.text or ""
                except Exception:
                    logger.error(
                        "dream_screenpipe: model extraction call failed; tonight's screenpipe "
                        "pass yields no facts",
                        exc_info=True,
                    )
                    raw_text = ""

                candidates = _parse_candidates(raw_text)
                if candidates:
                    try:
                        existing_keys = _existing_fact_keys(self._user_facts)
                    except Exception:
                        logger.error(
                            "dream_screenpipe: reading existing facts for dedupe failed; "
                            "proceeding as if no facts exist yet",
                            exc_info=True,
                        )
                        existing_keys = set()

                    for candidate in candidates:
                        if new_facts >= _MAX_NEW_FACTS_PER_NIGHT:
                            break
                        key = _fact_key(candidate)
                        if key in existing_keys:
                            logger.info(
                                "dream_screenpipe: dropping duplicate candidate fact_key=%s", key
                            )
                            continue
                        try:
                            self._user_facts.upsert_fact(key, candidate, source="behavior")
                        except Exception:
                            logger.error(
                                "dream_screenpipe: upsert_fact failed for fact_key=%s; skipping",
                                key,
                                exc_info=True,
                            )
                            continue
                        existing_keys.add(key)
                        new_facts += 1
                        logger.info("dream_screenpipe: accepted new fact fact_key=%s", key)

        return Transition(
            to="dream_biometrics",
            output=Artifact(
                kind=DREAM_SCREENPIPE_REPORT_KIND,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
                data={"new_facts": new_facts},
            ),
        )


__all__ = [
    "DREAM_SCREENPIPE_REPORT_KIND",
    "DreamScreenpipeStage",
    "ScreenpipeSearchClient",
]
