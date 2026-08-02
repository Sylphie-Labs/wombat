"""tests/unit/test_wipe_cli.py — TK-335: the `python -m wombat wipe` entrypoint, dry-run by
default, the quiesce refusal, and the argv-dispatch boundary (DEC-77 r1/r2/r3).

All tests monkeypatch ``wombat_main.load_config`` to a directly-constructed ``WombatConfig``
(never a live runtime, never real Postgres/network I/O — a DSN pointing at an unreachable host
would otherwise cost several real seconds of DNS resolution per test) and drive ``main()``
through ``sys.argv``, exactly as the real console-script/`` python -m wombat`` entrypoint would.

  AC4 dry-run default — no flags: prints the enumeration + archive path, touches nothing, exits
      nonzero.
  AC5 confirmed run — `--confirm` with no runtime running performs the wipe (engine calls
      monkeypatched, since the engine itself is proven in test_wipe.py/test_wipe_files.py) and
      exits 0 with the archive dir as the final stdout line; a failure exits nonzero with the
      abort reason on stderr.
  AC6 quiesce refusal — `--confirm` while a real loopback socket answers on the singleton port OR
      the handshake port REFUSES, zero destructive acts; a stale (non-answering) handshake port is
      correctly treated as not-live.

Plus the DEC-77 r1 argv-dispatch boundary: an unrecognized subcommand/flag is usage-to-stderr,
exit 2.
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import pytest

from wombat import __main__ as wombat_main
from wombat.config import ConfigurationError, WombatConfig
from wombat.wipe import WipeAborted


def _free_port() -> int:
    """An ephemeral, currently-unused loopback port — mirrors test_main_entry.py's helper."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def _make_config(
    *,
    wombat_pg_dsn: str | None = "postgresql://fake-host/fake-db",
    wombat_singleton_port: int | None = None,
    wombat_chat_handshake_file: str | None = None,
    wombat_brief_path: str | None = None,
    wombat_feedback_file: str | None = None,
    wombat_asr_drop_dir: str | None = None,
) -> WombatConfig:
    """A directly-constructed ``WombatConfig`` — bypasses ``load_config``'s wombat_settings-table
    fetch entirely (that fetch only runs inside ``load_config()``, never inside bare
    ``WombatConfig(...)`` construction), so this is zero-network regardless of the DSN given."""
    return WombatConfig(
        deepseek_api_key="sk-test",
        deepseek_base_url="https://api.deepseek.com",
        wombat_pg_dsn=wombat_pg_dsn,
        wombat_singleton_port=(
            wombat_singleton_port if wombat_singleton_port is not None else _free_port()
        ),
        wombat_chat_handshake_file=wombat_chat_handshake_file,
        wombat_brief_path=wombat_brief_path,
        wombat_feedback_file=wombat_feedback_file,
        wombat_asr_drop_dir=wombat_asr_drop_dir,
    )


# ================================================================================================
# AC4 — dry-run by default.
# ================================================================================================


def test_dry_run_prints_enumeration_and_archive_path_and_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    brief = tmp_path / "brief.md"
    brief.write_text("hello", encoding="utf-8")
    config = _make_config(wombat_brief_path=str(brief))
    monkeypatch.setattr(wombat_main, "load_config", lambda: config)

    engine_calls = {"n": 0}
    monkeypatch.setattr(
        wombat_main, "archive_and_wipe", lambda *a, **k: engine_calls.__setitem__("n", 1)
    )
    monkeypatch.setattr(sys, "argv", ["wombat", "wipe"])

    with pytest.raises(SystemExit) as exc_info:
        wombat_main.main()
    assert exc_info.value.code != 0

    out = capsys.readouterr().out
    assert "would archive" in out.lower()
    assert str(brief) in out
    assert "archive director" in out.lower()  # "archive directory: ..."

    assert engine_calls["n"] == 0
    assert brief.read_text(encoding="utf-8") == "hello"
    assert not (tmp_path / "archives").exists()


def test_dry_run_never_probes_liveness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dry-run must be safe (and informative) even while a runtime IS live — it never calls the
    liveness probe at all."""
    monkeypatch.chdir(tmp_path)
    config = _make_config()
    monkeypatch.setattr(wombat_main, "load_config", lambda: config)

    def _boom(_config: WombatConfig) -> bool:
        raise AssertionError("dry-run must never probe liveness")

    monkeypatch.setattr(wombat_main, "_is_runtime_live", _boom)
    monkeypatch.setattr(sys, "argv", ["wombat", "wipe"])

    with pytest.raises(SystemExit) as exc_info:
        wombat_main.main()
    assert exc_info.value.code != 0
    assert "would archive" in capsys.readouterr().out.lower()


# ================================================================================================
# AC5 — confirmed run.
# ================================================================================================


def test_confirmed_run_performs_wipe_and_prints_archive_dir_as_final_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    config = _make_config()
    monkeypatch.setattr(wombat_main, "load_config", lambda: config)

    archive_dir_used = tmp_path / "archives" / "wipe-20260801-000000"
    guard_calls = 0
    pg_calls: list[tuple[str, Path]] = []
    fs_calls: list[Path] = []

    class _FakeReport:
        archive_dir = archive_dir_used

    def _fake_guard() -> str:
        nonlocal guard_calls
        guard_calls += 1
        return "cold_boot"

    def _fake_archive_and_wipe(dsn: str, archive_dir: Path, **kwargs: object) -> _FakeReport:
        pg_calls.append((dsn, archive_dir))
        return _FakeReport()

    def _fake_wipe_filesystem_tier(archive_dir: Path, **kwargs: object) -> None:
        fs_calls.append(archive_dir)

    monkeypatch.setattr(wombat_main, "check_substrate_guard", _fake_guard)
    monkeypatch.setattr(wombat_main, "archive_and_wipe", _fake_archive_and_wipe)
    monkeypatch.setattr(wombat_main, "wipe_filesystem_tier", _fake_wipe_filesystem_tier)
    monkeypatch.setattr(sys, "argv", ["wombat", "wipe", "--confirm"])

    with pytest.raises(SystemExit) as exc_info:
        wombat_main.main()
    assert exc_info.value.code == 0

    out_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert out_lines[-1] == str(archive_dir_used)
    assert guard_calls == 1
    assert len(pg_calls) == 1
    pg_dsn, pg_archive_dir = pg_calls[0]
    assert pg_dsn == config.wombat_pg_dsn
    # The SAME computed archive_dir is threaded into both tiers.
    assert fs_calls == [pg_archive_dir]


def test_confirmed_run_failure_prints_abort_reason_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    config = _make_config()
    monkeypatch.setattr(wombat_main, "load_config", lambda: config)
    monkeypatch.setattr(wombat_main, "check_substrate_guard", lambda: "cold_boot")

    def _boom(*args: object, **kwargs: object) -> None:
        raise WipeAborted("simulated archive write failure")

    monkeypatch.setattr(wombat_main, "archive_and_wipe", _boom)
    monkeypatch.setattr(sys, "argv", ["wombat", "wipe", "--confirm"])

    with pytest.raises(SystemExit) as exc_info:
        wombat_main.main()
    assert exc_info.value.code != 0
    assert "simulated archive write failure" in capsys.readouterr().err


def test_confirmed_run_with_archive_dir_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    config = _make_config()
    monkeypatch.setattr(wombat_main, "load_config", lambda: config)
    override_dir = tmp_path / "custom-archive"

    seen: dict[str, Path] = {}

    class _FakeReport:
        def __init__(self, archive_dir: Path) -> None:
            self.archive_dir = archive_dir

    def _fake_archive_and_wipe(dsn: str, archive_dir: Path, **kwargs: object) -> _FakeReport:
        seen["archive_dir"] = archive_dir
        return _FakeReport(archive_dir)

    monkeypatch.setattr(wombat_main, "check_substrate_guard", lambda: "cold_boot")
    monkeypatch.setattr(wombat_main, "archive_and_wipe", _fake_archive_and_wipe)
    monkeypatch.setattr(wombat_main, "wipe_filesystem_tier", lambda *a, **k: None)
    monkeypatch.setattr(
        sys, "argv", ["wombat", "wipe", "--confirm", "--archive-dir", str(override_dir)]
    )

    with pytest.raises(SystemExit) as exc_info:
        wombat_main.main()
    assert exc_info.value.code == 0
    assert seen["archive_dir"] == override_dir
    assert capsys.readouterr().out.splitlines()[-1] == str(override_dir)


def test_confirmed_run_threads_substrate_guard_value_into_archive_and_wipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Batch-review repair (round 3, minor finding): ``check_substrate_guard()``'s return value
    must actually reach ``archive_and_wipe`` — previously it was computed and then discarded, so
    AC3's "WipeReport records the substrate as cold_boot" was never actually true anywhere."""
    monkeypatch.chdir(tmp_path)
    config = _make_config()
    monkeypatch.setattr(wombat_main, "load_config", lambda: config)

    captured: dict[str, object] = {}

    class _FakeReport:
        archive_dir = tmp_path / "archives" / "wipe-x"

    def _fake_archive_and_wipe(dsn: str, archive_dir: Path, **kwargs: object) -> _FakeReport:
        captured.update(kwargs)
        return _FakeReport()

    monkeypatch.setattr(wombat_main, "check_substrate_guard", lambda: "cold_boot")
    monkeypatch.setattr(wombat_main, "archive_and_wipe", _fake_archive_and_wipe)
    monkeypatch.setattr(wombat_main, "wipe_filesystem_tier", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["wombat", "wipe", "--confirm"])

    with pytest.raises(SystemExit) as exc_info:
        wombat_main.main()
    assert exc_info.value.code == 0
    assert captured.get("substrate") == "cold_boot"


# ================================================================================================
# Batch-review repair (round 3, minor finding) — a ConfigurationError from load_config() (e.g. a
# missing required env var) must print the SAME clean "wombat wipe: aborted - <reason>" line
# every other failure mode in this command already produces, not a raw Python traceback.
# ================================================================================================


def test_configuration_error_from_load_config_prints_clean_abort_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom() -> WombatConfig:
        raise ConfigurationError("missing required environment variable FOO")

    monkeypatch.setattr(wombat_main, "load_config", _boom)
    monkeypatch.setattr(sys, "argv", ["wombat", "wipe", "--confirm"])

    with pytest.raises(SystemExit) as exc_info:
        wombat_main.main()
    assert exc_info.value.code != 0

    err = capsys.readouterr().err
    assert "wombat wipe: aborted -" in err
    assert "missing required environment variable FOO" in err
    assert "Traceback" not in err


# ================================================================================================
# AC6 — quiesce refusal (DEC-77 r2).
# ================================================================================================


def test_confirm_refuses_when_singleton_port_is_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    port = _free_port()
    config = _make_config(wombat_singleton_port=port)
    monkeypatch.setattr(wombat_main, "load_config", lambda: config)

    brief = tmp_path / "brief.md"
    brief.write_text("hello", encoding="utf-8")

    live_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    live_socket.bind(("127.0.0.1", port))
    live_socket.listen(1)
    try:
        engine_calls = {"n": 0}
        monkeypatch.setattr(
            wombat_main, "archive_and_wipe", lambda *a, **k: engine_calls.__setitem__("n", 1)
        )
        monkeypatch.setattr(sys, "argv", ["wombat", "wipe", "--confirm"])

        with pytest.raises(SystemExit) as exc_info:
            wombat_main.main()
        assert exc_info.value.code != 0

        err = capsys.readouterr().err
        assert "live" in err.lower()
        assert "stop the runtime" in err.lower()
        assert engine_calls["n"] == 0
        assert brief.read_text(encoding="utf-8") == "hello"
        assert not (tmp_path / "archives").exists()
    finally:
        live_socket.close()


def test_confirm_refuses_when_singleton_port_is_bind_only_held_not_listening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Batch-review repair (round 3, MAJOR — the regression this whole repair exists to pin): a
    real wombat runtime's singleton lock (``wombat.__main__._acquire_singleton_lock``) binds its
    port but NEVER calls ``listen()`` — it is a pure OS-level mutex, not a server socket. This
    test holds the port the SAME way (bind-only, no ``listen()``) rather than the older sibling
    test's ``listen(1)`` shape, which — unlike a real runtime — happens to make a plain TCP
    connect succeed too and so would have passed even under the pre-fix, broken connect-based
    probe. This is the live-proven bug: with two real ``-m wombat`` processes running, port 63218
    refused connect and the OLD probe still returned "not live", proceeding straight toward
    TRUNCATE against a live runtime. The fixed probe must refuse here."""
    monkeypatch.chdir(tmp_path)
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))  # bind-only — deliberately NEVER listen(), mirrors the real lock
    try:
        port = holder.getsockname()[1]
        config = _make_config(wombat_singleton_port=port)
        monkeypatch.setattr(wombat_main, "load_config", lambda: config)

        brief = tmp_path / "brief.md"
        brief.write_text("hello", encoding="utf-8")

        engine_calls = {"n": 0}
        monkeypatch.setattr(
            wombat_main, "archive_and_wipe", lambda *a, **k: engine_calls.__setitem__("n", 1)
        )
        monkeypatch.setattr(sys, "argv", ["wombat", "wipe", "--confirm"])

        with pytest.raises(SystemExit) as exc_info:
            wombat_main.main()
        assert exc_info.value.code != 0

        err = capsys.readouterr().err
        assert "live" in err.lower()
        assert "stop the runtime" in err.lower()
        assert engine_calls["n"] == 0
        assert brief.read_text(encoding="utf-8") == "hello"
        assert not (tmp_path / "archives").exists()
    finally:
        holder.close()


def test_confirm_refuses_when_handshake_port_is_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    live_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    live_socket.bind(("127.0.0.1", 0))
    live_socket.listen(1)
    try:
        handshake_port = live_socket.getsockname()[1]
        handshake_path = tmp_path / "chat-handshake.json"
        handshake_path.write_text(
            json.dumps({"port": handshake_port, "token": "tok"}), encoding="utf-8"
        )
        config = _make_config(wombat_chat_handshake_file=str(handshake_path))
        monkeypatch.setattr(wombat_main, "load_config", lambda: config)

        engine_calls = {"n": 0}
        monkeypatch.setattr(
            wombat_main, "archive_and_wipe", lambda *a, **k: engine_calls.__setitem__("n", 1)
        )
        monkeypatch.setattr(sys, "argv", ["wombat", "wipe", "--confirm"])

        with pytest.raises(SystemExit) as exc_info:
            wombat_main.main()
        assert exc_info.value.code != 0
        assert "live" in capsys.readouterr().err.lower()
        assert engine_calls["n"] == 0
    finally:
        live_socket.close()


def test_stale_handshake_file_is_not_live_and_wipe_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    # A port that is guaranteed free (bound then immediately closed) so it never answers.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    stale_port = probe.getsockname()[1]
    probe.close()

    handshake_path = tmp_path / "chat-handshake.json"
    handshake_path.write_text(
        json.dumps({"port": stale_port, "token": "tok"}), encoding="utf-8"
    )
    config = _make_config(wombat_chat_handshake_file=str(handshake_path))
    monkeypatch.setattr(wombat_main, "load_config", lambda: config)

    calls = {"pg": 0, "fs": 0}
    archive_dir_used = tmp_path / "archives" / "wipe-x"

    class _FakeReport:
        archive_dir = archive_dir_used

    def _fake_pg(dsn: str, archive_dir: Path, **kwargs: object) -> _FakeReport:
        calls["pg"] += 1
        return _FakeReport()

    def _fake_fs(archive_dir: Path, **kwargs: object) -> None:
        calls["fs"] += 1

    monkeypatch.setattr(wombat_main, "check_substrate_guard", lambda: "cold_boot")
    monkeypatch.setattr(wombat_main, "archive_and_wipe", _fake_pg)
    monkeypatch.setattr(wombat_main, "wipe_filesystem_tier", _fake_fs)
    monkeypatch.setattr(sys, "argv", ["wombat", "wipe", "--confirm"])

    with pytest.raises(SystemExit) as exc_info:
        wombat_main.main()
    assert exc_info.value.code == 0
    assert calls == {"pg": 1, "fs": 1}
    assert capsys.readouterr().out.splitlines()[-1] == str(archive_dir_used)


# ================================================================================================
# DEC-77 r1 — argv dispatch: an unrecognized subcommand/flag is usage-to-stderr, exit 2.
# ================================================================================================


def test_unknown_subcommand_prints_usage_and_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["wombat", "bogus"])
    with pytest.raises(SystemExit) as exc_info:
        wombat_main.main()
    assert exc_info.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_unknown_wipe_flag_prints_usage_and_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["wombat", "wipe", "--nope"])
    with pytest.raises(SystemExit) as exc_info:
        wombat_main.main()
    assert exc_info.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()
