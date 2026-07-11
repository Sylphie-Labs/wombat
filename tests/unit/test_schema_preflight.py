"""TK-203 — schema pre-flight acceptance criteria (CR3-1, Q-104).

ALL DB tests in this module require a REAL Postgres and are gated on the ``WOMBAT_TEST_PG_DSN``
env var (the SAME convention as ``tests/gate/test_pending_journal_pg.py`` / ``tests/integration/
test_serve_boot.py``): absent it, tests are skipped LOUDLY. Spin up a throwaway Postgres locally:

    docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres

  AC1 a brand-new empty database: ``ensure_all_schemas(dsn)`` creates all six packaged tables,
      a second call is idempotent, AND the 2026-07-09 incident itself is fixed —
      ``assemble_runtime(replay_pending=True)`` against a fresh throwaway database no longer
      raises ``UndefinedTable`` at the eager pending-journal replay.
  AC2 an unreachable dsn: ``assemble_runtime(replay_pending=False)`` still succeeds
      connection-free (no pg gate needed for this one — the whole point is no connection is
      attempted).
  AC3 tables that already exist and hold rows: a second ``ensure_all_schemas(dsn)`` call is
      create-if-absent only — existing rows survive.
"""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo

import psycopg
import pytest

from wombat import bootstrap
from wombat.config import WombatConfig
from wombat.params import load_operating_params
from wombat.queue import EnqueueResult, QueueItem, WombatQueue
from wombat.schema_preflight import ensure_all_schemas

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping schema pre-flight DB tests that require a "
        "real throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres"
    ),
)

# The six packaged tables this pre-flight must create (Q-104-verified module homes; TK-240 added
# the sixth, wombat_settings).
_PACKAGED_TABLES = (
    "wombat_queue",
    "daily_ledger",
    "pending_journal",
    "wombat_behavior_events",
    "action_trail_projection",
    "wombat_settings",
)


def _config() -> WombatConfig:
    # An unreachable base_url (mirrors test_serve_boot.py's own documented pattern): this module
    # proves the runtime WIRING (schema pre-flight), not a real DeepSeek response.
    return WombatConfig(deepseek_api_key="dummy-not-real-key", deepseek_base_url="https://x.test")


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    bootstrap.reset_engine()


@pytest.fixture
def fresh_database() -> None:
    """Drop every packaged table, simulating a brand-new empty Postgres (AC1)."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        with conn.cursor() as cur:
            for table in _PACKAGED_TABLES:
                cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        conn.commit()


def _existing_tables(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
        return {row[0] for row in cur.fetchall()}


# --------------------------------------------------------------------------------------- AC1


@_requires_pg
def test_ac1_ensure_all_schemas_creates_all_six_packaged_tables(fresh_database: None) -> None:
    assert _DSN is not None
    ensure_all_schemas(_DSN)

    existing = _existing_tables(_DSN)
    for table in _PACKAGED_TABLES:
        assert table in existing, f"expected table {table!r} to exist after ensure_all_schemas"


@_requires_pg
def test_ac1_ensure_all_schemas_is_idempotent(fresh_database: None) -> None:
    """A second call over the same (now-current) database raises nothing."""
    assert _DSN is not None
    ensure_all_schemas(_DSN)
    ensure_all_schemas(_DSN)  # must not raise

    existing = _existing_tables(_DSN)
    for table in _PACKAGED_TABLES:
        assert table in existing


@_requires_pg
def test_ac1_incident_repro_assemble_runtime_survives_a_fresh_database(
    fresh_database: None,
) -> None:
    """The 2026-07-09 live incident: assemble_runtime(replay_pending=True) against a brand-new,
    empty Postgres must no longer raise UndefinedTable at the eager pending-journal replay."""
    assert _DSN is not None
    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(config=_config(), dsn=_DSN, params=op, tz=ZoneInfo("UTC"))
    try:
        assert bundle.pathways.get(bundle.drain_pathway_id) is not None
    finally:
        bundle.queue.close()
        bundle.daily_ledger.close()
        bundle.pending_journal.close()
        bundle.behavior_event_log.close()


# --------------------------------------------------------------------------------------- AC2


def test_ac2_replay_pending_false_stays_connection_free_over_an_unreachable_dsn() -> None:
    """assemble_runtime(replay_pending=False) over an unreachable dsn still succeeds — the
    pre-flight is gated on replay_pending and must not add a new reachability requirement."""
    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(
        config=_config(),
        dsn="postgresql://nonexistent-host-should-never-be-dialed:1/db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )
    assert bundle.pathways.get(bundle.drain_pathway_id) is not None


# --------------------------------------------------------------------------------------- AC3


@_requires_pg
def test_ac3_ensure_all_schemas_preserves_existing_rows(fresh_database: None) -> None:
    """Given tables that already exist and hold a row, a second ensure_all_schemas call is
    create-if-absent only — the row survives."""
    assert _DSN is not None
    ensure_all_schemas(_DSN)

    queue = WombatQueue(_DSN, max_size=10)
    try:
        result = queue.enqueue(
            QueueItem(idempotency_key="tk203-survivor", payload={"subject": "survives"})
        )
        assert result is EnqueueResult.QUEUED
    finally:
        queue.close()

    ensure_all_schemas(_DSN)  # must not drop/recreate the table, must not error

    with psycopg.connect(_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM wombat_queue WHERE idempotency_key = %s", ("tk203-survivor",)
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 1
