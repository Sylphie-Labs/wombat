"""TTSAdapter — the swappable local-TTS seam SpeakSink depends on (TK-164, Q-96).

``TTSAdapter`` is a structural ``Protocol`` (one method, ``speak(text) -> None``) so
``SpeakSink``/``bootstrap.make_speak_callable`` never depend on a concrete TTS library type —
only on "something that can speak a string" (tests inject a recording/raising fake).

``Pyttsx3Adapter`` is the ONE concrete adapter (Q-96 ruling): ``pyttsx3`` (offline, Windows SAPI5
backend — CST-2/TECH-11 local-only speech, no cloud TTS). ``import pyttsx3`` happens LAZILY inside
``__init__`` — NEVER at this module's top level — so merely importing
``wombat.sinks.tts_adapter`` never fails on a checkout that has not installed the optional
``voice`` extra (Q-46/Q-72 clean-checkout bar; ``pyproject.toml``'s ``[project.optional-
dependencies] voice`` group, never a core dependency). Construction propagates whatever
``pyttsx3``/the OS TTS engine raises (``ImportError`` when the lib is absent, or any engine-init
failure) so callers (``bootstrap.build_speak_sink`` / ``bootstrap.make_speak_callable``) can catch
it and degrade to voice-off, logging loud rather than blocking boot (AC4).
"""

from __future__ import annotations

from typing import Protocol


class TTSAdapter(Protocol):
    """Something that can speak a string aloud, once, synchronously."""

    def speak(self, text: str) -> None: ...


class Pyttsx3Adapter:
    """Local, offline TTS via ``pyttsx3`` (SAPI5 on Windows) — the ONE concrete ``TTSAdapter``
    (Q-96). Lazily imports+initializes the engine at construction time; ``speak()`` is a thin,
    blocking wrapper over ``say()``/``runAndWait()``."""

    def __init__(self) -> None:
        import pyttsx3  # lazy: only imported when actually constructing this adapter (Q-46/Q-72)

        self._engine = pyttsx3.init()

    def speak(self, text: str) -> None:
        """Speak ``text`` verbatim, blocking until the utterance finishes."""
        self._engine.say(text)
        self._engine.runAndWait()


__all__ = ["Pyttsx3Adapter", "TTSAdapter"]
