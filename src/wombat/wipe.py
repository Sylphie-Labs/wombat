"""wipe — the archive-then-truncate core for the Postgres tier (TK-334, DEC-75/DEC-76/DEC-77 r4).

``archive_and_wipe(dsn, archive_dir, *, connect=psycopg.connect) -> WipeReport`` is EP-38's
engine: a Jim-directed "wipe wombat's memory and start clean" act that must be SYSTEMIC (every
public base table, not a hand-kept list) and never lose data silently.

FOUR PHASES, IN ORDER, FAIL-CLOSED:
  (1) ENUMERATE BY INTROSPECTION (DEC-75b) — ``SELECT table_name FROM information_schema.tables
      WHERE table_schema = 'public' AND table_type = 'BASE TABLE'``. This is the whole reason the
      wipe is drift-proof: a future migration (or the cog-worx ``cogworx_*`` tables, if the real
      substrate is ever wired onto this DSN) is swept with zero edits here. Nothing in this
      module names a table.
  (2) ARCHIVE FIRST — one JSON file per table under ``archive_dir`` (rows as JSON-native dicts,
      Q-49 convention: every value is either already JSON-native from psycopg's own type mapping
      — JSONB columns decode to plain dict/list — or a ``datetime``/``date``, converted to its
      ISO string) plus ``manifest.json`` (per-table ``row_count``, per-file sha256, an ISO
      timestamp, and the keep-list actually applied). Every file is fsync'd, THEN READ BACK and
      its sha256 recomputed from the bytes on disk before a single destructive statement runs.
  (3) DESTROY — ONE ``TRUNCATE TABLE ... RESTART IDENTITY CASCADE`` naming every non-keep-listed
      table, inside the ONE transaction psycopg already keeps open. Never ``DROP``, never a
      per-table ``DELETE`` loop — schema, indexes, and the NG-3 no-migration-framework posture
      all survive; ``schema_preflight.ensure_all_schemas`` stays a no-op afterward.
  (4) KEEP-LIST is EXACTLY ``{wombat_settings}`` (DEC-76, Jim-confirmed, binding) — archived but
      NOT truncated. The archived-but-not-truncated asymmetry is a deliberate, tested property.

Any archive write or verification failure raises ``WipeAborted`` before phase (3) ever runs —
zero rows touched (CON-4's "every side effect appears before it happens" applied to deletion).

``connect`` is keyword-only with a ``psycopg.connect`` default (DEC-77 r4): every real caller
passes exactly two positional args (``dsn``, ``archive_dir``); ``tests/unit/test_wipe.py``
injects a recording fake to prove the abort-before-destroy ordering with zero Postgres.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import psycopg

# DEC-76 (Jim-confirmed, binding): exactly this set. A builder must not widen or narrow it —
# wombat_settings is the DEC-43 configuration tier (peer of .env/keyring), not memory, so it is
# archived alongside everything else but deliberately survives the TRUNCATE.
_KEEP_LIST: frozenset[str] = frozenset({"wombat_settings"})

_MANIFEST_FILENAME = "manifest.json"

# DEC-77 r4's injectable seam only needs to be DB-API-shaped (``cursor()``/``commit()``/
# ``close()``, and a cursor with ``execute``/``fetchall``/``description``) — real callers get a
# real ``psycopg.Connection``; ``tests/unit/test_wipe.py`` injects a recording fake with zero
# Postgres. Typed loosely on purpose: pinning this to ``psycopg.Connection`` would defeat the
# whole point of the seam (a fake that is not a real psycopg connection could never satisfy it).
_PgConnection = Any


class WipeAborted(Exception):
    """Raised when an archive write or verification failure occurs before any destructive
    statement is issued. Structural guarantee: zero rows are ever touched when this is raised."""


@dataclass
class WipeReport:
    """What ``archive_and_wipe`` actually did, for a caller (TK-335's CLI) to report."""

    tables: list[str]
    row_counts: dict[str, int]
    truncated: list[str]
    kept: list[str]
    archive_dir: Path
    manifest_path: Path
    timestamp: str


def archive_and_wipe(
    dsn: str,
    archive_dir: Path,
    *,
    connect: Callable[[str], _PgConnection] = psycopg.connect,
) -> WipeReport:
    """Archive every public base table on ``dsn`` to JSON under ``archive_dir``, verify every
    file, then ``TRUNCATE`` everything except ``wombat_settings`` in one transaction.

    Raises ``WipeAborted`` (zero rows touched) on any archive write/verification failure. Never
    ``DROP``s, never runs a per-table ``DELETE`` — see the module docstring for the four phases.
    """
    archive_dir = Path(archive_dir)
    conn = connect(dsn)
    try:
        tables = _enumerate_base_tables(conn)
        archive_dir.mkdir(parents=True, exist_ok=True)

        manifest_tables: dict[str, dict[str, Any]] = {}
        try:
            for table in tables:
                rows = _fetch_table_rows(conn, table)
                digest = _write_json_file(archive_dir / f"{table}.json", rows)
                manifest_tables[table] = {"row_count": len(rows), "sha256": digest}

            timestamp = datetime.now(UTC).isoformat()
            manifest = {
                "timestamp": timestamp,
                "keep_list": sorted(_KEEP_LIST),
                "tables": manifest_tables,
            }
            manifest_path = archive_dir / _MANIFEST_FILENAME
            _write_json_file(manifest_path, manifest)
        except OSError as exc:
            raise WipeAborted(
                f"archive write/verification failed before any destructive statement was "
                f"issued — zero rows touched: {exc}"
            ) from exc

        to_truncate = sorted(t for t in tables if t not in _KEEP_LIST)
        if to_truncate:
            _truncate_tables(conn, to_truncate)

        return WipeReport(
            tables=tables,
            row_counts={t: manifest_tables[t]["row_count"] for t in tables},
            truncated=to_truncate,
            kept=sorted(t for t in tables if t in _KEEP_LIST),
            archive_dir=archive_dir,
            manifest_path=manifest_path,
            timestamp=timestamp,
        )
    finally:
        conn.close()


def _enumerate_base_tables(conn: _PgConnection) -> list[str]:
    """The single most load-bearing 'how' in DEC-75(b) — runtime introspection, never a
    hand-kept list. Drives both AC5 (an unmigrated ``wombat_drift_probe`` table is swept) and
    the systemic/drift-proof property the whole ticket exists for."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
            "ORDER BY table_name"
        )
        rows = cur.fetchall()
    return [row[0] for row in rows]


def _fetch_table_rows(conn: _PgConnection, table: str) -> list[dict[str, Any]]:
    """Every row of ``table`` as a JSON-native dict (Q-49 convention). Column names come from
    ``cursor.description`` (index 0, the DB-API convention) so this works for a table this
    module has never heard of — the AC5 drift-probe proof."""
    with conn.cursor() as cur:
        cur.execute(f'SELECT * FROM "{table}"')
        columns = [col[0] for col in cur.description]
        raw_rows = cur.fetchall()
    return [
        {col: _json_native(val) for col, val in zip(columns, row, strict=True)}
        for row in raw_rows
    ]


def _json_native(value: Any) -> Any:
    """``datetime``/``date`` -> ISO string; everything else psycopg already hands back
    JSON-native (JSONB columns decode to plain dict/list, TEXT/INT/BOOL/FLOAT are native)."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def _write_json_file(path: Path, data: Any) -> str:
    """Write ``data`` as JSON to ``path``, fsync it, then READ IT BACK and recompute its sha256
    from the bytes actually on disk (never the in-memory bytes) — the archive-fidelity proof
    AC1/AC4 pin. Any write/fsync/read/mismatch failure raises ``OSError``, which the caller
    turns into ``WipeAborted`` before any destructive statement runs."""
    payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    with open(path, "wb") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    on_disk = path.read_bytes()
    if on_disk != payload:
        raise OSError(f"archive verification failed: {path} does not match the written bytes")
    return hashlib.sha256(on_disk).hexdigest()


def _truncate_tables(conn: _PgConnection, tables: list[str]) -> None:
    """ONE ``TRUNCATE TABLE ... RESTART IDENTITY CASCADE`` naming every table in ``tables``,
    inside ONE transaction — never ``DROP``, never a per-table ``DELETE`` loop."""
    identifiers = ", ".join(f'"{t}"' for t in tables)
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE TABLE {identifiers} RESTART IDENTITY CASCADE")
    conn.commit()


__all__ = [
    "WipeAborted",
    "WipeReport",
    "archive_and_wipe",
]
