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

Q-105(a) (binding, TK-190): ``post`` also accepts OPTIONAL ADDITIVE ``data``/``files`` params for
multipart-form callers (e.g. ``ElevenLabsScribeTranscriber``) — mapped straight to httpx's
``data=``/``files=``. Existing ``content=``/``json=`` callers are byte-untouched; same timeout
and non-2xx-raise semantics apply regardless of which body kind is used.

``HttpxVoiceTransport`` is the ONE concrete production adapter, over ``httpx`` (rides the
optional ``voice-cloud`` extra — Q-46/Q-72 clean-checkout bar). ``httpx`` is LAZILY imported
inside ``__init__``, never at this module's top level, so ``import wombat.voice.transport``
always succeeds without the extra installed — only CONSTRUCTING ``HttpxVoiceTransport`` requires
it. Every request carries the explicit ``VOICE_HTTP_TIMEOUT_SECONDS`` timeout (a descriptive
module constant, not a TK-13 tunable); a non-2xx response raises ``VoiceTransportError`` rather
than returning a "successful" call a caller could misread as one.

DEC-28 (zero egress by default): nothing in this module is constructed anywhere in ``src``
outside of a caller — this ticket only sets the pattern; TK-193 wires provider selection.

DEC-73b (TK-330, additive protocol extension): ``StreamingVoiceTransport`` is a SEPARATE
``runtime_checkable`` ``Protocol`` extending ``VoiceTransport`` with one extra method,
``stream`` — ``VoiceTransport`` and ``post`` stay byte-untouched, so every existing
implementer/fake remains conformant with zero edits. ``HttpxVoiceTransport.stream`` is a
generator over httpx's streaming API (``client.stream`` POST context + ``iter_bytes``): a
non-2xx response raises ``VoiceTransportError`` before any chunk is yielded (generator laziness
means this fires on the caller's first iteration step); a connection failure mid-stream raises
``VoiceTransportError`` after exactly the chunks already yielded. No buffering, no retry, no
reconnect — the caller owns partial-failure semantics (TK-332).
"""

from __future__ import annotations

from collections.abc import Iterator
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
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes]] | None = None,
    ) -> tuple[int, bytes]:
        """POST to ``url`` with ``headers`` and one of: raw ``content`` bytes, a ``json`` body, or
        a ``data``/``files`` multipart-form pair (Q-105(a)) — never more than one kind at once.
        Returns ``(status_code, body_bytes)`` on a 2xx response; raises ``VoiceTransportError``
        otherwise."""
        ...


@runtime_checkable
class StreamingVoiceTransport(VoiceTransport, Protocol):
    """TK-330 (DEC-73b): an ADDITIVE extension of ``VoiceTransport`` — one extra method,
    ``stream`` — for providers whose response arrives as a chunked byte stream rather than one
    buffered body. ``VoiceTransport`` and ``post`` stay byte-untouched (a SEPARATE protocol, not
    a change to the existing one) so every existing implementer/fake remains conformant with zero
    edits; a transport that lacks ``stream`` simply is not usable for streaming calls.
    """

    def stream(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object] | None = None,
    ) -> Iterator[bytes]:
        """POST to ``url`` with ``headers`` and an optional ``json`` body, yielding raw response
        body chunks as they arrive. A non-2xx response raises ``VoiceTransportError`` BEFORE any
        chunk is yielded. A connection failure mid-stream raises ``VoiceTransportError`` AFTER
        exactly the chunks already yielded — the caller owns partial-failure semantics (TK-332)."""
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
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes]] | None = None,
    ) -> tuple[int, bytes]:
        response = self._httpx.post(
            url,
            headers=headers,
            content=content,
            json=json,
            data=data,
            files=files,
            timeout=VOICE_HTTP_TIMEOUT_SECONDS,
        )
        if not (200 <= response.status_code < 300):
            raise VoiceTransportError(
                f"voice transport POST {url} returned {response.status_code}: "
                f"{response.text[:500]!r}"
            )
        return response.status_code, response.content

    def stream(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object] | None = None,
    ) -> Iterator[bytes]:
        """TK-330 (DEC-73b): the ``StreamingVoiceTransport`` implementation — a generator over
        httpx's streaming API (``client.stream`` POST context + ``iter_bytes``), same
        ``VOICE_HTTP_TIMEOUT_SECONDS`` timeout as ``post``. Because this is a generator, NOTHING
        below runs until the caller takes the first iteration step — so a non-2xx status raises
        ``VoiceTransportError`` on that first step, before any chunk is yielded. A connection
        failure partway through raises ``VoiceTransportError`` AFTER exactly the chunks already
        yielded; no buffering, no retry, no reconnect."""
        with self._httpx.stream(
            "POST",
            url,
            headers=headers,
            json=json,
            timeout=VOICE_HTTP_TIMEOUT_SECONDS,
        ) as response:
            if not (200 <= response.status_code < 300):
                response.read()
                raise VoiceTransportError(
                    f"voice transport stream POST {url} returned {response.status_code}: "
                    f"{response.text[:500]!r}"
                )
            try:
                yield from response.iter_bytes()
            except self._httpx.HTTPError as exc:
                raise VoiceTransportError(
                    f"voice transport stream POST {url} failed mid-stream: {exc}"
                ) from exc


__all__ = [
    "VOICE_HTTP_TIMEOUT_SECONDS",
    "HttpxVoiceTransport",
    "StreamingVoiceTransport",
    "VoiceTransport",
    "VoiceTransportError",
]
