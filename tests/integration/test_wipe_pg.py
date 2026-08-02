"""tests/integration/test_wipe_pg.py — pg-gated archive-then-truncate proof for
``wombat.wipe.archive_and_wipe`` (TK-334, DEC-75/DEC-76, DEC-77 r5).

ALL tests in this module require a real Postgres and are gated on ``WOMBAT_TEST_PG_DSN`` (the
SAME convention as ``tests/integration/test_serve_boot.py`` / ``tests/behavior/stages/
test_pattern_detector.py``): absent it, the whole module is skipped LOUDLY at collection time.

    docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres

DEC-77 r5 (test-DSN safety, non-negotiable): this is the ONE suite on the board that truncates an
entire public schema, so it additionally HARD-FAILS at collection — not skips, not runs — if the
resolved ``WOMBAT_TEST_PG_DSN`` is identical to the live DSN. The live DSN is resolved with
``wombat.config._resolve_pg_dsn`` — the SAME env-else-cwd-relative-``.env`` resolution
``load_config()`` uses (TK-334 repair) — not a bare ``os.environ`` read, because the operator's
real DSN normally lives only in the repo-root ``.env``, never in an exported env var. A
copy-pasted production DSN must be a loud failure here, never a wipe.

  AC1 archive fidelity — every public base table seeded, ``archive_and_wipe`` run, one JSON file
      per table plus ``manifest.json`` land under ``archive_dir``, every seeded row appears
      verbatim, manifest ``row_count``/``sha256`` match reality.
  AC2 emptied except the keep-list — every table empty afterward except ``wombat_settings``
      (row-for-row unchanged); identities restarted (a fresh ``wombat_observations`` insert
      returns id 1).
  AC3 schema survives — ``ensure_all_schemas`` re-run afterward raises nothing and changes
      nothing; ``information_schema`` reports the identical table AND index set.
  AC5 drift probe — an unmigrated ``wombat_drift_probe`` table is BOTH archived and emptied,
      proving enumeration is runtime introspection, not a hand-kept list.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from wombat.config import _resolve_pg_dsn
from wombat.schema_preflight import ensure_all_schemas
from wombat.wipe import archive_and_wipe

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")
# TK-334 repair: the operator's real DSN normally lives only in the repo-root .env, not an
# exported WOMBAT_PG_DSN env var — resolve it the SAME way load_config() does (env, else
# cwd-relative .env) so a copy-pasted live DSN is actually caught, not missed.
_LIVE_DSN = _resolve_pg_dsn()

if _DSN and _LIVE_DSN and _DSN == _LIVE_DSN:
    raise RuntimeError(
        "WOMBAT_TEST_PG_DSN is identical to the live WOMBAT_PG_DSN (env or repo-root .env) — "
        "refusing to collect the TK-334 archive-then-truncate suite (DEC-77 r5). This suite "
        "TRUNCATEs an entire public schema; a copy-pasted production DSN here must be a loud "
        "failure, never a wipe. Point WOMBAT_TEST_PG_DSN at a throwaway Postgres instead."
    )

if not _DSN:
    pytest.skip(
        "WOMBAT_TEST_PG_DSN is not set — skipping the TK-334 archive-then-truncate pg-gated "
        "suite, which requires a real throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres",
        allow_module_level=True,
    )


# ================================================================================================
# Introspection + seeding helpers
# ================================================================================================


def _public_base_tables(conn: psycopg.Connection[Any]) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
        return {row[0] for row in cur.fetchall()}


def _public_indexes(conn: psycopg.Connection[Any]) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
        return {row[0] for row in cur.fetchall()}


def _count_rows(conn: psycopg.Connection[Any], table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f'SELECT COUNT(*) FROM "{table}"')
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def _seed_all_twelve_tables(conn: psycopg.Connection[Any]) -> None:
    """One row into every packaged table (AC1's 'seed at least one row into EVERY public base
    table')."""
    now = datetime.now(UTC)
    today = date.today()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO wombat_queue (idempotency_key, payload) VALUES (%s, %s)",
            ("tk334-queue-1", "{}"),
        )
        cur.execute(
            "INSERT INTO daily_ledger (ledger_name, wombat_date, value) VALUES (%s, %s, %s)",
            ("tk334", today, 1),
        )
        cur.execute(
            "INSERT INTO action_trail_projection "
            "(action_id, action_type, human_summary, target, proposed_at, status) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            ("tk334-action-1", "draft_email", "a test proposal", "someone@example.com", now,
             "pending"),
        )
        cur.execute(
            "INSERT INTO pending_journal (record_type, item_id) VALUES (%s, %s)",
            ("clear", None),
        )
        cur.execute(
            "INSERT INTO wombat_behavior_events "
            "(idempotency_key, event_type, source_id, timestamp_utc, outcome_label) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("tk334-behavior-1", "draft_reply", "gmail", now, "load_bearing"),
        )
        cur.execute(
            "INSERT INTO wombat_settings (key, value) VALUES (%s, %s)",
            ("tk334_test_setting", Jsonb({"x": 1})),
        )
        cur.execute(
            "INSERT INTO wombat_external_items (source, item_key, payload, fetched_at) "
            "VALUES (%s, %s, %s, %s)",
            ("gmail", "tk334-item-1", Jsonb({"subject": "hi"}), now),
        )
        cur.execute(
            "INSERT INTO wombat_scratchpad (scope_key, entry_key, value) VALUES (%s, %s, %s)",
            ("tk334-scope", "tk334-entry", Jsonb({"y": 2})),
        )
        cur.execute(
            "INSERT INTO wombat_seen_events (idempotency_key, payload_fingerprint) "
            "VALUES (%s, %s)",
            ("tk334-seen-1", "deadbeef"),
        )
        cur.execute(
            "INSERT INTO wombat_user_facts (fact_key, fact, source) VALUES (%s, %s, %s)",
            ("tk334-fact-1", "likes archive fidelity tests", "told"),
        )
        cur.execute(
            "INSERT INTO wombat_chat_turns (text, voice, captured_at) VALUES (%s, %s, %s)",
            ("hello wombat", False, now),
        )
        cur.execute(
            "INSERT INTO wombat_observations "
            "(channel, kind, started_at, ended_at, payload, day_key) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            ("screen", "app_segment", now, now, Jsonb({"app": "vscode"}), today),
        )
    conn.commit()


def _create_and_seed_drift_probe(conn: psycopg.Connection[Any]) -> None:
    """A table this module never migrates — the AC5 proof that enumeration is runtime
    introspection, not a hand-kept list."""
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS wombat_drift_probe (id SERIAL PRIMARY KEY, "
                    "note TEXT NOT NULL)")
        cur.execute(
            "INSERT INTO wombat_drift_probe (note) VALUES (%s)", ("tk334-drift-probe",)
        )
    conn.commit()


def _drop_drift_probe(dsn: str) -> None:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS wombat_drift_probe")
        conn.commit()


@pytest.fixture
def clean_db() -> str:
    """``ensure_all_schemas`` at HEAD, then every public base table truncated so each test
    starts from a known-empty state."""
    assert _DSN is not None
    ensure_all_schemas(_DSN)
    with psycopg.connect(_DSN) as conn:
        tables = sorted(_public_base_tables(conn))
        identifiers = ", ".join(f'"{t}"' for t in tables)
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE {identifiers} RESTART IDENTITY CASCADE")
        conn.commit()
    return _DSN


# ================================================================================================
# AC1 — archive fidelity
# ================================================================================================


def test_ac1_archive_fidelity(clean_db: str, tmp_path: Path) -> None:
    dsn = clean_db
    with psycopg.connect(dsn) as conn:
        _seed_all_twelve_tables(conn)
        tables = sorted(_public_base_tables(conn))
        pre_counts = {t: _count_rows(conn, t) for t in tables}

    archive_dir = tmp_path / "archive"
    report = archive_and_wipe(dsn, archive_dir)

    assert sorted(report.tables) == tables
    assert (archive_dir / "manifest.json").exists()
    manifest = json.loads((archive_dir / "manifest.json").read_text(encoding="utf-8"))

    for table in tables:
        file_path = archive_dir / f"{table}.json"
        assert file_path.exists(), f"missing archive file for {table}"
        on_disk_bytes = file_path.read_bytes()
        assert manifest["tables"][table]["sha256"] == hashlib.sha256(on_disk_bytes).hexdigest()
        assert manifest["tables"][table]["row_count"] == pre_counts[table]
        rows = json.loads(on_disk_bytes)
        assert len(rows) == pre_counts[table]

    queue_rows = json.loads((archive_dir / "wombat_queue.json").read_text(encoding="utf-8"))
    assert any(r["idempotency_key"] == "tk334-queue-1" for r in queue_rows)

    fact_rows = json.loads((archive_dir / "wombat_user_facts.json").read_text(encoding="utf-8"))
    assert any(r["fact_key"] == "tk334-fact-1" for r in fact_rows)

    # JSON-native round trip: TIMESTAMPTZ lands as an ISO string, JSONB payload as a plain dict.
    obs_rows = json.loads(
        (archive_dir / "wombat_observations.json").read_text(encoding="utf-8")
    )
    obs_row = next(r for r in obs_rows if r["payload"] == {"app": "vscode"})
    assert isinstance(obs_row["started_at"], str)
    assert datetime.fromisoformat(obs_row["started_at"]) is not None


# ================================================================================================
# AC2 — emptied except the keep-list; identities restarted
# ================================================================================================


def test_ac2_emptied_except_keep_list_and_identities_restart(
    clean_db: str, tmp_path: Path
) -> None:
    dsn = clean_db
    with psycopg.connect(dsn) as conn:
        _seed_all_twelve_tables(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM wombat_settings ORDER BY key")
            settings_before = cur.fetchall()

    archive_and_wipe(dsn, tmp_path / "archive")

    with psycopg.connect(dsn) as conn:
        tables = sorted(_public_base_tables(conn))
        for table in tables:
            count = _count_rows(conn, table)
            if table == "wombat_settings":
                with conn.cursor() as cur:
                    cur.execute("SELECT key, value FROM wombat_settings ORDER BY key")
                    settings_after = cur.fetchall()
                assert settings_after == settings_before
            else:
                assert count == 0, f"{table} still holds rows after wipe"

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO wombat_observations "
                "(channel, kind, started_at, ended_at, payload, day_key) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    "screen",
                    "app_segment",
                    datetime.now(UTC),
                    datetime.now(UTC),
                    Jsonb({}),
                    date.today(),
                ),
            )
            new_row = cur.fetchone()
            assert new_row is not None
            assert new_row[0] == 1, "identity was not restarted by RESTART IDENTITY"
        conn.commit()


# ================================================================================================
# AC3 — schema survives; ensure_all_schemas stays a no-op
# ================================================================================================


def test_ac3_schema_survives_and_ensure_all_schemas_stays_a_no_op(
    clean_db: str, tmp_path: Path
) -> None:
    dsn = clean_db
    with psycopg.connect(dsn) as conn:
        _seed_all_twelve_tables(conn)
        tables_before = _public_base_tables(conn)
        indexes_before = _public_indexes(conn)

    archive_and_wipe(dsn, tmp_path / "archive")

    ensure_all_schemas(dsn)  # must raise nothing

    with psycopg.connect(dsn) as conn:
        tables_after = _public_base_tables(conn)
        indexes_after = _public_indexes(conn)

    assert tables_after == tables_before
    assert indexes_after == indexes_before


# ================================================================================================
# AC5 — the drift probe: enumeration is runtime introspection, not a hand-kept list
# ================================================================================================


def test_ac5_unmigrated_table_is_archived_and_emptied(clean_db: str, tmp_path: Path) -> None:
    dsn = clean_db
    try:
        with psycopg.connect(dsn) as conn:
            _seed_all_twelve_tables(conn)
            _create_and_seed_drift_probe(conn)

        archive_dir = tmp_path / "archive"
        report = archive_and_wipe(dsn, archive_dir)

        assert "wombat_drift_probe" in report.tables
        probe_file = archive_dir / "wombat_drift_probe.json"
        assert probe_file.exists()
        probe_rows = json.loads(probe_file.read_text(encoding="utf-8"))
        assert any(r["note"] == "tk334-drift-probe" for r in probe_rows)

        with psycopg.connect(dsn) as conn:
            assert _count_rows(conn, "wombat_drift_probe") == 0
    finally:
        _drop_drift_probe(dsn)
