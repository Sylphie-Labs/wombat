"""TK-240 — settings_store acceptance criteria (DEC-43/DEC-44).

DB tests (AC1/AC2/AC3-db) require a REAL Postgres and are gated on ``WOMBAT_TEST_PG_DSN`` (the
same convention as ``tests/unit/test_schema_preflight.py``): absent it, tests are skipped LOUDLY.

  AC1 ``ensure_all_schemas``-equivalent shape pin: ``ensure_schema`` creates ``wombat_settings``
      with the pinned columns, a second call raises/changes nothing; ``SettingsStore.put`` then
      ``get_all`` round-trips, and ``updated_at`` bumps on a second ``put`` of the same key.
  AC2 the DEC-44 legacy import: an empty table + a tmp ``wombat.settings.json`` carrying admitted
      keys, a secret ``*_api_key`` key, and ``wombat_persona_pins`` — lands admitted keys+pins as
      rows, drops the secret with one WARNING naming it, renames the file to ``.migrated`` with
      bytes verbatim; a second run (recreated file, now non-empty table) imports nothing.
  AC3 the DEC-44 hazard pin: bare ``ensure_schema``/``ensure_all_schemas`` NEVER call the import —
      an empty db + a valid settings file in cwd stays untouched under its original name after
      schema application alone; a structural/grep test proves neither pre-flight function
      contains an import call, and ``runtime.serve`` is the sole in-repo call site (TK-242 is not
      landed yet).
  AC4 structural: ``settings_store`` imports nothing from ``wombat.bootstrap``/``wombat.runtime``.

Per the v2.58(a) ruling, EVERY test here that constructs or exercises
``import_legacy_settings_file`` (or anything touching ``wombat.settings.json`` resolution)
``monkeypatch.chdir(tmp_path)`` FIRST — this must never see the repo root, where the operator's
real settings file lives.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
from datetime import datetime
from pathlib import Path

import psycopg
import pytest

from wombat import schema_preflight, settings_store
from wombat.schema_preflight import ensure_all_schemas
from wombat.settings_store import (
    PERSONA_PINS_KEY,
    SecretFieldRefused,
    SettingsStore,
    ensure_schema,
    import_legacy_settings_file,
)

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping settings_store DB tests that require a real "
        "throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres"
    ),
)


@pytest.fixture
def fresh_table() -> None:
    """Drop ``wombat_settings``, simulating a brand-new empty Postgres."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS wombat_settings CASCADE")
        conn.commit()


def _columns(dsn: str) -> dict[str, str]:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'wombat_settings'"
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def _updated_at(dsn: str, key: str) -> datetime:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT updated_at FROM wombat_settings WHERE key = %s", (key,))
        row = cur.fetchone()
        assert row is not None
        value: datetime = row[0]
        return value


# --------------------------------------------------------------------------------------- AC1


@_requires_pg
def test_ac1_ensure_schema_creates_pinned_shape_and_is_idempotent(fresh_table: None) -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        ensure_schema(conn)  # must not raise, must not change anything

    cols = _columns(_DSN)
    assert cols["key"] == "text"
    assert cols["value"] == "jsonb"
    assert cols["updated_at"] == "timestamp with time zone"


@_requires_pg
def test_ac1_put_then_get_all_round_trips_and_bumps_updated_at(fresh_table: None) -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        conn.commit()

    store = SettingsStore(_DSN)
    try:
        store.put({"wombat_assistant_name": "John"})
        assert store.get_all() == {"wombat_assistant_name": "John"}
        first_updated = _updated_at(_DSN, "wombat_assistant_name")

        store.put({"wombat_assistant_name": "Jane"})
        assert store.get_all() == {"wombat_assistant_name": "Jane"}
        second_updated = _updated_at(_DSN, "wombat_assistant_name")
        assert second_updated >= first_updated
    finally:
        store.close()


@_requires_pg
def test_put_refuses_secret_field_and_writes_nothing(fresh_table: None) -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        conn.commit()

    store = SettingsStore(_DSN)
    try:
        with pytest.raises(SecretFieldRefused, match="deepseek_api_key"):
            store.put({"deepseek_api_key": "sk-should-never-land", "wombat_assistant_name": "X"})
        assert store.get_all() == {}
    finally:
        store.close()


# --------------------------------------------------------------------------------------- AC2


@_requires_pg
def test_ac2_import_lands_admitted_keys_and_pins_drops_secret_renames_file(
    fresh_table: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        conn.commit()

    monkeypatch.chdir(tmp_path)
    settings_path = tmp_path / "wombat.settings.json"
    body = {
        "wombat_assistant_name": "John",
        PERSONA_PINS_KEY: {"humor": "2026-01-01T00:00:00+00:00"},
        "deepseek_api_key": "sk-must-never-land",
    }
    original_bytes = json.dumps(body).encode("utf-8")
    settings_path.write_bytes(original_bytes)

    with caplog.at_level(logging.WARNING):
        import_legacy_settings_file(_DSN)

    store = SettingsStore(_DSN)
    try:
        rows = store.get_all()
    finally:
        store.close()
    assert rows == {
        "wombat_assistant_name": "John",
        PERSONA_PINS_KEY: {"humor": "2026-01-01T00:00:00+00:00"},
    }
    assert "deepseek_api_key" in caplog.text
    assert sum("deepseek_api_key" in r.message for r in caplog.records) == 1

    assert not settings_path.exists()
    migrated = tmp_path / "wombat.settings.json.migrated"
    assert migrated.exists()
    assert migrated.read_bytes() == original_bytes


@_requires_pg
def test_ac2_second_run_recreated_file_non_empty_table_imports_nothing(
    fresh_table: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        conn.commit()

    monkeypatch.chdir(tmp_path)
    settings_path = tmp_path / "wombat.settings.json"
    settings_path.write_text(
        json.dumps({"wombat_assistant_name": "First"}), encoding="utf-8"
    )
    import_legacy_settings_file(_DSN)

    # Recreate the file (simulating a second boot after a fresh drop-in) — the table is now
    # non-empty, so the guard must make this a pure no-op: no new/changed rows, file untouched.
    settings_path.write_text(
        json.dumps({"wombat_assistant_name": "Second"}), encoding="utf-8"
    )
    import_legacy_settings_file(_DSN)

    store = SettingsStore(_DSN)
    try:
        rows = store.get_all()
    finally:
        store.close()
    assert rows == {"wombat_assistant_name": "First"}  # unchanged by the second run
    assert settings_path.exists()  # never touched the second time
    assert settings_path.read_text(encoding="utf-8") == json.dumps(
        {"wombat_assistant_name": "Second"}
    )


# --------------------------------------------------------------------------------------- AC3


@_requires_pg
def test_ac3_bare_ensure_schema_never_imports_leaves_file_untouched(
    fresh_table: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _DSN is not None
    monkeypatch.chdir(tmp_path)
    settings_path = tmp_path / "wombat.settings.json"
    original_bytes = json.dumps({"wombat_assistant_name": "John"}).encode("utf-8")
    settings_path.write_bytes(original_bytes)

    ensure_all_schemas(_DSN)
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        conn.commit()

    store = SettingsStore(_DSN)
    try:
        assert store.get_all() == {}  # table created EMPTY — no import ever ran
    finally:
        store.close()
    assert settings_path.exists()
    assert settings_path.read_bytes() == original_bytes  # byte-untouched, original name


def test_ac3_ensure_schema_and_ensure_all_schemas_never_call_the_import() -> None:
    """Structural: neither pre-flight function's source references
    ``import_legacy_settings_file`` (DEC-44 — the fold-in-into-ensure_schema mechanics this
    supersedes are the exact hazard that ate the operator's real settings file twice)."""
    for fn in (ensure_schema, ensure_all_schemas):
        source = inspect.getsource(fn)
        assert "import_legacy_settings_file(" not in source


def test_ac3_runtime_serve_is_the_sole_in_repo_call_site() -> None:
    """Structural: until TK-242 lands the settings_app entry point, ``wombat.runtime.serve`` is
    the ONLY production call site for ``import_legacy_settings_file`` (DEC-44: exactly two ever)."""
    from wombat import runtime

    src_root = Path(settings_store.__file__).parent
    call_sites = []
    for path in src_root.rglob("*.py"):
        if path.name == "settings_store.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "import_legacy_settings_file(" in text:
            call_sites.append(path)
    assert call_sites == [Path(runtime.__file__)]


# --------------------------------------------------------------------------------------- AC4


def test_ac4_settings_store_imports_nothing_from_bootstrap_or_runtime() -> None:
    import ast

    source = Path(settings_store.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
            imported_modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    assert not any("bootstrap" in mod for mod in imported_modules)
    assert not any(mod == "runtime" or mod.endswith(".runtime") for mod in imported_modules)


def test_ac4_schema_preflight_wires_settings_store_as_the_sixth_call() -> None:
    source = inspect.getsource(schema_preflight.ensure_all_schemas)
    assert "ensure_settings_store_schema(conn)" in source
