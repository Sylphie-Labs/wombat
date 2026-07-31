"""context_prefetch — deterministic today-gcal + recent-gmail grounding for voice turns (TK-290,
DEC-64 gap B).

``build_voice_context(store, tz=..., clock=...)`` reads the SAME ``wombat_external_items`` table
TK-244/TK-245 already populate (``external_store.ExternalItemStore``) and renders AT MOST TWO
payload fields, both plain compact text under HARD pinned caps (DEC-63 no-knob precedent — not
operator-tunable):

  ``context_calendar_today`` — ``store.get_window("gcal", start, end)`` over the CURRENT
      civil-local day in ``tz`` (DEC-21 ``wombat_today`` discipline), computed at call time.
      V2.151 ruling: bounds are the tz-local day 00:00:00 through 23:59:59.999999 as AWARE
      datetimes; ``get_window``'s SQL filter is inclusive both ends and does the exclusion
      (``external_store.py``), so this module never re-filters by window on the renderer side.
      Capped at 10 items / 800 chars.
  ``context_recent_email`` — ``store.get_recent("gmail", 5)`` (DESC LIMIT then re-sorted ascending
      by ``occurs_at`` inside the store), rendered in the store's returned order. Capped at 5
      items / 400 chars.

A source with zero rows contributes NO key (never an empty string). ``store`` being ``None``, or
ANY exception raised by either store call, degrades to an empty dict plus exactly ONE WARNING
(CON-3: grounding is strictly additive — a pg hiccup never blocks the turn).

V2.151 ruling, structural (DEC-26 held by construction): the gmail row's ``payload`` carries
EXACTLY ``{message_id, subject, sender, received_at, priority_band}`` (the DEC-45 projection,
``sources/bootstrap.py``'s ``build_external_item_sink``) — the full message content has no key in
that table at all, so this module reads ONLY the five named projection fields above.

``build_user_facts_context(store)`` (TK-296, DEC-65f) is the sibling grounding builder for the
durable what-wombat-knows-about-the-user store (``user_facts.UserFactsStore``): AT MOST
``{"known_user_context": one-fact-per-line block}``, reading ``store.list_facts(_MAX_FACTS_LINES)``
and rendering via the SAME ``_render`` helper under NEW pinned caps (``_MAX_FACTS_LINES`` = 15 /
``_MAX_FACTS_CHARS`` = 900, DEC-63 no-knob precedent). Degrade shape mirrors
``build_voice_context`` EXACTLY: ``store=None`` -> ``{}`` no call no warning, zero rows -> no key,
any raise -> ``{}`` plus exactly ONE loud warning (CON-3).

``build_current_activity_context(current_activity)`` (TK-311, DEC-68(d)(1)) is the third sibling
grounding builder, this one over ``observations.CurrentActivity``'s in-memory single-slot
now-snapshot rather than a Postgres store: AT MOST ``{"current_activity": "<app> - <title>"}``,
with `` (in a call)`` appended when ``in_call`` is true, truncated at ``_MAX_ACTIVITY_CHARS`` = 160
(pinned, DEC-63 no-knob precedent). ``current_activity.app`` is ``observe_screen.py``'s raw
``QueryFullProcessImageNameW`` process image path (e.g. ``C:/Program Files/.../notepad.exe``) —
this renderer reduces it to the bare executable basename (``_process_basename``, batch-review
repair) before rendering, so a long install path never eats the ``_MAX_ACTIVITY_CHARS`` budget the
title needs and no ``C:/Users/<name>`` filesystem path leaks into the prompt. ``current_activity=
None`` (collector absent/toggle off) -> ``{}`` no read no warning. A stale/absent snapshot (``app``
or ``title`` is ``None`` — the collector's own closed-segment state — OR ``since`` older than
``observations._STALE_AFTER_SECONDS``, batch-review repair: a dead poller/machine-sleep snapshot
renders absent, never as live) -> no key. ANY exception
reading the snapshot's fields -> ``{}`` plus exactly ONE loud warning (CON-3 parity with the two
builders above).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, time
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from wombat.domain.daily_ledger import wombat_today
from wombat.observations import _STALE_AFTER_SECONDS

logger = logging.getLogger(__name__)

# Pinned caps (DEC-63 no-knob precedent — NOT operator-tunable): the hard spend/prompt-size guard
# for this grounding bundle, independent of compose.py's own daily token ceiling.
_GCAL_MAX_ITEMS = 10
_GCAL_MAX_CHARS = 800
_GMAIL_LIMIT = 5
_GMAIL_MAX_CHARS = 400

# TK-296 (DEC-65f): the known_user_context caps — same no-knob precedent as the pair above.
_MAX_FACTS_LINES = 15
_MAX_FACTS_CHARS = 900

# TK-311 (DEC-68(d)(1)): the current_activity line cap — same no-knob precedent.
_MAX_ACTIVITY_CHARS = 160

# The in-call marker appended whole (batch-review repair: the app-title part is truncated FIRST so
# this suffix is never itself cut mid-word by the _MAX_ACTIVITY_CHARS cap).
_IN_CALL_SUFFIX = " (in a call)"


class VoiceContextStore(Protocol):
    """The structural shape ``build_voice_context`` needs from a store — matches
    ``external_store.ExternalItemStore``'s ``get_window``/``get_recent`` exactly (mirrors the
    ``TokenStore``/``VoiceKeyStore`` Protocol convention elsewhere in this codebase). A test fake
    only needs to satisfy this shape, never import ``ExternalItemStore`` itself."""

    def get_window(self, source: str, start: datetime, end: datetime) -> list[dict[str, Any]]: ...

    def get_recent(self, source: str, limit: int) -> list[dict[str, Any]]: ...


class UserFactsContextStore(Protocol):
    """The structural shape ``build_user_facts_context`` needs from a store — matches
    ``user_facts.UserFactsStore.list_facts`` exactly (mirrors ``VoiceContextStore`` above); a
    test fake only needs to satisfy this shape, never import ``UserFactsStore`` itself."""

    def list_facts(self, limit: int) -> list[dict[str, Any]]: ...


class ActivitySnapshot(Protocol):
    """The structural shape ``build_current_activity_context`` needs from a now-snapshot — matches
    ``observations.CurrentActivity``'s ``app``/``title``/``in_call`` fields exactly (mirrors the
    ``VoiceContextStore``/``UserFactsContextStore`` Protocol convention above); a test fake only
    needs to satisfy this shape, never import ``CurrentActivity`` itself. Declared as read-only
    properties (this function only ever reads them) so both a plain mutable dataclass field
    (``CurrentActivity``) and a raising ``@property`` test fake satisfy it structurally."""

    @property
    def app(self) -> str | None: ...

    @property
    def title(self) -> str | None: ...

    @property
    def in_call(self) -> bool: ...

    @property
    def since(self) -> datetime | None: ...


def build_voice_context(
    store: VoiceContextStore | None,
    *,
    tz: ZoneInfo,
    clock: Callable[[], datetime],
) -> dict[str, str]:
    """Return AT MOST ``{"context_calendar_today": ..., "context_recent_email": ...}``.

    ``store`` is ``None`` on an unwired boot: returns ``{}`` immediately, no call, no warning.
    Otherwise both store calls are made inside ONE guarded block — either call raising degrades
    the WHOLE result to ``{}`` plus exactly ONE loud warning (never a partial result, never a
    second warning for the second call).
    """
    if store is None:
        return {}
    try:
        today = wombat_today(clock(), tz)
        start = datetime.combine(today, time.min, tzinfo=tz)
        end = datetime.combine(today, time(23, 59, 59, 999999), tzinfo=tz)
        gcal_rows = store.get_window("gcal", start, end)
        gmail_rows = store.get_recent("gmail", _GMAIL_LIMIT)
    except Exception:
        logger.warning(
            "build_voice_context: store raised — proceeding with no voice-context payload",
            exc_info=True,
        )
        return {}

    result: dict[str, str] = {}
    if gcal_rows:
        rendered = _render(
            (_gcal_line(row, tz) for row in gcal_rows),
            max_items=_GCAL_MAX_ITEMS,
            max_chars=_GCAL_MAX_CHARS,
        )
        if rendered:
            result["context_calendar_today"] = rendered
    if gmail_rows:
        rendered = _render(
            (_gmail_line(row, tz) for row in gmail_rows),
            max_items=_GMAIL_LIMIT,
            max_chars=_GMAIL_MAX_CHARS,
        )
        if rendered:
            result["context_recent_email"] = rendered
    return result


def build_user_facts_context(store: UserFactsContextStore | None) -> dict[str, str]:
    """Return AT MOST ``{"known_user_context": ...}`` — one fact per line, deterministically
    truncated at ``_MAX_FACTS_LINES`` items / ``_MAX_FACTS_CHARS`` chars (TK-296, DEC-65f).

    ``store`` is ``None`` on an unwired boot: returns ``{}`` immediately, no call, no warning.
    Zero rows contributes NO key (never an empty string). ANY exception raised by
    ``store.list_facts`` degrades to ``{}`` plus exactly ONE loud warning (CON-3 parity with
    ``build_voice_context``).
    """
    if store is None:
        return {}
    try:
        rows = store.list_facts(_MAX_FACTS_LINES)
    except Exception:
        logger.warning(
            "build_user_facts_context: store raised — proceeding with no known-user-context "
            "payload",
            exc_info=True,
        )
        return {}
    if not rows:
        return {}
    rendered = _render(
        (row["fact"] for row in rows), max_items=_MAX_FACTS_LINES, max_chars=_MAX_FACTS_CHARS
    )
    if not rendered:
        return {}
    return {"known_user_context": rendered}


def _process_basename(path: str) -> str:
    """The bare executable filename (e.g. ``notepad.exe``) from a full process image path.

    ``observe_screen.py``'s ``QueryFullProcessImageNameW`` reading is a raw Windows filesystem
    path — often 70+ chars of install directory before the executable name, and frequently a
    ``C:/Users/<name>`` segment. Splitting on BOTH separators (never just ``os.sep``) since this
    text always comes from a Windows API regardless of what platform the renderer itself runs on.
    A trailing-separator or otherwise-empty tail falls back to the original string unchanged
    (never renders an empty app name)."""
    tail = path.replace("\\", "/").rsplit("/", 1)[-1]
    return tail or path


def build_current_activity_context(
    current_activity: ActivitySnapshot | None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, str]:
    """Return AT MOST ``{"current_activity": "<app> - <title>"}`` (TK-311, DEC-68(d)(1)), with
    `` (in a call)`` appended when ``in_call`` is true, truncated at ``_MAX_ACTIVITY_CHARS``
    (the app-title part is cut FIRST, then the suffix appended whole — batch-review repair: the
    old order truncated the suffix itself mid-word on a long title).

    ``current_activity`` is ``None`` (collector absent/toggle off): returns ``{}`` immediately, no
    read, no warning. A stale/absent snapshot — ``app`` or ``title`` is ``None`` (the collector's
    own closed-segment state), OR ``since`` older than ``observations._STALE_AFTER_SECONDS``
    against ``clock()`` (batch-review repair: a dead poller or a machine waking from sleep must
    not present a stale window as live — absent, never wrong; ``since=None`` with app/title set
    carries no age and renders as before) — contributes NO key (never an empty string). ANY
    exception raised reading the snapshot's fields degrades to ``{}`` plus exactly ONE loud
    warning (CON-3 parity with ``build_voice_context``/``build_user_facts_context``). ``app`` is
    reduced to its bare executable basename (``_process_basename``, batch-review repair) before
    rendering — see the module docstring. ``clock`` defaults to aware-UTC now (injectable for
    tests only — callers pass nothing).
    """
    if current_activity is None:
        return {}
    try:
        app = current_activity.app
        title = current_activity.title
        in_call = current_activity.in_call
        since = current_activity.since
        stale = since is not None and (
            ((clock() if clock is not None else datetime.now(UTC)) - since).total_seconds()
            > _STALE_AFTER_SECONDS
        )
    except Exception:
        logger.warning(
            "build_current_activity_context: snapshot read raised — proceeding with no "
            "current-activity payload",
            exc_info=True,
        )
        return {}
    if app is None or title is None or stale:
        return {}
    line = f"{_process_basename(app)} - {title}"
    if in_call:
        line = line[: _MAX_ACTIVITY_CHARS - len(_IN_CALL_SUFFIX)] + _IN_CALL_SUFFIX
    return {"current_activity": line[:_MAX_ACTIVITY_CHARS]}


def _gcal_line(row: dict[str, Any], tz: ZoneInfo) -> str:
    """One compact line for a gcal row's ``payload`` (the raw ``CalendarEvent.to_payload``
    shape: ``event_id``/``title``/``start``/``end``/``all_day``)."""
    payload = row["payload"]
    title = payload["title"]
    if payload["all_day"]:
        return f"All day: {title}"
    start_local = datetime.fromisoformat(payload["start"]).astimezone(tz)
    return f"{start_local.strftime('%H:%M')} {title}"


def _gmail_line(row: dict[str, Any], tz: ZoneInfo) -> str:
    """One compact line for a gmail row's ``payload`` (the DEC-45 projection shape:
    ``message_id``/``subject``/``sender``/``received_at``/``priority_band``) — reads ONLY these
    five named fields (DEC-26 held by construction, AC4)."""
    payload = row["payload"]
    received_local = datetime.fromisoformat(payload["received_at"]).astimezone(tz)
    return (
        f"{payload['subject']} from {payload['sender']} "
        f"({received_local.strftime('%Y-%m-%d %H:%M')})"
    )


def _render(lines: Iterable[str], *, max_items: int, max_chars: int) -> str:
    """Join ``lines`` one-per-line, stopping at whichever of ``max_items``/``max_chars`` is hit
    first — deterministic (same input always yields the same bytes) and never unbounded."""
    kept: list[str] = []
    total = 0
    for line in lines:
        if len(kept) >= max_items:
            break
        added = len(line) + (1 if kept else 0)  # +1 for the joining newline, from the 2nd line on
        if total + added > max_chars:
            break
        kept.append(line)
        total += added
    return "\n".join(kept)


__all__ = [
    "ActivitySnapshot",
    "UserFactsContextStore",
    "VoiceContextStore",
    "build_current_activity_context",
    "build_user_facts_context",
    "build_voice_context",
]
