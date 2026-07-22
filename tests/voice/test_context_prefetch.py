"""TK-290 acceptance criteria — build_voice_context (DEC-64 gap B).

  AC1 fake store PLUS a pg-gated WOMBAT_TEST_PG_DSN case (real ExternalItemStore) seeded with
      today-window gcal rows + recent gmail rows: the payload carries both context keys, rendered
      deterministically (same input = same bytes) within caps.
  AC2 out-of-window gcal rows and above-cap counts: excluded/truncated, never an unbounded prompt.
  AC3 empty store / None store / raising store: NO context keys, exactly one WARNING on the
      failure case, turn proceeds.
  AC4 structural: no body/body_text reference anywhere in the module.

pg-gated tests use ONLY a throwaway WOMBAT_TEST_PG_DSN Postgres — never the live wombat DB (same
convention as tests/unit/test_external_store.py).
"""

from __future__ import annotations

import inspect
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest

from wombat.external_store import ExternalItem, ExternalItemStore, ensure_schema
from wombat.voice import context_prefetch
from wombat.voice.context_prefetch import (
    _GCAL_MAX_CHARS,
    _GCAL_MAX_ITEMS,
    _GMAIL_MAX_CHARS,
    build_voice_context,
)

_TZ = ZoneInfo("America/Chicago")

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping context_prefetch DB tests that require a real "
        "throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres"
    ),
)


def _clock(instant: datetime) -> Callable[[], datetime]:
    return lambda: instant


class _FakeStore:
    """A minimal stand-in satisfying ``VoiceContextStore`` — records the args each call received
    so tests can assert the exact window/limit passed."""

    def __init__(
        self,
        *,
        window_rows: list[dict[str, Any]] | None = None,
        recent_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.window_rows = window_rows or []
        self.recent_rows = recent_rows or []
        self.window_calls: list[tuple[str, datetime, datetime]] = []
        self.recent_calls: list[tuple[str, int]] = []

    def get_window(self, source: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        self.window_calls.append((source, start, end))
        return self.window_rows

    def get_recent(self, source: str, limit: int) -> list[dict[str, Any]]:
        self.recent_calls.append((source, limit))
        return self.recent_rows


class _RaisingStore:
    def get_window(self, source: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        raise RuntimeError("boom")

    def get_recent(self, source: str, limit: int) -> list[dict[str, Any]]:
        raise RuntimeError("boom")


def _gcal_row(item_key: str, start: datetime, end: datetime, title: str) -> dict[str, Any]:
    return {
        "item_key": item_key,
        "payload": {
            "event_id": item_key,
            "title": title,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "all_day": False,
        },
        "occurs_at": start,
        "fetched_at": start,
        "first_seen_at": start,
    }


def _gmail_row(item_key: str, received_at: datetime, subject: str, sender: str) -> dict[str, Any]:
    return {
        "item_key": item_key,
        "payload": {
            "message_id": item_key,
            "subject": subject,
            "sender": sender,
            "received_at": received_at.isoformat(),
            "priority_band": "normal",
        },
        "occurs_at": received_at,
        "fetched_at": received_at,
        "first_seen_at": received_at,
    }


_NOON = datetime(2026, 7, 21, 12, 0, tzinfo=_TZ)


# --------------------------------------------------------------------------------------- AC1 fake


def test_ac1_fake_store_returns_both_keys_rendered_deterministically() -> None:
    store = _FakeStore(
        window_rows=[
            _gcal_row(
                "e1",
                datetime(2026, 7, 21, 9, 0, tzinfo=_TZ),
                datetime(2026, 7, 21, 9, 30, tzinfo=_TZ),
                "Standup",
            ),
            _gcal_row(
                "e2",
                datetime(2026, 7, 21, 14, 0, tzinfo=_TZ),
                datetime(2026, 7, 21, 15, 0, tzinfo=_TZ),
                "1:1",
            ),
        ],
        recent_rows=[
            _gmail_row(
                "m1", datetime(2026, 7, 20, 8, 0, tzinfo=_TZ), "Invoice", "billing@vendor.com"
            ),
            _gmail_row(
                "m2", datetime(2026, 7, 21, 7, 0, tzinfo=_TZ), "Re: sync", "alice@example.com"
            ),
        ],
    )

    result1 = build_voice_context(store, tz=_TZ, clock=_clock(_NOON))
    result2 = build_voice_context(store, tz=_TZ, clock=_clock(_NOON))

    assert result1 == result2  # same input -> same bytes
    assert set(result1.keys()) == {"context_calendar_today", "context_recent_email"}
    assert "Standup" in result1["context_calendar_today"]
    assert "1:1" in result1["context_calendar_today"]
    assert "09:00" in result1["context_calendar_today"]
    assert "Invoice" in result1["context_recent_email"]
    assert "Re: sync" in result1["context_recent_email"]
    # gmail rendered in the store's returned order (already ascending by occurs_at).
    assert result1["context_recent_email"].index("Invoice") < result1["context_recent_email"].index(
        "Re: sync"
    )

    # The window passed is the tz-local civil day, computed via wombat_today at call time.
    source, start, end = store.window_calls[0]
    assert source == "gcal"
    assert start == datetime(2026, 7, 21, 0, 0, 0, 0, tzinfo=_TZ)
    assert end == datetime(2026, 7, 21, 23, 59, 59, 999999, tzinfo=_TZ)
    assert store.recent_calls[0] == ("gmail", 5)


def test_zero_rows_from_a_source_contributes_no_key() -> None:
    store = _FakeStore(window_rows=[], recent_rows=[])
    result = build_voice_context(store, tz=_TZ, clock=_clock(_NOON))
    assert result == {}


def test_only_gcal_rows_present_yields_only_that_key() -> None:
    store = _FakeStore(
        window_rows=[_gcal_row("e1", _NOON, _NOON, "Only event")],
        recent_rows=[],
    )
    result = build_voice_context(store, tz=_TZ, clock=_clock(_NOON))
    assert set(result.keys()) == {"context_calendar_today"}


# --------------------------------------------------------------------------------------- AC1 pg


@pytest.fixture
def fresh_table() -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS wombat_external_items CASCADE")
        conn.commit()


@_requires_pg
def test_ac1_pg_seeded_today_gcal_and_recent_gmail_rows(fresh_table: None) -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        conn.commit()

    store = ExternalItemStore(_DSN)
    try:
        today_start = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)
        store.upsert_many(
            "gcal",
            [
                ExternalItem(
                    item_key="evt-1",
                    payload={
                        "event_id": "evt-1",
                        "title": "Design review",
                        "start": today_start.isoformat(),
                        "end": (today_start).isoformat(),
                        "all_day": False,
                    },
                    occurs_at=today_start,
                )
            ],
            fetched_at=today_start,
        )
        store.upsert_many(
            "gmail",
            [
                ExternalItem(
                    item_key="msg-1",
                    payload={
                        "message_id": "msg-1",
                        "subject": "Weekly digest",
                        "sender": "digest@example.com",
                        "received_at": today_start.isoformat(),
                        "priority_band": "normal",
                    },
                    occurs_at=today_start,
                )
            ],
            fetched_at=today_start,
        )

        result = build_voice_context(
            store, tz=ZoneInfo("UTC"), clock=_clock(datetime(2026, 7, 21, 20, 0, tzinfo=UTC))
        )
        assert "Design review" in result["context_calendar_today"]
        assert "Weekly digest" in result["context_recent_email"]
    finally:
        store.close()


# --------------------------------------------------------------------------------------- AC2


def test_ac2_above_cap_gcal_item_count_is_truncated_to_ten() -> None:
    rows = [
        _gcal_row(
            f"e{i}",
            datetime(2026, 7, 21, 8, i, tzinfo=_TZ),
            datetime(2026, 7, 21, 9, i, tzinfo=_TZ),
            f"Event {i}",
        )
        for i in range(15)
    ]
    store = _FakeStore(window_rows=rows, recent_rows=[])
    result = build_voice_context(store, tz=_TZ, clock=_clock(_NOON))
    lines = result["context_calendar_today"].splitlines()
    assert len(lines) == _GCAL_MAX_ITEMS
    assert len(result["context_calendar_today"]) <= _GCAL_MAX_CHARS


def test_ac2_gcal_char_cap_truncates_even_under_the_item_cap() -> None:
    rows = [
        _gcal_row(
            f"e{i}",
            datetime(2026, 7, 21, 8, i, tzinfo=_TZ),
            datetime(2026, 7, 21, 9, i, tzinfo=_TZ),
            "x" * 200,
        )
        for i in range(8)
    ]
    store = _FakeStore(window_rows=rows, recent_rows=[])
    result = build_voice_context(store, tz=_TZ, clock=_clock(_NOON))
    lines = result["context_calendar_today"].splitlines()
    assert len(lines) < 8
    assert len(result["context_calendar_today"]) <= _GCAL_MAX_CHARS


def test_ac2_gmail_char_cap_is_never_exceeded() -> None:
    rows = [
        _gmail_row(
            f"m{i}", datetime(2026, 7, 21, 8, i, tzinfo=_TZ), "x" * 150, "sender@example.com"
        )
        for i in range(5)
    ]
    store = _FakeStore(window_rows=[], recent_rows=rows)
    result = build_voice_context(store, tz=_TZ, clock=_clock(_NOON))
    assert len(result["context_recent_email"]) <= _GMAIL_MAX_CHARS


def test_ac2_out_of_window_rows_are_excluded_by_the_stores_own_sql_bounds() -> None:
    """The renderer never re-filters by window -- it trusts whatever get_window already scoped.
    This test proves the renderer keeps every row the store hands back (the exclusion is the
    store's job, verified separately in test_external_store.py's AC2)."""
    store = _FakeStore(window_rows=[_gcal_row("e1", _NOON, _NOON, "In window")], recent_rows=[])
    result = build_voice_context(store, tz=_TZ, clock=_clock(_NOON))
    assert result["context_calendar_today"].count("\n") == 0  # exactly one line, nothing extra


# --------------------------------------------------------------------------------------- AC3


def test_ac3_none_store_yields_no_keys() -> None:
    result = build_voice_context(None, tz=_TZ, clock=_clock(_NOON))
    assert result == {}


def test_ac3_empty_store_yields_no_keys() -> None:
    store = _FakeStore(window_rows=[], recent_rows=[])
    result = build_voice_context(store, tz=_TZ, clock=_clock(_NOON))
    assert result == {}


def test_ac3_raising_store_yields_no_keys_and_exactly_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="wombat.voice.context_prefetch")
    result = build_voice_context(_RaisingStore(), tz=_TZ, clock=_clock(_NOON))
    assert result == {}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


# --------------------------------------------------------------------------------------- AC4


def test_ac4_no_body_or_body_text_reference_anywhere_in_the_module() -> None:
    source = inspect.getsource(context_prefetch)
    assert "body_text" not in source
    assert '"body"' not in source
    assert "'body'" not in source
    assert ".body" not in source
