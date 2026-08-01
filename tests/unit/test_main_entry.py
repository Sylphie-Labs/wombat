"""tests/unit/test_main_entry.py — TK-259 (DEC-52a/DEC-53b): runtime-owned per-boot file
logging, faulthandler-into-the-log, and the last-gasp fatal record, all with ``serve()``
monkeypatched and a tmp cwd — NEVER a live boot.

  AC1 healthy boot (serve returns immediately) -> logs/runtime-<ts>.log exists with a boot
      banner + honest shutdown line; root logger carries exactly ONE FileHandler; importing
      wombat/wombat.settings_app adds zero handlers.
  AC2 serve raises RuntimeError -> nonzero exit, log ends CRITICAL with full traceback + the
      honest shutdown line.
  AC3 serve raises KeyboardInterrupt -> honest shutdown line, no traceback spew.
  AC4 wombat-console.ps1 no longer pipes through Tee-Object (static/script inspection).
  AC5 after logging setup, faulthandler.is_enabled() is true and its target is the per-boot log
      file's own open stream.

TK-261 (DEC-52e) additions — the singleton loopback-port guard, all with an ephemeral
``WOMBAT_SINGLETON_PORT`` override (never the production default 63218):
  AC6 a first main() boot holds the singleton bind (serve monkeypatched to block); a second
      main() with the same config exits nonzero fast with exactly one loud ERROR naming the
      port; the first instance is unaffected and completes cleanly once released.
  AC7 after the first instance "dies" (raw socket closed, no cleanup code involved — hard-kill
      semantics), a new boot's bind succeeds immediately.
  AC8 grep-level proof: no stale-lock file / cleanup branch anywhere in __main__.py.
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import re
import socket
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from wombat import __main__ as wombat_main


@pytest.fixture(autouse=True)
def _bare_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """TK-335 (DEC-77 r1): main() now parses ``sys.argv`` as its first act. Every test in this
    module exercises the pre-existing bare-boot path, so pin argv to just the program name —
    regardless of whatever pytest itself was invoked with — the same way TK-335's own argv-aware
    tests pin theirs (tests/unit/test_wipe_cli.py)."""
    monkeypatch.setattr(sys, "argv", ["wombat"])


@pytest.fixture(autouse=True)
def _clean_root_logger() -> Iterator[None]:
    """Root logger + faulthandler state is process-global; isolate each test so TK-259's own
    handler/faulthandler setup in one test can never leak into another."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    was_enabled = faulthandler.is_enabled()
    root.handlers.clear()
    try:
        yield
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers.clear()
        root.handlers.extend(saved_handlers)
        root.setLevel(saved_level)
        if not was_enabled:
            faulthandler.disable()


def _free_port() -> int:
    """An ephemeral, currently-unused loopback port — never the production default 63218."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@pytest.fixture(autouse=True)
def _deepseek_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """TK-261: main() now calls load_config() before serve() (to read the singleton port), so
    every test needs the two REQUIRED_ENV vars satisfied hermetically."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")


@pytest.fixture(autouse=True)
def _singleton_port_env(monkeypatch: pytest.MonkeyPatch) -> int:
    """Every test gets its own fresh ephemeral singleton port, so main() calls across different
    tests (and within test AC6/AC7 themselves) never collide on the production default."""
    port = _free_port()
    monkeypatch.setenv("WOMBAT_SINGLETON_PORT", str(port))
    return port


@pytest.fixture(autouse=True)
def _release_singleton_socket() -> Iterator[None]:
    """Safety net: if a test's main() call successfully binds the singleton socket, close it
    afterward so the module-global doesn't leak a held port into a later, unrelated test. This is
    test-hygiene only — production __main__.py has no such release path (TK-261 AC2/AC8)."""
    yield
    if wombat_main._singleton_socket is not None:
        wombat_main._singleton_socket.close()
        wombat_main._singleton_socket = None


def _log_files(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "logs").glob("runtime-*.log"))


def test_healthy_boot_writes_banner_and_shutdown_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    async def _fake_serve() -> None:
        return None

    monkeypatch.setattr(wombat_main, "serve", _fake_serve)

    wombat_main.main()

    files = _log_files(tmp_path)
    assert len(files) == 1
    assert re.fullmatch(r"runtime-\d{8}-\d{6}\.log", files[0].name)
    content = files[0].read_text(encoding="utf-8")
    assert "boot" in content.lower()
    assert "shutting down" in content.lower()

    file_handlers = [
        h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)
    ]
    assert len(file_handlers) == 1


def test_importing_wombat_and_settings_app_adds_zero_handlers() -> None:
    import importlib

    import wombat
    import wombat.settings_app

    before = list(logging.getLogger().handlers)
    importlib.reload(wombat)
    importlib.reload(wombat.settings_app)

    assert logging.getLogger().handlers == before


def test_serve_runtime_error_exits_nonzero_with_critical_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    async def _fake_serve() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(wombat_main, "serve", _fake_serve)

    with pytest.raises(SystemExit) as exc_info:
        wombat_main.main()
    assert exc_info.value.code != 0

    files = _log_files(tmp_path)
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "CRITICAL" in content
    assert "RuntimeError: boom" in content
    assert "Traceback" in content
    assert "shutting down" in content.lower()
    # the shutdown line is the honest last word, after the traceback record.
    assert content.rindex("shutting down") > content.rindex("Traceback")


def test_serve_keyboard_interrupt_no_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    async def _fake_serve() -> None:
        raise KeyboardInterrupt()

    monkeypatch.setattr(wombat_main, "serve", _fake_serve)

    wombat_main.main()

    files = _log_files(tmp_path)
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "shutting down" in content.lower()
    assert "Traceback" not in content
    assert "CRITICAL" not in content


def test_faulthandler_enabled_targeting_the_log_file_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    async def _fake_serve() -> None:
        assert faulthandler.is_enabled()
        return None

    monkeypatch.setattr(wombat_main, "serve", _fake_serve)

    wombat_main.main()

    file_handlers = [
        h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)
    ]
    assert len(file_handlers) == 1
    stream = file_handlers[0].stream
    assert stream is not None
    # the FileHandler's own stream is the faulthandler target (DEC-53b) - proven by writing
    # through faulthandler.dump_traceback and finding the output landed in the same file.
    faulthandler.dump_traceback(file=stream)
    stream.flush()
    files = _log_files(tmp_path)
    content = files[0].read_text(encoding="utf-8")
    assert "Current thread" in content


_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_console_script_drops_tee_object() -> None:
    script = (_REPO_ROOT / "scripts" / "wombat-console.ps1").read_text(encoding="utf-8")
    # the piping itself is gone (docstring prose may still reference Tee-Object by name for
    # context on why it was removed - that is not custody).
    assert "| Tee-Object" not in script
    assert "-m wombat 2>&1" not in script
    # the single-instance guard and the post-start root-match assert are untouched.
    assert "Get-WombatRootProcesses" in script
    assert "exit 0" in script


# --- TK-261 (DEC-52e): the runtime-owned singleton loopback-port guard ------------------------


def test_singleton_lock_blocks_concurrent_second_instance_and_first_is_unaffected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    _singleton_port_env: int,
) -> None:
    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.INFO)
    port = _singleton_port_env
    first_ready = threading.Event()
    release_first = threading.Event()

    async def _blocking_serve() -> None:
        first_ready.set()
        while not release_first.is_set():
            await asyncio.sleep(0.01)

    monkeypatch.setattr(wombat_main, "serve", _blocking_serve)

    first_thread = threading.Thread(target=wombat_main.main)
    first_thread.start()
    try:
        assert first_ready.wait(timeout=5)

        # A second boot, same config/port, while the first instance still holds the lock.
        with pytest.raises(SystemExit) as exc_info:
            wombat_main.main()
        assert exc_info.value.code != 0
    finally:
        release_first.set()
        first_thread.join(timeout=5)
    assert not first_thread.is_alive()

    # caplog captures each logged record exactly once regardless of how many FileHandlers the
    # (shared, process-wide) root logger accumulated across the two in-process boots.
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    message = error_records[0].getMessage()
    assert str(port) in message
    assert "already running" in message.lower()
    assert "wombat_singleton_port" in message
    assert not any(r.levelno == logging.CRITICAL for r in caplog.records)
    # the first instance ran to a clean, unaffected completion once released.
    assert any("shutting down (serve returned)" in r.getMessage() for r in caplog.records)


def test_singleton_lock_available_immediately_after_first_instance_dies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _singleton_port_env: int
) -> None:
    monkeypatch.chdir(tmp_path)
    port = _singleton_port_env

    # Simulate a prior instance's hard-kill: bind then close with zero cleanup logic involved
    # (an abrupt process death releases the OS-level bind the exact same way).
    prior = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    prior.bind(("127.0.0.1", port))
    prior.close()

    async def _fake_serve() -> None:
        return None

    monkeypatch.setattr(wombat_main, "serve", _fake_serve)

    wombat_main.main()  # must succeed immediately - no stale lock left behind

    files = _log_files(tmp_path)
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "ERROR" not in content
    assert "shutting down (serve returned)" in content


def test_no_stale_lock_cleanup_path_in_source() -> None:
    """AC2/AC8: grep-level proof there is no pidfile/lockfile artifact and no code path that
    explicitly releases the successfully-acquired singleton socket — the OS is the only thing
    that ever releases it, on any process death including a hard kill."""
    source = (_REPO_ROOT / "src" / "wombat" / "__main__.py").read_text(encoding="utf-8")
    assert "lockfile" not in source.lower()
    assert "pidfile" not in source.lower()
    assert "atexit" not in source
    assert "signal.signal" not in source
    assert "_singleton_socket.close" not in source
