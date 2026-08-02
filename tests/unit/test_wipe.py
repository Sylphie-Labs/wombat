"""tests/unit/test_wipe.py — fakes-only proof of wipe.py's fail-closed archive-then-truncate
ordering (TK-334, DEC-75/DEC-76, DEC-77 r4/r5).

Runs WITHOUT Postgres: a hand-rolled ``_FakeConnection``/``_FakeCursor`` pair, injected through
the ``connect`` seam ``archive_and_wipe`` exposes (DEC-77 r4 — keyword-only, defaults to
``psycopg.connect``). ``WOMBAT_TEST_PG_DSN`` is blank on this build machine (the pg-gated
``tests/integration/test_wipe_pg.py`` skips here), so this module carries the real coverage
weight for AC4 (fail-closed, zero rows touched) and AC6 (this module runs regardless of the pg
gate) — plus the schema-driven-enumeration and keep-list properties, proven against a fake
``information_schema.tables`` read rather than a real one.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

import wombat.wipe as wipe_module
from wombat.wipe import WipeAborted, WipeReport, archive_and_wipe

# ================================================================================================
# Fakes — a minimal psycopg-shaped connection/cursor, enough to drive wipe.py's SQL surface
# (information_schema enumeration, SELECT * per table, TRUNCATE TABLE ... RESTART IDENTITY
# CASCADE) with zero real I/O beyond the filesystem archive writes.
# ================================================================================================


class _FakeCursor:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn
        self._result: list[tuple[Any, ...]] = []
        self.description: list[tuple[str]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self._conn.executed.append(sql)
        upper = " ".join(sql.split()).upper()
        if "INFORMATION_SCHEMA.TABLES" in upper:
            names = sorted(self._conn.tables)
            self._result = [(name,) for name in names]
            self.description = [("table_name",)]
        elif upper.startswith("SELECT * FROM"):
            table = sql.split('"')[1]
            rows = self._conn.tables[table]
            columns = self._conn.columns[table]
            self._result = [tuple(row[c] for c in columns) for row in rows]
            self.description = [(c,) for c in columns]
        elif upper.startswith("TRUNCATE TABLE"):
            for table in _quoted_identifiers(sql):
                self._conn.tables[table] = []
            self._result = []
        else:  # pragma: no cover - a design bug, not a runtime path
            raise AssertionError(f"unexpected SQL in fake cursor: {sql!r}")

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._result)


def _quoted_identifiers(sql: str) -> list[str]:
    parts = sql.split('"')
    return [parts[i] for i in range(1, len(parts), 2)]


class _FakeConnection:
    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in tables.items()
        }
        self.columns: dict[str, list[str]] = {
            name: list(rows[0].keys()) for name, rows in tables.items()
        }
        self.executed: list[str] = []
        self.committed = False
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        self.closed = True


def _seeded_tables() -> dict[str, list[dict[str, Any]]]:
    """One seeded row per table across a representative slice of the twelve real tables plus an
    unmigrated extra table (the AC5 drift-probe proof, done here fakes-only since AC5 itself is
    pg-gated) — nothing in wipe.py special-cases any of these names."""
    return {
        "wombat_queue": [
            {"id": 1, "idempotency_key": "k1", "payload": "{}", "status": "ready"}
        ],
        "wombat_settings": [{"key": "wombat_persona_pins", "value": {"pins": ["a"]}}],
        "wombat_observations": [
            {
                "id": 1,
                "channel": "screen",
                "payload": {"app": "vscode"},
                "started_at": datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC),
            }
        ],
        "wombat_drift_probe": [{"id": 1, "note": "not in any migration"}],
    }


def _make_connect(conn: _FakeConnection) -> Callable[[str], _FakeConnection]:
    def _connect(dsn: str) -> _FakeConnection:
        return conn

    return _connect


# ================================================================================================
# DEC-77 r4 — the connect seam itself: keyword-only, defaults to psycopg.connect.
# ================================================================================================


def test_connect_seam_is_keyword_only_and_defaults_to_psycopg_connect() -> None:
    sig = inspect.signature(archive_and_wipe)
    assert sig.parameters["connect"].kind == inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["connect"].default is psycopg.connect
    # Real callers pass exactly two positional args.
    assert list(sig.parameters)[:2] == ["dsn", "archive_dir"]
    assert sig.parameters["dsn"].kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert sig.parameters["archive_dir"].kind == inspect.Parameter.POSITIONAL_OR_KEYWORD


# ================================================================================================
# AC4 — fail-closed: an injected archive write/fsync failure raises WipeAborted, zero
# TRUNCATE/DELETE/DROP statements ever executed, every table still holds its rows.
# ================================================================================================


def test_archive_write_failure_aborts_before_any_destructive_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tables = _seeded_tables()
    conn = _FakeConnection(tables)

    def _boom(path: Path, data: Any) -> str:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(wipe_module, "_write_json_file", _boom)

    with pytest.raises(WipeAborted):
        archive_and_wipe("fake-dsn", tmp_path, connect=_make_connect(conn))

    assert not any(
        keyword in stmt.upper()
        for stmt in conn.executed
        for keyword in ("TRUNCATE", "DELETE", "DROP")
    )
    assert not conn.committed
    for table, rows in tables.items():
        assert conn.tables[table] == rows


def test_archive_failure_on_the_manifest_file_also_aborts_before_destroy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure can land on manifest.json itself, AFTER every per-table file already
    succeeded — the abort must still happen before any TRUNCATE."""
    tables = _seeded_tables()
    conn = _FakeConnection(tables)
    real_write = wipe_module._write_json_file
    calls = {"n": 0}

    def _fail_on_manifest(path: Path, data: Any) -> str:
        calls["n"] += 1
        if path.name == "manifest.json":
            raise OSError("simulated manifest write failure")
        return real_write(path, data)

    monkeypatch.setattr(wipe_module, "_write_json_file", _fail_on_manifest)

    with pytest.raises(WipeAborted):
        archive_and_wipe("fake-dsn", tmp_path, connect=_make_connect(conn))

    assert calls["n"] == len(tables) + 1  # every table file attempted, then the manifest
    assert not any("TRUNCATE" in stmt.upper() for stmt in conn.executed)
    for table, rows in tables.items():
        assert conn.tables[table] == rows


# ================================================================================================
# Happy path — schema-driven enumeration, archive fidelity, keep-list asymmetry, one TRUNCATE.
# ================================================================================================


def test_happy_path_archives_every_table_then_truncates_all_but_the_keep_list(
    tmp_path: Path,
) -> None:
    tables = _seeded_tables()
    conn = _FakeConnection(tables)

    report = archive_and_wipe("fake-dsn", tmp_path, connect=_make_connect(conn))

    # Enumeration is schema-driven: the unmigrated wombat_drift_probe table is swept too.
    assert set(report.tables) == set(tables)
    assert "wombat_drift_probe" in report.tables

    # Exactly one TRUNCATE statement, naming every non-keep-listed table, RESTART IDENTITY
    # CASCADE, and nothing else destructive.
    truncate_statements = [s for s in conn.executed if s.upper().startswith("TRUNCATE")]
    assert len(truncate_statements) == 1
    stmt = truncate_statements[0]
    assert "RESTART IDENTITY CASCADE" in stmt
    for table in tables:
        if table == "wombat_settings":
            assert f'"{table}"' not in stmt
        else:
            assert f'"{table}"' in stmt
    assert conn.committed

    # Keep-list: wombat_settings archived but NOT truncated; every other table emptied.
    assert conn.tables["wombat_settings"] == tables["wombat_settings"]
    for table in tables:
        if table != "wombat_settings":
            assert conn.tables[table] == []

    assert report.kept == ["wombat_settings"]
    assert report.truncated == sorted(t for t in tables if t != "wombat_settings")
    assert report.row_counts == {t: len(rows) for t, rows in tables.items()}

    # Archive fidelity: one JSON file per table plus manifest.json, verbatim rows, matching
    # manifest row_count/sha256.
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["keep_list"] == ["wombat_settings"]
    for table, rows in tables.items():
        file_path = tmp_path / f"{table}.json"
        on_disk = json.loads(file_path.read_text(encoding="utf-8"))
        assert len(on_disk) == len(rows)
        assert manifest["tables"][table]["row_count"] == len(rows)
        assert (
            manifest["tables"][table]["sha256"]
            == hashlib.sha256(file_path.read_bytes()).hexdigest()
        )

    # A datetime value round-trips as its ISO string (Q-49 JSON-native convention).
    obs_on_disk = json.loads((tmp_path / "wombat_observations.json").read_text(encoding="utf-8"))
    assert obs_on_disk[0]["started_at"] == "2026-08-01T12:00:00+00:00"


def test_connection_is_always_closed_even_on_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _FakeConnection(_seeded_tables())

    def _boom(path: Path, data: Any) -> str:
        raise OSError("simulated failure")

    monkeypatch.setattr(wipe_module, "_write_json_file", _boom)

    with pytest.raises(WipeAborted):
        archive_and_wipe("fake-dsn", tmp_path, connect=_make_connect(conn))

    assert conn.closed


def test_wipe_report_is_a_plain_dataclass_with_the_documented_fields(tmp_path: Path) -> None:
    conn = _FakeConnection(_seeded_tables())
    report = archive_and_wipe("fake-dsn", tmp_path, connect=_make_connect(conn))
    assert isinstance(report, WipeReport)
    assert report.archive_dir == tmp_path
    assert report.manifest_path == tmp_path / "manifest.json"
    assert isinstance(report.timestamp, str) and report.timestamp


# ================================================================================================
# Batch-review repair (round 3, minor finding) — AC3's "WipeReport records the substrate as
# cold_boot" is threaded through from the already-computed check_substrate_guard() value, not
# just computed and discarded.
# ================================================================================================


def test_wipe_report_defaults_substrate_to_cold_boot_when_caller_passes_nothing(
    tmp_path: Path,
) -> None:
    conn = _FakeConnection(_seeded_tables())
    report = archive_and_wipe("fake-dsn", tmp_path, connect=_make_connect(conn))
    assert report.substrate == "cold_boot"


def test_wipe_report_carries_the_substrate_value_the_caller_passed_through(
    tmp_path: Path,
) -> None:
    conn = _FakeConnection(_seeded_tables())
    report = archive_and_wipe(
        "fake-dsn", tmp_path, connect=_make_connect(conn), substrate="cold_boot"
    )
    assert report.substrate == "cold_boot"
