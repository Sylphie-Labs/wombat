"""wombat.voice.transport — the thin HTTP transport seam cloud voice providers ride (TK-189,
EP-31, Q-100, Q-104).

Q-104 ruling (binding): homed here, NOT ``stt.py`` — a future ``wombat.voice.tts`` cloud
provider rides this SAME seam, so TTS never has to cross-import the STT module just to make an
HTTP call. Q-100 (binding): thin ``httpx`` REST only, no vendor SDKs anywhere in the voice-cloud
provider stack (TK-190/191/192 ride this same pattern).

``VoiceTransport`` is a minimal ``Protocol`` — one method, ``post`` — so provider tests
(``DeepgramTranscriber`` and future providers) never need a real network call: inject a fake
that records the call and returns a canned ``(status_code, body_bytes)`` pair, or raises
``VoiceTransportError`` to simulate a non-2xx response (DEF-7: no live calls in tests).

``HttpxVoiceTransport`` is the ONE concrete production adapter, over ``httpx`` (rides the
optional ``voice-cloud`` extra — Q-46/Q-72 clean-checkout bar). ``httpx`` is LAZILY imported
inside ``__init__``, never at this module's top level, so ``import wombat.voice.transport``
always succeeds without the extra installed — only CONSTRUCTING ``HttpxVoiceTransport`` requires
it. Every request carries the explicit ``VOICE_HTTP_TIMEOUT_SECONDS`` timeout (a descriptive
module constant, not a TK-13 tunable); a non-2xx response raises ``VoiceTransportError`` rather
than returning a "successful" call a caller could misread as one.

DEC-28 (zero egress by default): nothing in this module is constructed anywhere in ``src``
outside of a caller — this ticket only sets the pattern; TK-193 wires provider selection.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# Explicit per-request timeout for every voice-cloud HTTP call (Q-104) — never configurable.
VOICE_HTTP_TIMEOUT_SECONDS = 30.0


class VoiceTransportError(RuntimeError):
    """Raised by a ``VoiceTransport`` implementation when an HTTP POST returns a non-2xx status
    (Q-104) — never returned as a quiet ``(status_code, body_bytes)`` pair a caller could
    misinterpret as success."""


@runtime_checkable
class VoiceTransport(Protocol):
    """The one HTTP operation every cloud voice provider needs: a single POST returning the
    status code and raw response body. Implementations raise ``VoiceTransportError`` on a
    non-2xx response rather than returning it — a successful return is always a 2xx."""

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        content: bytes | None = None,
        json: dict[str, object] | None = None,
    ) -> tuple[int, bytes]:
        """POST to ``url`` with ``headers`` and either raw ``content`` bytes or a ``json`` body
        (never both). Returns ``(status_code, body_bytes)`` on a 2xx response; raises
        ``VoiceTransportError`` otherwise."""
        ...


class HttpxVoiceTransport:
    """The real production ``VoiceTransport`` — a thin wrapper over ``httpx.post`` (Q-100: no
    vendor SDKs). ``httpx`` is LAZILY imported here, inside ``__init__``, so a checkout without
    the ``voice-cloud`` extra still imports this module cleanly (Q-46/Q-72); only constructing
    this class requires the extra to be installed.
    """

    def __init__(self) -> None:
        import httpx  # lazy import (Q-46/Q-72) — voice-cloud extra

        self._httpx = httpx

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        content: bytes | None = None,
        json: dict[str, object] | None = None,
    ) -> tuple[int, bytes]:
        response = self._httpx.post(
            url,
            headers=headers,
            content=content,
            json=json,
            timeout=VOICE_HTTP_TIMEOUT_SECONDS,
        )
        if not (200 <= response.status_code < 300):
            raise VoiceTransportError(
                f"voice transport POST {url} returned {response.status_code}: "
                f"{response.text[:500]!r}"
            )
        return response.status_code, response.content


__all__ = [
    "VOICE_HTTP_TIMEOUT_SECONDS",
    "HttpxVoiceTransport",
    "VoiceTransport",
    "VoiceTransportError",
]
