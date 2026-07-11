"""settings_store — Postgres persistence for wombat's app-editable settings (TK-240, DEC-43/44).

Owns ``wombat_settings`` (``key`` TEXT PRIMARY KEY, ``value`` JSONB NOT NULL, ``updated_at``
TIMESTAMPTZ NOT NULL DEFAULT now()). ``ensure_schema(conn)`` is the packaged, idempotent
``CREATE TABLE IF NOT EXISTS`` (NG-3: no migration framework — the five-sibling precedent,
``migrations/007_wombat_settings.sql``), wired as ``schema_preflight.ensure_all_schemas``'s SIXTH
entry.

``SettingsStore`` is a ``dsn``-injected psycopg reader/writer (the Q-46 lazy-connection
convention): ``get_all()`` returns every row as a plain ``dict``; ``put(mapping)`` upserts, bumping
``updated_at`` on every write. ``put`` REFUSES loudly (raises ``SecretFieldRefused``) any key that
names a ``SecretStr``-typed ``WombatConfig`` field — secrets/the DSN never live in this table.

``import_legacy_settings_file`` is a SEPARATE, opt-in-only function (DEC-44, superseding DEC-43's
fold-in-into-``ensure_schema`` mechanics, which ate the operator's real settings file TWICE):
neither ``ensure_schema`` nor ``ensure_all_schemas`` ever calls it. Exactly two production call
sites exist ever (DEC-44) — ``wombat.runtime.serve()`` (this ticket) and the ``settings_app``
``__main__`` entry point (TK-242). It is a one-time migration, guarded by the table being EMPTY:
if ``wombat_settings`` already holds any row, it is a no-op (idempotent across restarts). On a
genuine one-time run it reads the cwd-relative ``wombat.settings.json`` (``config.
WOMBAT_SETTINGS_FILE``), lands ``config.APP_EDITABLE_FIELDS`` keys plus ``wombat_persona_pins``
as rows, drops any secret-field key with exactly one ``logger.warning`` naming it, and — never
deletes (removal discipline) — RENAMES the file to ``wombat.settings.json.migrated``.

STRUCTURAL: this module imports NOTHING from ``wombat.bootstrap`` or ``wombat.runtime``.
"""

from __future__ import annotations

import json
import logging
from importlib import resources
from pathlib import Path
from typing import Any, get_args

import psycopg
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from .config import APP_EDITABLE_FIELDS, WombatConfig

logger = logging.getLogger(__name__)

_MIGRATION_PACKAGE = "wombat.migrations"
_MIGRATION_FILENAME = "007_wombat_settings.sql"

TABLE = "wombat_settings"

# The gitignored, app-editable, CWD-relative legacy settings file (TK-196). Canonical home moved
# here from ``config.py`` by TK-241/R1 (DEC-43): ``config.py`` no longer reads this file at all —
# WombatConfig's app-editable tier now sources from ``wombat_settings`` directly — so the file's
# ONLY remaining reader is this module's one-time ``import_legacy_settings_file``.
WOMBAT_SETTINGS_FILE = "wombat.settings.json"

# The TK-214 persona-pin key (persona/live.py's _PERSONA_PINS_KEY) — carried through the legacy
# import alongside APP_EDITABLE_FIELDS, though it is not itself a WombatConfig field.
PERSONA_PINS_KEY = "wombat_persona_pins"

_MIGRATED_SUFFIX = ".migrated"


class SecretFieldRefused(ValueError):
    """Raised by ``SettingsStore.put`` when a key names a ``SecretStr``-typed config field."""


def ensure_schema(conn: psycopg.Connection[Any]) -> None:
    """Apply the packaged, idempotent ``wombat_settings`` migration on ``conn``.

    Reads ``migrations/007_wombat_settings.sql`` via ``importlib.resources`` and executes it
    as-is (``CREATE TABLE IF NOT EXISTS`` — safe to call every process start, NG-3: no migration
    framework). Callers: tests and ``schema_preflight.ensure_all_schemas``. Never calls
    ``import_legacy_settings_file`` (DEC-44) — schema application and the one-time legacy import
    are deliberately separate concerns.
    """
    sql = resources.files(_MIGRATION_PACKAGE).joinpath(_MIGRATION_FILENAME).read_text(
        encoding="utf-8"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def _secret_field_names() -> set[str]:
    """The ``WombatConfig`` field names typed ``SecretStr`` (or ``SecretStr | None``) — mirrors
    ``config._SettingsTableSource.__call__``'s own secret-field detection exactly."""
    return {
        name
        for name, field in WombatConfig.model_fields.items()
        if field.annotation is SecretStr or SecretStr in get_args(field.annotation)
    }


class SettingsStore:
    """The Postgres-backed reader/writer over ``wombat_settings`` (TK-240, Q-46 conventions).

    ``dsn`` is an injected constructor arg (no module-level DSN literal); this class owns exactly
    ONE lazy psycopg (v3) connection, opened on first use — ``close()`` releases it, no pooling
    (single-host, DEC-6). Schema is applied separately via module-level ``ensure_schema`` — never
    invoked automatically inside ``get_all``/``put``.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: psycopg.Connection[Any] | None = None

    def _connection(self) -> psycopg.Connection[Any]:
        if self._conn is None:
            self._conn = psycopg.connect(self._dsn)
        return self._conn

    def close(self) -> None:
        """Release the lazily-opened connection, if one was ever opened."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def get_all(self) -> dict[str, Any]:
        """Return every ``wombat_settings`` row as a plain ``{key: value}`` dict."""
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(f"SELECT key, value FROM {TABLE}")
            rows = cur.fetchall()
        conn.commit()
        return {row[0]: row[1] for row in rows}

    def put(self, mapping: dict[str, Any]) -> None:
        """Upsert every ``{key: value}`` pair in ``mapping``, bumping ``updated_at`` to now() on
        every write (including a re-write of an existing key).

        REFUSES loudly (``SecretFieldRefused``, naming every offending key) if ANY key names a
        ``SecretStr``-typed ``WombatConfig`` field — checked BEFORE any row is written, so a
        refusal never partially applies ``mapping``. Secrets/the DSN never live in this table.
        """
        secret_keys = sorted(set(mapping) & _secret_field_names())
        if secret_keys:
            raise SecretFieldRefused(
                f"refusing to store secret field(s) {secret_keys!r} in {TABLE}; "
                "secrets never live in the settings table"
            )
        if not mapping:
            return
        conn = self._connection()
        with conn.cursor() as cur:
            for key, value in mapping.items():
                cur.execute(
                    f"""
                    INSERT INTO {TABLE} (key, value, updated_at)
                    VALUES (%s, %s, now())
                    ON CONFLICT (key) DO UPDATE SET
                        value = EXCLUDED.value,
                        updated_at = now()
                    """,
                    (key, Jsonb(value)),
                )
        conn.commit()


def import_legacy_settings_file(dsn: str) -> None:
    """One-time, opt-in-only legacy import of the cwd ``wombat.settings.json`` file into
    ``wombat_settings`` (DEC-44, superseding DEC-43's fold-in-into-``ensure_schema`` mechanics).

    NEVER called by ``ensure_schema``/``ensure_all_schemas`` — callers invoke this explicitly,
    AFTER schema has already been applied. Guarded by the table being EMPTY (a no-op otherwise,
    making a restart idempotent): if ``wombat_settings`` already holds any row, this returns
    immediately without touching the filesystem.

    On a genuine one-time run: if the cwd-relative ``wombat.settings.json`` is missing, unreadable,
    or does not parse to a JSON object, this is a no-op (never crashes boot, CON-3 posture).
    Otherwise, ``config.APP_EDITABLE_FIELDS`` keys plus ``wombat_persona_pins`` (TK-214) are landed
    as rows; a key naming a ``SecretStr``-typed ``WombatConfig`` field is DROPPED with exactly one
    ``logger.warning`` naming it (never written); any other key is dropped silently. The file is
    then RENAMED (never deleted — removal discipline) to ``wombat.settings.json.migrated``,
    verbatim bytes, so the empty-table guard makes this a true one-time act.
    """
    conn = psycopg.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM {TABLE} LIMIT 1")
            if cur.fetchone() is not None:
                return  # already migrated (or otherwise populated) — one-time guard, no-op

        path = Path(WOMBAT_SETTINGS_FILE)
        try:
            raw = path.read_text(encoding="utf-8")
            loaded = json.loads(raw)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return  # missing/unreadable/malformed — never crashes boot (CON-3)
        if not isinstance(loaded, dict):
            return

        admitted_keys = set(APP_EDITABLE_FIELDS) | {PERSONA_PINS_KEY}
        secret_keys = _secret_field_names()
        to_write: dict[str, Any] = {}
        for key, value in loaded.items():
            if key in admitted_keys:
                to_write[key] = value
            elif key in secret_keys:
                logger.warning(
                    "%s contains %r, a secret field; dropping it from the legacy import "
                    "(secrets never live in wombat_settings)",
                    WOMBAT_SETTINGS_FILE,
                    key,
                )
            # else: not an admitted field — dropped silently.

        if to_write:
            with conn.cursor() as cur:
                for key, value in to_write.items():
                    cur.execute(
                        f"""
                        INSERT INTO {TABLE} (key, value, updated_at)
                        VALUES (%s, %s, now())
                        ON CONFLICT (key) DO UPDATE SET
                            value = EXCLUDED.value,
                            updated_at = now()
                        """,
                        (key, Jsonb(value)),
                    )
            conn.commit()

        try:
            path.rename(path.with_name(path.name + _MIGRATED_SUFFIX))
        except FileNotFoundError:
            # TK-241/R2: serve() and settings_app can both reach this function on a simultaneous
            # boot; the empty-table guard above lets both past it when the table is still empty
            # at the time each checks, but only one process wins the rename. The other process's
            # rename target is already gone (not lost — the winner already migrated it) — log and
            # continue, never crash boot over losing this race (CON-3).
            logger.info(
                "%s was already migrated by another process during this import; continuing",
                WOMBAT_SETTINGS_FILE,
            )
    finally:
        conn.close()


__all__ = [
    "PERSONA_PINS_KEY",
    "TABLE",
    "SecretFieldRefused",
    "SettingsStore",
    "ensure_schema",
    "import_legacy_settings_file",
]
