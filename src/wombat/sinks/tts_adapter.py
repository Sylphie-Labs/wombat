"""TTSAdapter — the swappable local-TTS seam SpeakSink depends on (TK-164, Q-96).

``TTSAdapter`` is a structural ``Protocol`` (one method, ``speak(text) -> None``) so
``SpeakSink``/``bootstrap.make_speak_callable`` never depend on a concrete TTS library type —
only on "something that can speak a string" (tests inject a recording/raising fake).

``Pyttsx3Adapter`` is the LOCAL DEFAULT concrete adapter (Q-96 ruling, rescoped by DEC-28/TK-218):
``pyttsx3`` (offline, Windows SAPI5 backend — CST-2/TECH-11 local-only speech). In the DEFAULT
configuration no cloud TTS exists. Cloud adapters (``FishAudioTTSAdapter``/``ElevenLabsTTSAdapter``/
``DeepgramAuraTTSAdapter``, TK-191/TK-192) live in ``wombat.voice.tts`` and implement this SAME
``TTSAdapter`` Protocol; they are constructed ONLY by the structural opt-in seam
``wombat.voice.select.build_tts_adapter`` — a cloud provider is selected AND a user-supplied key
resolves, else no cloud instance is ever constructed (DEC-28); degrade is STRICTLY cloud-to-local,
never the reverse.

SUBPROCESS ISOLATION (TK-265, DEC-54): a SAPI/comtypes native hard-abort inside an in-process
``pyttsx3`` engine can kill the whole runtime with zero record (ISS-18) — it evades even the
last-gasp handler. So ``speak()`` never runs ``pyttsx3`` in this process: it spawns a short-lived
child (``python -m wombat.sinks._local_speak_child``) that does, and only ever talks to it via
stdin text + an exit code. A child crash/hang becomes an ordinary Python exception here, which
flows into ``SpeakSink``'s existing adapter-exception containment (terminal ``Degraded``) exactly
like any other adapter failure. ``__init__`` no longer constructs an engine; it merely PROBES
that ``pyttsx3`` is importable (``importlib.util.find_spec``, no import) and raises an
``ImportError`` when the optional ``voice`` extra is absent — byte-compatible with the bootstrap
catch-and-voice-off contract (AC4). The module top level stays free of ``pyttsx3`` imports so
merely importing ``wombat.sinks.tts_adapter`` never fails on a checkout that has not installed the
optional ``voice`` extra (Q-46/Q-72 clean-checkout bar; ``pyproject.toml``'s ``[project.optional-
dependencies] voice`` group, never a core dependency).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from typing import Protocol

#: Wall-clock budget for the child helper to speak one utterance and exit (TK-265). A named code
#: constant, not configurable — this ticket adds no config surface.
_SPEAK_TIMEOUT_SECONDS = 30.0


class TTSAdapter(Protocol):
    """Something that can speak a string aloud, once, synchronously."""

    def speak(self, text: str) -> None: ...


class Pyttsx3Adapter:
    """Local, offline TTS via ``pyttsx3`` (SAPI5 on Windows) — the ONE concrete ``TTSAdapter``
    (Q-96). ``speak()`` runs the engine in a short-lived subprocess (TK-265, DEC-54) so a native
    engine crash can never take down this process — it surfaces here as an ordinary exception."""

    def __init__(self) -> None:
        # Loud availability probe, no engine construction (TK-265): importing this module must
        # stay clean on a checkout without the optional ``voice`` extra (Q-46/Q-72), but
        # constructing this adapter must still fail exactly as before when ``pyttsx3`` is absent.
        if importlib.util.find_spec("pyttsx3") is None:
            msg = "No module named 'pyttsx3'"
            raise ImportError(msg)

    def speak(self, text: str) -> None:
        """Speak ``text`` verbatim via a short-lived child process, blocking until it exits.

        Raises ``RuntimeError`` naming the failure class when the child exits nonzero, times out
        (the child is killed — no orphan), or fails to spawn at all."""
        try:
            result = subprocess.run(  # fixed argv, no shell, text via stdin only
                [sys.executable, "-m", "wombat.sinks._local_speak_child"],
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=_SPEAK_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            # subprocess.run kills the child (and, on POSIX, its process group is not implied —
            # but this is a single leaf process with no children of its own) before raising.
            msg = f"Pyttsx3Adapter: local TTS child timed out after {_SPEAK_TIMEOUT_SECONDS}s"
            raise RuntimeError(msg) from exc
        except OSError as exc:
            msg = f"Pyttsx3Adapter: local TTS child failed to spawn: {exc}"
            raise RuntimeError(msg) from exc

        if result.returncode != 0:
            msg = f"Pyttsx3Adapter: local TTS child exited with code {result.returncode}"
            raise RuntimeError(msg)


__all__ = ["Pyttsx3Adapter", "TTSAdapter"]
