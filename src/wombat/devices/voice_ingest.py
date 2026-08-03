"""wombat.devices.voice_ingest — VoiceIngestHandler: ``POST /v1/voice`` audio ingest (TK-340,
DEC-78(c) route 2, ``planning/design/wire-contract.md`` §2).

The route writes the raw audio body, byte-identically, into a REMOTE-origin drop directory that a
SECOND ``ASRSource`` (``sources.bootstrap.RemoteASRSource``, id ``"asr_remote"``) already watches
(R2) — zero new transcription machinery. This module owns exactly the wire-level validation §2
pins: the required ``X-Wombat-Captured-At`` staleness refusal, the 10 MiB body cap, and a
magic-byte audio sniff (extension/``Content-Type`` alone is never trusted). It never touches
``sources/asr.py`` (byte-untouched) and never talks to ``DeviceSurface`` directly — ``handle()``
is a pure ``(device_id, headers, body) -> (status, json_body)`` seam that
``DeviceSurface._dispatch`` (R1) calls and forwards to its own ``_respond``.

``max_body_bytes`` is deliberately a PUBLIC attribute (not private): ``DeviceSurface._handle_
connection`` reads it, for the ONE route wired to this handler, to decide how many bytes to
``readexactly`` off the wire *before* dispatch — the same "never buffer an over-cap body in full"
posture ``_MAX_BODY_BYTES`` already gives every other route (§0's size-cap rule), just at this
route's OWN larger, pinned cap rather than the surface's generic 1 MiB default.

Idempotency (§2): the event key ``RemoteASRSource``/``ASRSource`` derive is the sha256 of the
audio bytes — this module mints ``utterance_id`` server-side at accept and keeps an in-process
``{sha256 -> utterance_id}`` map (process-lifetime; TK-340 does not need persistence across a
restart) so a duplicate POST of identical bytes returns the SAME ``utterance_id`` and — since the
write is skipped entirely on a repeat — never reintroduces a second copy of the file into the
watched directory, regardless of whether a poll already moved the first copy to ``processed/``.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from wombat.devices.surface import STALE_AUDIO_WINDOW_SECONDS
from wombat.sources.asr import _AUDIO_SUFFIXES

logger = logging.getLogger(__name__)

# §2: this route's OWN pinned body cap — distinct from and larger than DeviceSurface's generic
# _MAX_BODY_BYTES (1 MiB, sized for the bodyless GET /v1/health).
MAX_VOICE_BODY_BYTES = 10 * 1024 * 1024  # 10 MiB

_CONTENT_TYPE = "audio/wav"
_WRITE_SUFFIX = ".wav"
assert _WRITE_SUFFIX in _AUDIO_SUFFIXES  # never diverge from the set ASRSource itself scans for

_CAPTURED_AT_HEADER = "x-wombat-captured-at"

_ERROR_BAD_REQUEST: dict[str, object] = {"v": 1, "error": "bad_request"}


def _utc_now() -> datetime:
    """The real-clock default for ``VoiceIngestHandler``'s injected ``clock``."""
    return datetime.now(UTC)


def _looks_like_wav(data: bytes) -> bool:
    """Magic-byte sniff (§2: "extension alone is never trusted") — a real WAV file's first 12
    bytes are the ASCII ``RIFF`` chunk id, a 4-byte size field, then the ASCII ``WAVE`` format
    id. This is the ONE audio-ness check this route relies on; a claimed ``Content-Type`` that
    doesn't match reality never gets past it."""
    return len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WAVE"


def _parse_declared_length(raw: str | None) -> int:
    """The ORIGINAL ``Content-Length`` header value, parsed defensively — mirrors ``DeviceSurface.
    _handle_connection``'s own ``int(...) or 0`` fallback. Read from ``headers`` (never from
    ``len(body)``): ``DeviceSurface`` clamps the actual read to ``max_body_bytes``, so an
    over-cap declared length is what this function must catch even when the bytes that reached
    this handler were already truncated to the cap (the same "never hangs, never over-reads"
    proof ``test_oversized_body_is_capped_and_never_hangs_the_response`` establishes for
    ``/v1/health``)."""
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _parse_captured_at(raw: str | None) -> datetime | None:
    """``X-Wombat-Captured-At`` (§2): required, ISO-8601 with an explicit offset or ``Z``.
    ``None`` on missing, unparseable, or naive (no ``tzinfo``) — every one of those is a 400."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


class VoiceIngestHandler:
    """``POST /v1/voice`` (TK-340, R1): constructed at the composition root ONLY when
    ``config.wombat_device_remote_drop_dir`` is non-blank, then injected into ``DeviceSurface``
    as its optional ``voice_ingest_handler`` kwarg — a ``None`` handler leaves that route
    indistinguishable from an unknown path (the SAME 401 fallback, DEC-78(b))."""

    def __init__(
        self,
        *,
        drop_dir: Path,
        clock: Callable[[], datetime] = _utc_now,
        max_body_bytes: int = MAX_VOICE_BODY_BYTES,
        stale_window_seconds: int = STALE_AUDIO_WINDOW_SECONDS,
    ) -> None:
        self._drop_dir = drop_dir
        self._clock = clock
        self.max_body_bytes = max_body_bytes
        self._stale_window_seconds = stale_window_seconds
        # sha256(bytes) -> utterance_id, process-lifetime only (see module docstring).
        self._dedup: dict[str, str] = {}

    async def handle(
        self, *, device_id: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[int, dict[str, object]]:
        """Validate and accept one captured utterance. Never raises — every rejection path
        returns a ``(status, json_body)`` pair for ``DeviceSurface`` to send via its own
        ``_respond``; nothing is ever written to ``drop_dir`` on a rejection."""
        if _parse_declared_length(headers.get("content-length")) > self.max_body_bytes:
            return 400, _ERROR_BAD_REQUEST

        captured_at = _parse_captured_at(headers.get(_CAPTURED_AT_HEADER))
        if captured_at is None:
            return 400, _ERROR_BAD_REQUEST

        age_seconds = (self._clock() - captured_at).total_seconds()
        if age_seconds > self._stale_window_seconds:
            # DEC-78(i): refuse stale audio, never deliver it late — nothing written.
            return 409, {
                "v": 1,
                "error": "stale_audio",
                "stale_audio_window_seconds": self._stale_window_seconds,
            }

        if headers.get("content-type", "").strip().lower() != _CONTENT_TYPE:
            return 400, _ERROR_BAD_REQUEST
        if not _looks_like_wav(body):
            return 400, _ERROR_BAD_REQUEST

        digest = hashlib.sha256(body).hexdigest()
        existing_utterance_id = self._dedup.get(digest)
        if existing_utterance_id is not None:
            # A repeat of identical bytes: same utterance_id, drop_dir is never touched again —
            # this is what keeps ASRSource.poll() at exactly one SourceEvent for this content
            # regardless of whether an earlier poll already moved the first copy to processed/.
            return 202, {
                "v": 1,
                "accepted": True,
                "utterance_id": existing_utterance_id,
                "device_id": device_id,
            }

        utterance_id = str(uuid.uuid4())
        self._drop_dir.mkdir(parents=True, exist_ok=True)
        (self._drop_dir / f"{digest}{_WRITE_SUFFIX}").write_bytes(body)
        self._dedup[digest] = utterance_id
        return 202, {
            "v": 1,
            "accepted": True,
            "utterance_id": utterance_id,
            "device_id": device_id,
        }


__all__ = ["MAX_VOICE_BODY_BYTES", "VoiceIngestHandler"]
