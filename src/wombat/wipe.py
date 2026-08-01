"""wipe — the archive-then-truncate core for the Postgres tier (TK-334, DEC-75/DEC-76/DEC-77 r4),
plus TK-335's filesystem tier and durable-substrate fail-loud guard (DEC-77 r7).

``archive_and_wipe(dsn, archive_dir, *, connect=psycopg.connect) -> WipeReport`` is EP-38's
engine: a Jim-directed "wipe wombat's memory and start clean" act that must be SYSTEMIC (every
public base table, not a hand-kept list) and never lose data silently.

TK-335 adds two more standalone pieces that ``wombat.__main__``'s ``wipe`` subcommand composes
with the above (guard, then Postgres, then filesystem — see that module for the orchestration):
  - ``check_substrate_guard()`` — DEC-77 r7's fail-loud check for a durable cog-worx substrate
    endpoint. Raises before any archive or destructive act; v1 ships no Neo4j purge code.
  - ``wipe_filesystem_tier()`` — brief/feedback/trail text artifacts (copied then zeroed), the
    trail's sidecar cursor (deleted), and the ASR voice-drop directory (audio MOVED into the
    archive with a manifest).

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
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import psycopg

from wombat.sources.asr import _FAILED_DIRNAME as _ASR_FAILED_DIRNAME
from wombat.sources.asr import _PROCESSED_DIRNAME as _ASR_PROCESSED_DIRNAME
from wombat.substrate import SubstrateConfig
from wombat.trail.renderer import _sidecar_path_for

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


# ================================================================================================
# TK-335 — the durable-substrate fail-loud guard (DEC-77 r7, binding, supersedes the ticket's
# original wording).
# ================================================================================================

# Source-verified (DEC-77 r7): cog-worx's adapters.config.SubstrateSettings is the ONLY real
# endpoint surface (env_prefix="COGWORX_"). It is read directly from os.environ here, never by
# constructing SubstrateSettings() — that class ships non-blank defaults for every field, so an
# unconfigured environment would still read as "configured" if we ever instantiated it.
_COGWORX_SUBSTRATE_ENV_VARS: tuple[str, ...] = ("COGWORX_NEO4J_URI", "COGWORX_PG_DSN")


class DurableSubstrateConfigured(WipeAborted):
    """Raised when a durable cog-worx substrate endpoint is configured (DEC-77 r7) — the wipe
    cannot safely reach data living outside what it archives, so it aborts by name before any
    archive or destructive act rather than silently under-wiping. A ``WipeAborted`` subclass so
    existing callers that catch the base class still catch this."""


def check_substrate_guard(substrate_config: SubstrateConfig | None = None) -> str:
    """DEC-77 r7: wombat has NO neo4j field anywhere in config.py/params.py/wombat_params.yaml,
    and ``substrate.SubstrateConfig`` has zero real callers — both ``bootstrap.build_substrate()``
    call sites pass no config, i.e. the cold-boot in-memory doubles. So the only real signal that
    a durable substrate is wired is cog-worx's own ``COGWORX_`` endpoint prefix, PLUS an explicit
    ``SubstrateConfig`` ever handed to this call (structural — nothing in wombat constructs one
    today).

    Returns ``"cold_boot"`` when neither signal is present. Raises ``DurableSubstrateConfigured``
    otherwise, naming the specific store, BEFORE any archive or destructive act — v1 ships no
    Neo4j purge code (DEC-75d); this guard is the whole obligation.
    """
    if substrate_config is not None:
        raise DurableSubstrateConfigured(
            "a wombat SubstrateConfig was supplied to the wipe — a durable substrate is wired "
            "and this wipe cannot safely reach it. Aborting before any archive or destructive act."
        )
    for var in _COGWORX_SUBSTRATE_ENV_VARS:
        if os.environ.get(var, "").strip():
            raise DurableSubstrateConfigured(
                f"{var} is set — a durable cog-worx substrate endpoint is configured and this "
                "wipe cannot safely reach it. Aborting before any archive or destructive act."
            )
    return "cold_boot"


# ================================================================================================
# TK-335 — the filesystem tier: brief/feedback/trail text artifacts (copied then zeroed), the
# trail's sidecar cursor (deleted), and the ASR voice-drop directory (audio MOVED into the
# archive with a manifest, never base64'd into JSON).
# ================================================================================================


@dataclass
class FileWipeReport:
    """What ``wipe_filesystem_tier`` actually did, for a caller (TK-335's CLI) to report."""

    files_dir: Path
    text_files_archived: list[str]
    text_files_truncated: list[str]
    sidecar_deleted: bool
    voice_drop_manifest_path: Path | None
    voice_drop_files: list[str]


def wipe_filesystem_tier(
    archive_dir: Path,
    *,
    brief_path: Path | None,
    feedback_path: Path | None,
    trail_log_path: Path,
    asr_drop_dir: Path | None,
) -> FileWipeReport:
    """Archive then wipe the non-Postgres persistence tier under ``archive_dir/files/`` (TK-335).

    Text artifacts (``brief_path``/``feedback_path``/``trail_log_path`` — any that is ``None`` or
    does not exist on disk is skipped, the same OPTIONAL-channel loud-skip posture as
    ``sources.bootstrap``'s ``_maybe_register_*`` fields) are COPIED byte-identically under
    ``files/`` by basename, then TRUNCATED TO ZERO BYTES — the file keeps existing, never removed
    (the same empty-it-do-not-remove-the-thing-it-needs rule ``archive_and_wipe`` applies to
    tables). The trail's dedup-cursor sidecar (``trail.renderer._sidecar_path_for``) is DELETED
    outright, never archived — it is a cursor, not user data.

    ``asr_drop_dir`` (if set and present) has every audio file in its root PLUS its
    ``processed/``/``failed/`` subdirectories (``sources.asr``'s own dirnames) MOVED into
    ``files/voice_drop/`` — each recorded name is prefixed with its subdirectory
    (``"processed/foo.wav"``) so same-named files from different subdirectories never collide —
    alongside ``voice-drop-manifest.json`` (name/size/mtime/sha256 per file), never base64'd into
    JSON. The drop dir and its subdirectories are left in place, empty.
    """
    archive_dir = Path(archive_dir)
    files_dir = archive_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    archived: list[str] = []
    truncated: list[str] = []
    for text_path in (brief_path, feedback_path, trail_log_path):
        if text_path is None or not text_path.exists():
            continue
        shutil.copyfile(text_path, files_dir / text_path.name)
        archived.append(text_path.name)
        text_path.write_bytes(b"")
        truncated.append(text_path.name)

    sidecar_path = _sidecar_path_for(Path(trail_log_path))
    sidecar_deleted = sidecar_path.exists()
    if sidecar_deleted:
        sidecar_path.unlink()

    voice_drop_manifest_path: Path | None = None
    voice_drop_files: list[str] = []
    if asr_drop_dir is not None and Path(asr_drop_dir).is_dir():
        drop_dir = Path(asr_drop_dir)
        voice_drop_dir = files_dir / "voice_drop"
        manifest_entries: list[dict[str, Any]] = []
        for subdir_name in (None, _ASR_PROCESSED_DIRNAME, _ASR_FAILED_DIRNAME):
            scan_dir = drop_dir / subdir_name if subdir_name else drop_dir
            if not scan_dir.is_dir():
                continue
            for audio_path in sorted(scan_dir.iterdir()):
                if not audio_path.is_file():
                    continue
                relative_name = (
                    f"{subdir_name}/{audio_path.name}" if subdir_name else audio_path.name
                )
                stat = audio_path.stat()
                digest = hashlib.sha256(audio_path.read_bytes()).hexdigest()
                dest = voice_drop_dir / relative_name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(audio_path), str(dest))
                manifest_entries.append(
                    {
                        "name": relative_name,
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                        "sha256": digest,
                    }
                )
                voice_drop_files.append(relative_name)
        voice_drop_manifest_path = files_dir / "voice-drop-manifest.json"
        _write_json_file(voice_drop_manifest_path, manifest_entries)

    return FileWipeReport(
        files_dir=files_dir,
        text_files_archived=archived,
        text_files_truncated=truncated,
        sidecar_deleted=sidecar_deleted,
        voice_drop_manifest_path=voice_drop_manifest_path,
        voice_drop_files=voice_drop_files,
    )


__all__ = [
    "DurableSubstrateConfigured",
    "FileWipeReport",
    "WipeAborted",
    "WipeReport",
    "archive_and_wipe",
    "check_substrate_guard",
    "wipe_filesystem_tier",
]
