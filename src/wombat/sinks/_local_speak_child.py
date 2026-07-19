"""Local TTS child helper — the subprocess-isolated worker ``Pyttsx3Adapter.speak()`` spawns
(TK-265, DEC-54).

Runs as ``python -m wombat.sinks._local_speak_child``: reads the utterance text from stdin
(UTF-8, NEVER argv — text can be arbitrarily long/shaped and argv is a poor wire for that),
imports ``pyttsx3`` fresh, constructs a FRESH engine, speaks the text once, and exits 0. ANY
failure (import, engine construction, or the speak call itself) exits nonzero with NO traceback
spew — the exit code IS the wire back to the parent; the parent never parses this process's
stderr/stdout as part of its contract surface.

This isolation exists because a SAPI/comtypes native hard-abort inside ``pyttsx3`` can kill the
whole process with zero record (ISS-18) — running it here means that abort only takes down this
short-lived child, and the parent sees an ordinary nonzero exit (or a timeout it can kill), never
a silent runtime death.
"""

from __future__ import annotations

import sys


def main() -> int:
    """Speak the UTF-8 text on stdin with a fresh ``pyttsx3`` engine. Returns the process exit
    code: 0 on success, 1 on any failure."""
    try:
        text = sys.stdin.buffer.read().decode("utf-8")

        import pyttsx3  # lazy: only imported inside this short-lived child process

        engine = pyttsx3.init()
        engine.say(text)
        engine.runAndWait()
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
