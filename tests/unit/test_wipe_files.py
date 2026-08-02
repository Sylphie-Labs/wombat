"""tests/unit/test_wipe_files.py — TK-335: the filesystem tier, the named-exclusion proof, and
the durable-substrate fail-loud guard (DEC-77 r3/r7).

Runs WITHOUT Postgres, same as tests/unit/test_wipe.py (TK-334): a minimal hand-rolled
``_FakeConnection``/``_FakeCursor`` pair (an empty ``information_schema.tables`` — the pg tier's
own behavior is already proven thoroughly there) stands in for a "full wipe" so AC2's exclusion
proof genuinely exercises the substrate guard + Postgres tier + filesystem tier together, with
zero real I/O beyond the tmp_path filesystem.

  AC1 file tier — a tmp root with brief.md/feedback.txt/wombat-trail.log + its sidecar, and a
      voice-drop dir with audio in its root, processed/, and failed/.
  AC2 exclusions — logs/, archives/, chat-handshake.json, .env, wombat.settings.json(.migrated),
      wombat_params.yaml, persona_policy.yaml survive a full wipe byte-for-byte (hashed, not a
      comment).
  AC3 substrate guard — COGWORX_NEO4J_URI / COGWORX_PG_DSN (env or a cwd-relative .env, TK-335
      repair) / an explicit SubstrateConfig each abort before any archive or destructive act; the
      cold-boot default returns "cold_boot".
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import wombat.wipe as wipe
from wombat.substrate import SubstrateConfig

# ================================================================================================
# Fakes — a minimal psycopg-shaped connection/cursor exposing an EMPTY information_schema (the
# pg tier's own archive/truncate behavior is already covered by tests/unit/test_wipe.py; here it
# only needs to complete cleanly as one leg of a "full wipe").
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
            self._result = []
            self.description = [("table_name",)]
        else:  # pragma: no cover - the fake DB is deliberately table-free
            raise AssertionError(f"unexpected SQL in fake cursor: {sql!r}")

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._result)


class _FakeConnection:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.committed = False
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        self.closed = True


def _make_connect(conn: _FakeConnection) -> Callable[[str], _FakeConnection]:
    def _connect(dsn: str) -> _FakeConnection:
        return conn

    return _connect


@pytest.fixture(autouse=True)
def _clean_cogworx_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """DEC-77 r7's whole signal is these two env vars — start every test from the cold-boot
    default (zero COGWORX_ vars, matching this machine's real .env today) regardless of what a
    prior test or the ambient shell left behind."""
    monkeypatch.delenv("COGWORX_NEO4J_URI", raising=False)
    monkeypatch.delenv("COGWORX_PG_DSN", raising=False)


# ================================================================================================
# AC1 — the file tier.
# ================================================================================================


def test_file_tier_archives_copies_then_zeroes_text_and_moves_audio(tmp_path: Path) -> None:
    brief = tmp_path / "brief.md"
    brief.write_text("brief content", encoding="utf-8")
    feedback = tmp_path / "feedback.txt"
    feedback.write_text("feedback content", encoding="utf-8")
    trail = tmp_path / "wombat-trail.log"
    trail.write_text("[PROPOSED 2026-08-01T00:00:00+00:00] send_email: hi\n", encoding="utf-8")
    sidecar = tmp_path / "wombat-trail.log.sidecar.json"
    sidecar.write_text("{}", encoding="utf-8")

    drop_dir = tmp_path / "voice_drop"
    processed = drop_dir / "processed"
    failed = drop_dir / "failed"
    processed.mkdir(parents=True)
    failed.mkdir(parents=True)
    root_bytes = b"root-audio-bytes"
    processed_bytes = b"processed-audio-bytes"
    failed_bytes = b"failed-audio-bytes"
    (drop_dir / "root.wav").write_bytes(root_bytes)
    (processed / "done.wav").write_bytes(processed_bytes)
    (failed / "oops.wav").write_bytes(failed_bytes)

    archive_dir = tmp_path / "archive"
    report = wipe.wipe_filesystem_tier(
        archive_dir,
        brief_path=brief,
        feedback_path=feedback,
        trail_log_path=trail,
        asr_drop_dir=drop_dir,
    )

    files_dir = archive_dir / "files"
    assert (files_dir / "brief.md").read_text(encoding="utf-8") == "brief content"
    assert (files_dir / "feedback.txt").read_text(encoding="utf-8") == "feedback content"
    assert (files_dir / "wombat-trail.log").read_text(encoding="utf-8") == (
        "[PROPOSED 2026-08-01T00:00:00+00:00] send_email: hi\n"
    )

    # Zero bytes, file still exists — never removed.
    assert brief.exists() and brief.read_bytes() == b""
    assert feedback.exists() and feedback.read_bytes() == b""
    assert trail.exists() and trail.read_bytes() == b""
    assert not sidecar.exists()

    voice_drop_dir = files_dir / "voice_drop"
    assert (voice_drop_dir / "root.wav").read_bytes() == root_bytes
    assert (voice_drop_dir / "processed" / "done.wav").read_bytes() == processed_bytes
    assert (voice_drop_dir / "failed" / "oops.wav").read_bytes() == failed_bytes

    manifest_path = files_dir / "voice-drop-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_name = {entry["name"]: entry for entry in manifest}
    assert set(by_name) == {"root.wav", "processed/done.wav", "failed/oops.wav"}
    for entry in manifest:
        assert set(entry) == {"name", "size", "mtime", "sha256"}
    assert by_name["root.wav"]["size"] == len(root_bytes)
    assert by_name["root.wav"]["sha256"] == hashlib.sha256(root_bytes).hexdigest()
    assert by_name["processed/done.wav"]["sha256"] == hashlib.sha256(processed_bytes).hexdigest()
    assert by_name["failed/oops.wav"]["sha256"] == hashlib.sha256(failed_bytes).hexdigest()

    # Drop dir + processed/ + failed/ still exist, now empty.
    assert drop_dir.is_dir() and list(drop_dir.glob("*.wav")) == []
    assert processed.is_dir() and list(processed.iterdir()) == []
    assert failed.is_dir() and list(failed.iterdir()) == []

    assert report.sidecar_deleted is True
    assert set(report.text_files_archived) == {"brief.md", "feedback.txt", "wombat-trail.log"}
    assert set(report.text_files_truncated) == {"brief.md", "feedback.txt", "wombat-trail.log"}
    assert set(report.voice_drop_files) == {
        "root.wav",
        "processed/done.wav",
        "failed/oops.wav",
    }
    assert report.voice_drop_manifest_path == manifest_path


def test_file_tier_skips_unset_and_missing_artifacts(tmp_path: Path) -> None:
    """A ``None`` path (channel not configured) and a path that does not exist on disk (never
    written yet) are both loud-skips, never errors — the same OPTIONAL posture as
    ``sources.bootstrap``'s ``_maybe_register_*`` fields."""
    trail = tmp_path / "wombat-trail.log"  # never written

    report = wipe.wipe_filesystem_tier(
        tmp_path / "archive",
        brief_path=None,
        feedback_path=tmp_path / "never-written-feedback.txt",
        trail_log_path=trail,
        asr_drop_dir=None,
    )

    assert report.text_files_archived == []
    assert report.text_files_truncated == []
    assert report.sidecar_deleted is False
    assert report.voice_drop_manifest_path is None
    assert report.voice_drop_files == []
    assert (tmp_path / "archive" / "files").is_dir()


# ================================================================================================
# AC2 — named exclusions survive a full wipe, byte-for-byte (a test, not a comment).
# ================================================================================================


def test_named_exclusions_survive_a_full_wipe_byte_for_byte(tmp_path: Path) -> None:
    excluded = {
        "logs/runtime-20260801-000000.log": b"a runtime log line\n",
        "archives/wipe-20260731-000000/manifest.json": b'{"tables": {}}',
        "chat-handshake.json": b'{"port": 12345, "token": "tok"}',
        ".env": b"DEEPSEEK_API_KEY=sk-test\nDEEPSEEK_BASE_URL=https://api.deepseek.com\n",
        "wombat.settings.json": b'{"wombat_ptt_binding": ""}',
        "wombat.settings.json.migrated": b'{"wombat_ptt_binding": ""}',
        "wombat_params.yaml": b"version: 1\n",
        "persona_policy.yaml": b"version: 1\n",
    }
    for relative, content in excluded.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    before = {
        relative: hashlib.sha256((tmp_path / relative).read_bytes()).hexdigest()
        for relative in excluded
    }

    brief = tmp_path / "brief.md"
    brief.write_text("hello", encoding="utf-8")
    trail = tmp_path / "wombat-trail.log"
    trail.write_text("a trail line\n", encoding="utf-8")

    archive_dir = tmp_path / "wipe-archive"
    conn = _FakeConnection()
    substrate = wipe.check_substrate_guard()
    assert substrate == "cold_boot"
    wipe.archive_and_wipe("fake-dsn", archive_dir, connect=_make_connect(conn))
    wipe.wipe_filesystem_tier(
        archive_dir,
        brief_path=brief,
        feedback_path=None,
        trail_log_path=trail,
        asr_drop_dir=None,
    )

    after = {
        relative: hashlib.sha256((tmp_path / relative).read_bytes()).hexdigest()
        for relative in excluded
    }
    assert after == before
    # The wipe itself did happen (proving the exclusions weren't just never reached).
    assert brief.read_bytes() == b""
    assert trail.read_bytes() == b""


# ================================================================================================
# AC3 — the durable-substrate fail-loud guard (DEC-77 r7).
# ================================================================================================


def test_substrate_guard_cold_boot_default_returns_cold_boot() -> None:
    assert wipe.check_substrate_guard() == "cold_boot"


def test_substrate_guard_aborts_on_cogworx_neo4j_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COGWORX_NEO4J_URI", "bolt://durable-host:7687")
    with pytest.raises(wipe.DurableSubstrateConfigured) as exc_info:
        wipe.check_substrate_guard()
    assert "COGWORX_NEO4J_URI" in str(exc_info.value)


def test_substrate_guard_aborts_on_cogworx_pg_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COGWORX_PG_DSN", "postgresql://durable-host/cogworx")
    with pytest.raises(wipe.DurableSubstrateConfigured) as exc_info:
        wipe.check_substrate_guard()
    assert "COGWORX_PG_DSN" in str(exc_info.value)


def test_substrate_guard_aborts_on_cogworx_neo4j_uri_in_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TK-335 repair: wombat's own secrets (WOMBAT_PG_DSN, DEEPSEEK_API_KEY) live only in the
    repo-root .env, never exported — cog-worx's real SubstrateSettings (env_file=".env") reads
    that identical file, so this guard must too, not just os.environ."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("COGWORX_NEO4J_URI=bolt://localhost:7687\n", encoding="utf-8")
    with pytest.raises(wipe.DurableSubstrateConfigured) as exc_info:
        wipe.check_substrate_guard()
    assert "COGWORX_NEO4J_URI" in str(exc_info.value)


def test_substrate_guard_aborts_on_cogworx_pg_dsn_in_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "COGWORX_PG_DSN=postgresql://localhost/cogworx\n", encoding="utf-8"
    )
    with pytest.raises(wipe.DurableSubstrateConfigured) as exc_info:
        wipe.check_substrate_guard()
    assert "COGWORX_PG_DSN" in str(exc_info.value)


def test_substrate_guard_cold_boot_with_dotenv_present_but_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A .env with unrelated keys (the shape of wombat's real repo-root .env today) must not
    false-positive the guard."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "WOMBAT_PG_DSN=postgresql://localhost/wombat\n", encoding="utf-8"
    )
    assert wipe.check_substrate_guard() == "cold_boot"


def test_substrate_guard_aborts_on_explicit_substrate_config() -> None:
    config = SubstrateConfig(
        pg_dsn="postgresql://durable-host/cogworx",
        neo4j_uri="bolt://durable-host:7687",
        neo4j_user="neo4j",
        neo4j_password="pw",
        latent_dim=8,
    )
    with pytest.raises(wipe.DurableSubstrateConfigured):
        wipe.check_substrate_guard(config)


def test_substrate_guard_aborts_before_any_archive_or_destructive_act(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COGWORX_NEO4J_URI", "bolt://durable-host:7687")
    archive_dir = tmp_path / "archive"

    with pytest.raises(wipe.DurableSubstrateConfigured):
        wipe.check_substrate_guard()
        # Never reached — proves the guard fires before archive_and_wipe is even called.
        wipe.archive_and_wipe(
            "fake-dsn", archive_dir, connect=_make_connect(_FakeConnection())
        )

    assert not archive_dir.exists()


def test_substrate_guard_error_is_a_wipe_aborted() -> None:
    """A caller that only catches the base ``WipeAborted`` still catches this."""
    assert issubclass(wipe.DurableSubstrateConfigured, wipe.WipeAborted)
