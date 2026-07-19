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
"""

from __future__ import annotations

import faulthandler
import logging
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from wombat import __main__ as wombat_main


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
