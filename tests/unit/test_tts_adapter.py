"""TK-164 — TTSAdapter/Pyttsx3Adapter acceptance criteria (Q-96).

Proves the lazy-import contract directly: importing this module (and ``wombat.sinks.tts_adapter``)
never touches ``pyttsx3`` — only *constructing* ``Pyttsx3Adapter`` does. This suite runs with
``pyttsx3`` genuinely NOT installed (it rides the optional ``voice`` extra, never a core dep —
Q-46/Q-72), so the construction-failure assertion below is a real, unmocked proof, not a simulation.
"""

from __future__ import annotations

import importlib
import sys

import pytest

from wombat.sinks.tts_adapter import Pyttsx3Adapter, TTSAdapter


def test_module_imports_cleanly_without_pyttsx3_installed() -> None:
    """AC4 (lesion): the module already imported above (top of file) without raising — this test
    just makes the claim explicit and re-imports for good measure."""
    assert "pyttsx3" not in sys.modules
    importlib.reload(importlib.import_module("wombat.sinks.tts_adapter"))
    assert "pyttsx3" not in sys.modules  # a bare re-import never touches pyttsx3 either


def test_pyttsx3_adapter_construction_raises_when_pyttsx3_is_not_installed() -> None:
    """The real, unmocked lazy-import-failure path: no ``voice`` extra installed -> construction
    raises (never silently no-ops) so callers (bootstrap's build_speak_sink/make_speak_callable)
    can catch it and degrade loud."""
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
