"""TK-164 — TTSAdapter/Pyttsx3Adapter acceptance criteria (Q-96).

Proves the lazy-import contract directly: importing this module (and ``wombat.sinks.tts_adapter``)
never touches ``pyttsx3`` — only *constructing* ``Pyttsx3Adapter`` does. ``pyttsx3`` rides the
optional ``voice`` extra (Q-46/Q-72), never a core dep — but a dev/operator checkout MAY have it
installed anyway (Q-103), so the absence these tests need is SIMULATED via ``_simulate_absent``
(TK-202) rather than assumed from the environment: a real, unmocked import-failure path either way.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Sequence
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from types import ModuleType

import pytest

from wombat.sinks.tts_adapter import Pyttsx3Adapter, TTSAdapter


class _BlockedFinder(MetaPathFinder):
    """A meta-path finder that fails the import of one named module (and its submodules)."""

    def __init__(self, blocked: str) -> None:
        self._blocked = blocked

    def find_spec(
        self, fullname: str, path: Sequence[str] | None, target: ModuleType | None = None
    ) -> ModuleSpec | None:
        if fullname == self._blocked or fullname.startswith(f"{self._blocked}."):
            raise ModuleNotFoundError(f"No module named {fullname!r} (simulated absence, TK-202)")
        return None


def _simulate_absent(monkeypatch: pytest.MonkeyPatch, module_name: str) -> None:
    """Simulate ``module_name`` being genuinely not installed, regardless of whether it actually
    is on this machine (TK-202/Q-103): evict any cached import (a prior test in this session may
    have already imported it) AND install a meta-path finder ahead of the real one so any
    subsequent ``import``/``from ... import`` raises ``ModuleNotFoundError`` — robust to the
    module actually being present."""
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockedFinder(module_name), *sys.meta_path])


def test_module_imports_cleanly_without_pyttsx3_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC4 (lesion): the module already imported above (top of file) without raising — this test
    just makes the claim explicit and re-imports for good measure."""
    _simulate_absent(monkeypatch, "pyttsx3")
    assert "pyttsx3" not in sys.modules
    importlib.reload(importlib.import_module("wombat.sinks.tts_adapter"))
    assert "pyttsx3" not in sys.modules  # a bare re-import never touches pyttsx3 either


def test_pyttsx3_adapter_construction_raises_when_pyttsx3_is_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real, unmocked lazy-import-failure path: no ``voice`` extra installed -> construction
    raises (never silently no-ops) so callers (bootstrap's build_speak_sink/make_speak_callable)
    can catch it and degrade loud."""
    _simulate_absent(monkeypatch, "pyttsx3")
    with pytest.raises(ModuleNotFoundError):
        Pyttsx3Adapter()


class _Stub:
    """Any object with a matching ``speak(str) -> None`` method satisfies ``TTSAdapter``
    structurally — no inheritance required."""

    def speak(self, text: str) -> None:
        pass


def test_pyttsx3_adapter_satisfies_the_tts_adapter_protocol_structurally() -> None:
    assert hasattr(Pyttsx3Adapter, "speak")
    stub: TTSAdapter = _Stub()
    stub.speak("hi")
