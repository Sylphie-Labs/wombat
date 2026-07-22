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
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import datetime, time
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from wombat.domain.daily_ledger import wombat_today

logger = logging.getLogger(__name__)

# Pinned caps (DEC-63 no-knob precedent — NOT operator-tunable): the hard spend/prompt-size guard
# for this grounding bundle, independent of compose.py's own daily token ceiling.
_GCAL_MAX_ITEMS = 10
_GCAL_MAX_CHARS = 800
_GMAIL_LIMIT = 5
_GMAIL_MAX_CHARS = 400


class VoiceContextStore(Protocol):
    """The structural shape ``build_voice_context`` needs from a store — matches
    ``external_store.ExternalItemStore``'s ``get_window``/``get_recent`` exactly (mirrors the
    ``TokenStore``/``VoiceKeyStore`` Protocol convention elsewhere in this codebase). A test fake
    only needs to satisfy this shape, never import ``ExternalItemStore`` itself."""

    def get_window(self, source: str, start: datetime, end: datetime) -> list[dict[str, Any]]: ...

    def get_recent(self, source: str, limit: int) -> list[dict[str, Any]]: ...


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


__all__ = ["VoiceContextStore", "build_voice_context"]
