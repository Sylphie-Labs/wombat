"""wombat.sources.asr — ASRSource: poll-shaped drop-directory voice capture (TK-162, EP-29,
Q-97).

Q-97 RULING (binding): local voice capture is a WATCHED DROP-DIRECTORY, poll-shaped — the
operator drops a recording into a configured directory (push-to-activate; no wake-word, no
continuous mic listening, non_goal). ``ASRSource`` implements ``sources.base.InputSource``
DIRECTLY rather than riding ``PushSource`` (Q-86): ``PushSource`` is the contract for
GENUINELY push-shaped producers (an in-process caller invoking ``push()``, e.g.
``FeedbackInputSource``/reply-intent emission); a filesystem drop-directory has nothing to
push into this process and is naturally something you POLL. This REFINES Q-86 rather than
violating it — the registry (``sources.registry.SourceRegistry``) is completely untouched.

Each ``poll()`` tick: scan ``drop_dir`` (non-recursively — its own ``processed/``/``failed/``
subdirectories are never rescanned) for audio files (``.wav``/``.m4a``/``.mp3``/``.flac``,
case-insensitive); for each, read its bytes, hash them (sha256 hex) for content-hash event
keying, and transcribe via the INJECTED ``Transcriber``. On success the file moves to
``processed/`` and a ``SourceEvent`` is emitted; on any caught per-file error the file moves to
``failed/``, a WARNING is logged, and the OTHER files in this poll are still processed (one bad
file never kills the source — poller degrade parity with ``CalendarPoller``/``GmailPoller``). An
unexpected SCAN-level error (``drop_dir`` itself missing/unreadable) is caught around the whole
scan, logged loud, and degrades this poll to ``[]``.

The ``SourceEvent`` payload is ``{"transcript": <str>, "captured_at": <aware UTC ISO str>}`` —
user-facing fields ONLY (CON-1). No ``event_class`` key is ever stamped (the Q-41 total fallback
resolves this to ``ItemKind.GENERIC`` downstream — the TK-72 ``CalendarPoller`` precedent).
Content-hash keying (the sha256 event key) means the registry's canonical TK-12
``idempotency_key(source_id="asr", event_key=<sha256>)`` derivation makes a re-drop of
identical bytes dedupe at the queue (``EnqueueResult.ALREADY_QUEUED``) even across a restart —
this class keeps no dedup state of its own.

``Transcriber`` is a minimal Protocol (``transcribe(path: Path) -> str``) so TK-162/TK-163
tests run micless/modelless on a fake. ``FasterWhisperTranscriber`` is the real local backend
(Q-97 ruling a): faster-whisper, LAZY-imported inside its own ``__init__`` so importing this
module — or constructing any OTHER ``Transcriber`` — never requires the ``[voice]`` extra to be
installed; only constructing ``FasterWhisperTranscriber`` itself does. It is constructed ONLY
by ``sources.bootstrap._maybe_register_asr``, never here. Model-weight download is a one-time
install/first-use-time fetch to the local cache — in the DEFAULT configuration (local
``FasterWhisperTranscriber``), no audio or transcript ever leaves the machine (CST-2/ASMP-1
posture, rescoped by DEC-28/TK-218). Opt-in cloud STT providers (``ElevenLabsScribeTranscriber``/
``FishAudioTranscriber``, TK-190) live in ``wombat.voice.stt`` and implement this SAME
``Transcriber`` Protocol; they are constructed ONLY by the structural opt-in seam
``wombat.voice.select.build_transcriber`` (DEC-28) — never here.

TK-212 (EP-34, DEC-35 + DEC-37(f)): an optional ctor kwarg ``command_hook`` gives ``ASRSource``
PRE-QUEUE interception of matched persona voice commands. Evaluated in ``_process_one`` right
after ``transcribe()`` succeeds and BEFORE the ``SourceEvent`` is built: a hook that returns
``True`` means the utterance was CONSUMED — the file still moves to ``processed/`` (transcription
itself succeeded), but NO ``SourceEvent`` is emitted, so a matched command never enqueues, is
never gate-rated, and never reaches a mouth. A hook that is ``None`` or returns ``False`` leaves
this method byte-identical to pre-TK-212 behavior. The hook itself
(``sources.bootstrap.make_persona_command_hook``) is documented to NEVER raise; this module does
not additionally guard the call.

TK-213 (EP-35, DEC-36/DEC-37(h)): an optional ctor kwarg ``feedback_hook`` gives ``ASRSource``
side-channel recording of closed-lexicon persona FEEDBACK (as opposed to a command) — evaluated in
``_process_one`` AFTER the ``command_hook`` check and ONLY when the utterance was NOT consumed by
a command. Unlike ``command_hook``, ``feedback_hook`` NEVER consumes: it takes ``(transcript,
event_key)``, returns nothing, and the ``SourceEvent`` is still built and emitted byte-identically
either way (AC2) — this is a pure side effect, not a branch. The hook itself
(``sources.bootstrap.make_persona_feedback_hook``) is documented to NEVER raise (the factory
guards the call); this module adds no try/except of its own, mirroring the TK-212 pattern.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from wombat.sources.base import SourceEvent

logger = logging.getLogger(__name__)

# Recognized audio file suffixes (Q-97 ruling b), matched case-insensitively.
_AUDIO_SUFFIXES = frozenset({".wav", ".m4a", ".mp3", ".flac"})

_PROCESSED_DIRNAME = "processed"
_FAILED_DIRNAME = "failed"


def _utc_now() -> datetime:
    """The real-clock default for ``ASRSource``'s injected ``clock``."""
    return datetime.now(UTC)


class Transcriber(Protocol):
    """The one method ``ASRSource`` needs from a speech-to-text backend. Production injects
    ``FasterWhisperTranscriber``; tests inject a bare fake, so TK-162/TK-163 tests run
    micless/modelless."""

    def transcribe(self, path: Path) -> str:
        """Return the transcript text for the audio file at ``path``. May raise on failure —
        ``ASRSource._process_one`` catches it as a per-file error (moves to ``failed/``)."""
        ...


class FasterWhisperTranscriber:
    """The real local ASR backend (Q-97 ruling a) — faster-whisper (local CTranslate2 Whisper
    inference), LAZY-imported inside ``__init__`` so a checkout without the ``[voice]`` extra
    still imports this module and boots clean (``sources.bootstrap._maybe_register_asr``'s
    ``ImportError`` loud-skip handles the absent-extra case; construction is the ONLY place
    that import can fail)."""

    __slots__ = ("_model",)

    def __init__(self, model_name: str) -> None:
        from faster_whisper import WhisperModel  # lazy import (Q-97 ruling a) — [voice] extra

        self._model = WhisperModel(model_name)

    def transcribe(self, path: Path) -> str:
        """Transcribe the audio file at ``path``, joining every decoded segment's text."""
        segments, _info = self._model.transcribe(str(path))
        return " ".join(segment.text.strip() for segment in segments).strip()


class ASRSource:
    """Watches a drop-directory for audio files and transcribes them locally (Q-97 ruling b).
    Implements ``sources.base.InputSource`` directly — poll-shaped, not push-shaped (see the
    module docstring for the Q-86-refining rationale). See the module docstring for the full
    per-poll scan/transcribe/move/degrade design.
    """

    id: str = "asr"

    def __init__(
        self,
        *,
        drop_dir: Path,
        transcriber: Transcriber,
        poll_interval_seconds: float,
        clock: Callable[[], datetime] = _utc_now,
        command_hook: Callable[[str], bool] | None = None,
        feedback_hook: Callable[[str, str], None] | None = None,
    ) -> None:
        self.poll_interval_seconds = poll_interval_seconds
        self._drop_dir = drop_dir
        self._transcriber = transcriber
        self._clock = clock
        self._command_hook = command_hook
        self._feedback_hook = feedback_hook

    async def start(self) -> None:
        """No lifecycle setup needed — the injected transcriber is already constructed."""
        return None

    async def stop(self) -> None:
        """No lifecycle teardown needed."""
        return None

    async def poll(self) -> list[SourceEvent]:
        """Scan, transcribe, and move every new audio file in ``drop_dir`` this tick.

        NEVER raises: an unexpected SCAN-level error (``drop_dir`` missing/unreadable) is
        logged loud and degrades this poll to ``[]``; a per-file error is caught inside
        ``_process_one``, logged, and the file moved to ``failed/`` — it never stops the other
        files in this same poll from being processed.
        """
        try:
            candidates = sorted(
                path
                for path in self._drop_dir.iterdir()
                if path.is_file() and path.suffix.lower() in _AUDIO_SUFFIXES
            )
        except OSError:
            logger.warning(
                "asr source %r: failed to scan drop directory %s — degrading this poll to "
                "no events",
                self.id,
                self._drop_dir,
                exc_info=True,
            )
            return []

        events: list[SourceEvent] = []
        for path in candidates:
            event = self._process_one(path)
            if event is not None:
                events.append(event)
        return events

    def _process_one(self, path: Path) -> SourceEvent | None:
        """Transcribe and move a single file. Returns its ``SourceEvent`` on success; ``None``
        on any caught transcription error (already logged and moved to ``failed/``) OR when
        ``command_hook`` consumes the transcript as a matched persona command (TK-212) — that
        path ALSO moves the file to ``processed/`` (transcription succeeded; the utterance was
        just handled here instead of enqueued). When the utterance was NOT consumed,
        ``feedback_hook`` (TK-213) runs as a pure side effect before the ``SourceEvent`` is built
        — it never changes what is returned. Never raises — a bad file must never kill this
        poll or another file's processing (AC4)."""
        try:
            raw = path.read_bytes()
            event_key = hashlib.sha256(raw).hexdigest()
            transcript = self._transcriber.transcribe(path)
        except Exception:
            logger.warning(
                "asr source %r: failed to transcribe %s — moving to %s/",
                self.id,
                path,
                _FAILED_DIRNAME,
                exc_info=True,
            )
            self._safe_move(path, _FAILED_DIRNAME)
            return None

        if self._command_hook is not None and self._command_hook(transcript):
            # TK-212 (DEC-35, EP-34): a matched persona command is CONSUMED pre-queue — it never
            # becomes a SourceEvent, so it never enqueues, is never gate-rated, and never reaches
            # a mouth. The file still moves to processed/ since it was successfully handled.
            self._safe_move(path, _PROCESSED_DIRNAME)
            return None

        if self._feedback_hook is not None:
            # TK-213: a pure side effect — never consumes, never changes the SourceEvent below.
            self._feedback_hook(transcript, event_key)

        self._safe_move(path, _PROCESSED_DIRNAME)
        payload = {"transcript": transcript, "captured_at": self._clock().isoformat()}
        return SourceEvent(event_key=event_key, payload=payload)

    def _safe_move(self, path: Path, dest_dirname: str) -> None:
        """Best-effort move of ``path`` into ``drop_dir/<dest_dirname>/`` — a failure here is
        logged and swallowed (never raised), so a move failure can never itself kill the poll
        loop. The file is left in place on failure (it may be re-processed next poll)."""
        try:
            dest_dir = self._drop_dir / dest_dirname
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dest_dir / path.name))
        except OSError:
            logger.warning(
                "asr source %r: failed to move %s into %s/ — leaving it in place",
                self.id,
                path,
                dest_dirname,
                exc_info=True,
            )


__all__ = ["ASRSource", "FasterWhisperTranscriber", "Transcriber"]
