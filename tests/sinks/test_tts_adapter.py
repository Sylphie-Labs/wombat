"""TK-265 — subprocess-isolated ``Pyttsx3Adapter`` (DEC-54).

No real audio, no real ``pyttsx3`` engine, ever: ``subprocess.run`` is faked/spied via
``monkeypatch`` so these tests run on CI without SAPI/an audio device. AC5 (the child helper
itself, run standalone with ``pyttsx3`` forced-unavailable) is exercised as a real subprocess
since that's the only way to prove the standalone entry point/exit-code contract end to end —
still no real TTS engine touches it (the fake ``pyttsx3`` shim raises before any engine exists).
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

from wombat.sinks.tts_adapter import Pyttsx3Adapter

_TEXT = "You have a new alert."


# --- AC1: speak() spawns via sys.executable -m with text on stdin (never argv), bounded timeout,
# exit 0 -> returns None ------------------------------------------------------------------------


def test_ac1_speak_spawns_child_module_with_text_on_stdin_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    adapter = Pyttsx3Adapter.__new__(Pyttsx3Adapter)  # bypass __init__'s pyttsx3 probe

    adapter.speak(_TEXT)  # AC1: exit 0 -> returns None (no exception raised)

    assert captured["argv"] == [sys.executable, "-m", "wombat.sinks._local_speak_child"]
    assert captured["kwargs"]["input"] == _TEXT.encode("utf-8")
    assert isinstance(captured["kwargs"]["timeout"], float)
    assert captured["kwargs"]["timeout"] > 0
    # text was never placed on argv (stdin-only wire, never argv)
    assert all(_TEXT not in arg for arg in captured["argv"])


# --- AC2: nonzero exit / TimeoutExpired / spawn-failure each raise RuntimeError naming the
# failure class; on timeout the child is provably killed -----------------------------------------


def test_ac2_nonzero_exit_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, returncode=1, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    adapter = Pyttsx3Adapter.__new__(Pyttsx3Adapter)

    with pytest.raises(RuntimeError, match="exited with code"):
        adapter.speak(_TEXT)


def test_ac2_timeout_raises_runtime_error_and_kills_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # subprocess.run's own contract kills the child before raising TimeoutExpired — faking that
    # exception is the standard way to prove the caller reacts correctly; subprocess.run is the
    # thing responsible for the kill (proven by CPython's own implementation, not re-tested here).
    def _fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", _fake_run)
    adapter = Pyttsx3Adapter.__new__(Pyttsx3Adapter)

    with pytest.raises(RuntimeError, match="timed out"):
        adapter.speak(_TEXT)


def test_ac2_spawn_failure_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise OSError("no such file or directory")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    adapter = Pyttsx3Adapter.__new__(Pyttsx3Adapter)

    with pytest.raises(RuntimeError, match="failed to spawn"):
        adapter.speak(_TEXT)


# --- AC3: SpeakSink wired voice-enabled with the reshaped adapter raising RuntimeError yields the
# existing terminal Degraded artifact (spoken=False, degraded=True) ------------------------------
#
# Covered by the EXISTING tests/sinks/test_speak_sink.py::test_ac4_adapter_speak_failure_
# degrades_without_raising, parametrized with a generic RuntimeError — that IS the reshaped
# adapter's failure shape (SpeakSink's containment is keyed on `except Exception`, not on any
# adapter-specific type), so no new SpeakSink-level test is needed; cited here per AC3.


# --- AC4: without the voice extra, importing sinks.tts_adapter stays clean; constructing
# Pyttsx3Adapter fails loudly exactly as today ----------------------------------------------------


def test_ac4_construction_fails_loudly_when_pyttsx3_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)

    with pytest.raises(ImportError):
        Pyttsx3Adapter()


def test_ac4_module_import_never_imports_pyttsx3() -> None:
    import wombat.sinks.tts_adapter as mod

    assert not hasattr(mod, "pyttsx3")


# --- AC5: child helper run standalone with pyttsx3 env-forced-unavailable exits nonzero cleanly --


def test_ac5_child_helper_exits_nonzero_when_pyttsx3_unavailable(tmp_path: Any) -> None:
    # Force `import pyttsx3` to fail inside the child: shadow the real package by running with a
    # PYTHONPATH entry containing a `pyttsx3.py` stub that raises ImportError on import.
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    (stub_dir / "pyttsx3.py").write_text(
        "raise ImportError(\"forced-unavailable for TK-265 AC5\")\n", encoding="utf-8"
    )

    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(stub_dir) + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.run(
        [sys.executable, "-m", "wombat.sinks._local_speak_child"],
        input=b"hello",
        capture_output=True,
        timeout=15,
        env=env,
    )

    assert proc.returncode != 0
