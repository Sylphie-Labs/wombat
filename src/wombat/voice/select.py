"""wombat.voice.select — provider selection + cloud-to-local fallback (TK-193, EP-31, Q-105(d)).

The composition-root seam that turns ``config.wombat_stt_provider``/``config.wombat_tts_
provider`` into an actual ``sources.asr.Transcriber``/``sinks.tts_adapter.TTSAdapter`` instance
(or ``None``). This is the ONLY module that constructs a cloud voice-provider class (``voice.
stt``/``voice.tts``) anywhere reachable from boot (DEC-28: zero egress by default) — TK-189/190/
191/192 only set the provider pattern, they are never self-wired.

``build_transcriber``/``build_tts_adapter`` share one shape:

  1. ``provider == "local"`` -> construct exactly today's local wiring (``FasterWhisperTranscriber``
     / ``Pyttsx3Adapter``) and return. NO cloud class is constructed, NO key store read happens.
  2. A cloud provider is named -> resolve its key via ``key_store.resolve_provider_key`` (env
     override, else the vault; DEC-32). Unresolvable key, a missing REQUIRED ``voice_id``
     (fish/elevenlabs TTS), or an ``ImportError`` constructing the cloud class (the ``voice-cloud``
     extra not installed) each emit ONE loud warning naming the exact gap, then fall through to
     step 1's local construction — voice selection NEVER fails boot (CON-3, the Q-67 loud-skip
     shape).
  3. A successfully constructed cloud instance is wrapped in ``FallbackTranscriber``/
     ``FallbackTTSAdapter`` over a best-effort local instance (``None`` if that itself doesn't
     construct). DEC-28 direction is STRICTLY cloud -> local: no path here ever places a cloud
     instance in a fallback slot.

``key_store`` defaults to ``KeyringVoiceKeyStore()`` — unit tests always inject an in-memory fake
(Q-57(a); NEVER the real keyring, DEF-7).

``build_tts_adapter_with_info`` (TK-328, ruling v2.187 r1) is an ADDITIVE companion to
``build_tts_adapter`` above: it returns the SAME adapter plus a ``TTSBuildInfo`` recording whether
a Fish TTS primary was truly constructed (never a degrade path) — ``assemble_runtime`` is its sole
consumer, deciding whether to offer Fish's bracket-marker expressive instruction. ``build_tts_
adapter`` itself is unchanged in behavior, now a thin wrapper over it.

TK-332 (DEC-73a/d/f): ``_construct_cloud_tts``'s fish branch wires a ``StreamingAudioWriter``
factory into the adapter iff ``stream_playback.streaming_available()`` — a missing streaming dep
(sounddevice, part of the ``voice-cloud`` extra) is ONE loud warning, never a boot failure; the
adapter still builds, just without streaming. Streaming is orthogonal to expressive tags/model
choice — no interaction with the TK-328 ``TTSBuildInfo`` decision above.

TK-343 (DEC-79, R5): ``build_tts_adapter_with_info`` gains an OPTIONAL keyword-only
``turn_origin_register``/``sealed_utterance_store`` pair — wired ONLY at ``assemble_runtime``'s
drain-graph ``SpeakSink`` adapter construction, NEVER at ``make_speak_callable``'s separate brief
adapter, so a morning brief is structurally never remote-eligible (no register even reachable from
that call site) rather than merely usually-local. When BOTH are given AND
``stream_playback.streaming_available()`` (R5: remote routing is orthogonal to, and strictly
requires, the existing streaming gate), ``_construct_cloud_tts``'s fish branch wires an
origin-aware CLOSURE (``_build_remote_aware_writer_factory``) instead of the bare
``StreamingAudioWriter`` class — the closure re-reads ``turn_origin_register`` at EVERY call
(``voice.tts._speak_streaming`` invokes ``writer_factory()`` once per utterance, never once at
construction), claiming and consuming a fresh origin to route that ONE utterance to a fresh
``voice.remote_sinks.BufferedUtteranceSink``; an empty/stale/already-claimed register routes to
the SAME real local hardware ``StreamingAudioWriter()`` as today. ``_RemoteRouteAttempted`` is a
tiny same-seam flag the closure marks the instant it commits an utterance remotely and
``FallbackTTSAdapter`` consumes right after a primary failure — DEC-79's "no laptop fallback on
remote failure, ever": a remote-origin turn's total Fish failure must re-raise rather than
rescue-speak into an empty room while logging success. Either arg omitted (``None``, the default)
preserves EVERY existing call site byte-identically — the bare class, no flag, today's exact
fallback-on-any-failure posture.

TK-343 major repair: the ``PartialSpeechError`` ``played_any=True`` arm in ``FallbackTTSAdapter.
speak`` below now ALSO consumes ``_RemoteRouteAttempted`` before its unconditional re-raise (it
previously only checked/consumed the flag in the ``played_any=False`` and bare-``Exception`` arms)
— left unconsumed, a mark from a watch turn whose Fish stream died mid-stream leaked into the
NEXT ``speak()`` call, wrongly forcing that unrelated (often local) turn's own Fish failure to
re-raise instead of falling back to local TTS.

TK-343 critical repair: proactive (non voice-turn) surfacings must never claim the shared
``turn_origin_register`` even though they speak through the SAME drain-graph ``SpeakSink``/adapter/
closure a voice turn's own reply does — ``sinks.speak.SpeakSink`` now wraps its ``adapter.speak()``
call in ``turn_origin_register.claims_suppressed()`` for exactly that case (see that module's
docstring); this closure and ``LastTurnOriginRegister.take()`` are otherwise unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import SecretStr

from wombat.config import WombatConfig
from wombat.sinks.tts_adapter import Pyttsx3Adapter, TTSAdapter
from wombat.sources.asr import FasterWhisperTranscriber, Transcriber
from wombat.voice import tts as voice_tts
from wombat.voice.expressive import strip_allowed_tags
from wombat.voice.key_store import KeyringVoiceKeyStore, VoiceKeyStore, resolve_provider_key
from wombat.voice.remote_sinks import BufferedUtteranceSink, SealedUtteranceStore
from wombat.voice.stream_playback import StreamingAudioWriter, streaming_available
from wombat.voice.stt import DeepgramTranscriber, ElevenLabsScribeTranscriber, FishAudioTranscriber
from wombat.voice.tts import DeepgramAuraTTSAdapter, ElevenLabsTTSAdapter, FishAudioTTSAdapter
from wombat.voice.turn_origin import LastTurnOriginRegister

logger = logging.getLogger(__name__)

# Providers that REQUIRE config.wombat_tts_voice_id (Deepgram Aura has its own code default,
# DEEPGRAM_AURA_DEFAULT_MODEL, when voice_id is None — never listed here).
_TTS_PROVIDERS_REQUIRING_VOICE_ID = frozenset({"fish", "elevenlabs"})


class FallbackTranscriber:
    """Wraps a cloud ``Transcriber`` with a best-effort local fallback (DEC-28: cloud -> local,
    never the reverse). On ANY exception from ``primary.transcribe``, logs ONE loud warning, then
    tries ``fallback`` exactly once: ``fallback`` is ``None`` -> the primary's exception
    propagates; ``fallback`` itself raises -> ITS exception propagates. Either way the caller's
    own degrade machinery (``ASRSource`` per-file ``failed/``) sees a raised exception, never a
    lying return."""

    def __init__(self, primary: Transcriber, *, fallback: Transcriber | None) -> None:
        self._primary = primary
        self._fallback = fallback

    def transcribe(self, path: Path) -> str:
        try:
            return self._primary.transcribe(path)
        except Exception:
            logger.warning(
                "voice: cloud STT provider failed transcribing %s; falling back to local ASR",
                path,
                exc_info=True,
            )
            if self._fallback is None:
                raise
            return self._fallback.transcribe(path)


class FallbackTTSAdapter:
    """Wraps a cloud ``TTSAdapter`` with a best-effort local fallback (DEC-28: cloud -> local,
    never the reverse). On ANY exception from ``primary.speak``, logs ONE loud warning, then
    tries ``fallback`` exactly once: ``fallback`` is ``None`` -> the primary's exception
    propagates; ``fallback`` itself raises -> ITS exception propagates. Either way the caller's
    own degrade machinery (``SpeakSink``'s ``Degraded(to=None)``) sees a raised exception, never a
    lying success.

    TK-328 fallback hygiene: the fallback branch strips every ``voice.expressive.ALLOWED_TAGS``
    marker (``voice.expressive.strip_allowed_tags``) before handing text to ``fallback.speak`` —
    the local engine never speaks Fish's bracket markers aloud. The primary branch is untouched;
    ``primary.speak`` always receives ``text`` byte-identical/verbatim.

    TK-332 AC5 (ISS-39 f1 ruling, v2.195): ``voice.tts.PartialSpeechError`` is caught in its OWN
    ``except`` clause, ordered BEFORE the bare ``except Exception`` below (it is a ``RuntimeError``
    subclass, so ordering matters). ``played_any=True`` is always re-raised UNCHANGED, with NO
    fallback attempt — the user already heard the Fish primary begin speaking before it died
    mid-stream, so a local fallback speaking the whole utterance over again would duplicate audio
    rather than degrade gracefully. Re-raising lets ``SpeakSink``'s own dedicated
    ``PartialSpeechError`` handling (the DEC-73e played-partial-counts-as-spoken branch) actually
    run in the assembled runtime, where every Fish primary is always wrapped in this class.
    ``played_any=False`` (no audio ever reached the user) keeps TODAY'S EXACT posture instead —
    the same loud warning and stripped-tag fallback attempt (or re-raise if no fallback was
    constructed) as the bare ``except Exception`` arm below. The except clause references
    ``voice_tts.PartialSpeechError`` (a module attribute, ``import wombat.voice.tts as voice_tts``)
    rather than a value bound via ``from ... import PartialSpeechError`` — mirrors ``sinks.speak``'s
    own fix for the same hazard: a test suite that ``importlib.reload``s ``wombat.voice.tts``
    rebinds the class in that module's namespace, and a value-bound import here would freeze the
    pre-reload identity."""

    def __init__(
        self,
        primary: TTSAdapter,
        *,
        fallback: TTSAdapter | None,
        remote_attempt: _RemoteRouteAttempted | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        # TK-343 (DEC-79): the SAME flag object `voice.select`'s writer_factory closure marks —
        # None (the default) preserves every existing call site's behavior byte-identically.
        self._remote_attempt = remote_attempt

    def speak(self, text: str) -> None:
        try:
            self._primary.speak(text)
        except voice_tts.PartialSpeechError as exc:
            # TK-332 AC5 (ruling v2.195): ordered ahead of the bare Exception arm below.
            if exc.played_any:
                # Partial playback already reached the user -- re-raise unchanged, no fallback
                # attempt. TK-343 major repair: still CONSUME any remote_attempt mark before
                # re-raising -- this arm never checked it (a watch turn's own reply is exactly
                # the case most likely to mark it), so leaving it set here leaked the mark into
                # the NEXT speak() call and wrongly forced re-raise-no-fallback on an unrelated
                # laptop turn's Fish failure.
                self._remote_route_was_attempted()
                raise
            if self._remote_route_was_attempted():
                # TK-343 (DEC-79): a remote-origin turn's total Fish failure -- no chunk ever
                # reached the (would-be) watch buffer either -- must never rescue-speak on the
                # laptop into an empty room while logging success.
                raise
            # No audio ever played, and this was not a remote-origin turn -- degrades exactly
            # like any other primary failure below.
            self._warn_and_fallback(text)
        except Exception:
            if self._remote_route_was_attempted():
                raise
            self._warn_and_fallback(text)
        else:
            # TK-343 repair: a SUCCESSFUL primary speak still may have marked remote_attempt
            # (the writer_factory closure marks it before Fish's transport runs, not after a
            # failure). Left uncleared, the mark leaks into the NEXT speak() call and wrongly
            # blocks that call's local-TTS fallback on a Fish failure. Consume it here so every
            # speak() call starts this flag fresh, regardless of primary outcome.
            self._remote_route_was_attempted()

    def _remote_route_was_attempted(self) -> bool:
        """TK-343 (DEC-79): consumes the ONE-shot flag ``voice.select``'s writer_factory closure
        marked the instant it committed THIS ``speak()`` call to the remote sink. ``False`` when
        no ``remote_attempt`` was ever wired (every pre-TK-343 caller, and the brief adapter,
        which never receives one) -- byte-identical to before."""
        return self._remote_attempt is not None and self._remote_attempt.take()

    def _warn_and_fallback(self, text: str) -> None:
        """Today's exact degrade posture (shared by the bare ``Exception`` arm and the
        ``played_any=False`` branch of the ``PartialSpeechError`` arm, ruling v2.195): ONE loud
        warning, then a stripped-tag fallback attempt if constructed, else re-raise."""
        logger.warning(
            "voice: cloud TTS provider failed; falling back to local TTS", exc_info=True
        )
        if self._fallback is None:
            raise
        self._fallback.speak(strip_allowed_tags(text))


class _RemoteRouteAttempted:
    """TK-343 (DEC-79): a tiny mutable flag shared between ONE origin-aware writer_factory
    closure and the ``FallbackTTSAdapter`` wrapping the SAME primary — both built together at
    this construction seam. The closure ``mark()``s it the instant it commits a given utterance
    to the remote sink (BEFORE Fish's transport is ever called); ``FallbackTTSAdapter`` ``take()``
    s it right after a primary failure to decide whether a laptop-speaker rescue is even
    permitted. Not a register, not a config knob — a same-object, same-call-stack handoff."""

    def __init__(self) -> None:
        self._value = False

    def mark(self) -> None:
        self._value = True

    def take(self) -> bool:
        value, self._value = self._value, False
        return value


def _build_remote_aware_writer_factory(
    turn_origin_register: LastTurnOriginRegister,
    sealed_utterance_store: SealedUtteranceStore,
    remote_attempt: _RemoteRouteAttempted,
) -> Callable[[], StreamingAudioWriter]:
    """TK-343 (DEC-79, R5): the ONE closure at this construction seam, wired into
    ``FishAudioTTSAdapter``'s ``writer_factory`` in place of the bare ``StreamingAudioWriter``
    class. ``voice.tts._speak_streaming`` invokes the returned callable once PER UTTERANCE, never
    once at construction — so this reads ``turn_origin_register`` at CALL time on every
    invocation, letting two utterances spoken back to back through the identical closure route
    differently (TK-343 AC2). A claimed (fresh, unconsumed) origin marks ``remote_attempt`` and
    routes to a fresh ``voice.remote_sinks.BufferedUtteranceSink``; an empty register (nothing
    noted, already claimed by an earlier speak, or aged past its TTL) routes to the SAME real
    local-hardware ``StreamingAudioWriter()`` every pre-TK-343 caller gets."""

    def factory() -> StreamingAudioWriter:
        origin = turn_origin_register.take()
        if origin is None:
            return StreamingAudioWriter()
        remote_attempt.mark()
        return StreamingAudioWriter(
            stream_factory=lambda: BufferedUtteranceSink(
                origin_device_id=origin.device_id,
                utterance_id=origin.utterance_id,
                store=sealed_utterance_store,
            )
        )

    return factory


def _cloud_api_key_field(config: WombatConfig, provider: str) -> SecretStr | None:
    """The ``config.wombat_<provider>_api_key`` field for a cloud STT/TTS provider name."""
    return {
        "deepgram": config.wombat_deepgram_api_key,
        "elevenlabs": config.wombat_elevenlabs_api_key,
        "fish": config.wombat_fish_api_key,
    }[provider]


def _api_key_env_var(provider: str) -> str:
    """The env var name ``resolve_provider_key`` would have honored as an override (matches
    pydantic-settings' field -> env var derivation, e.g. ``wombat_deepgram_api_key`` ->
    ``WOMBAT_DEEPGRAM_API_KEY``) — named in the loud gap log, never guessed by the operator."""
    return f"WOMBAT_{provider.upper()}_API_KEY"


def _build_local_transcriber(
    config: WombatConfig, *, role: Literal["primary", "fallback"] = "primary"
) -> Transcriber | None:
    """Construct the local ``FasterWhisperTranscriber`` (today's exact wiring, byte-preserved).
    ANY construction failure (missing ``voice`` extra, or a whisper model load failure — uncached
    model + offline, a bad ``WOMBAT_ASR_MODEL``, a corrupted HF cache) is caught, logged LOUD, and
    degrades to ``None`` — never blocks boot (CON-3, CRF-6). ``role`` is a log-routing
    discriminator only (CR4-1, TK-217): ``"primary"`` (default) is today's exact byte-preserved
    message; ``"fallback"`` is used when filling the fallback slot of an already-healthy cloud
    transcriber, where local ASR is NOT the only voice input and the message must say so instead
    of implying ASR is unavailable altogether."""
    try:
        return FasterWhisperTranscriber(model_name=config.wombat_asr_model)
    except Exception:
        if role == "fallback":
            logger.warning(
                "voice: local ASR fallback is not installed — install the 'voice' extra "
                "(`uv sync --extra voice`) to enable it; the cloud STT primary remains active",
                exc_info=True,
            )
        else:
            logger.warning(
                "voice: local STT (faster-whisper) is not installed — install the 'voice' extra "
                "(`uv sync --extra voice`) to enable local ASR (boot continues without it)",
                exc_info=True,
            )
        return None


def _build_local_tts(
    config: WombatConfig, *, role: Literal["primary", "fallback"] = "primary"
) -> TTSAdapter | None:
    """Construct the local ``Pyttsx3Adapter`` (today's exact wiring, byte-preserved). ANY
    construction failure (missing ``voice`` extra or an OS TTS engine-init failure) is caught,
    logged LOUD, and degrades to ``None`` — never blocks boot. ``role`` is a log-routing
    discriminator only (CR4-1, TK-217): ``"primary"`` (default) is today's exact byte-preserved
    message; ``"fallback"`` is used when filling the fallback slot of an already-healthy cloud TTS
    adapter, where voice output is NOT disabled — the cloud primary still speaks — so the message
    must not claim otherwise."""
    del config  # Pyttsx3Adapter takes no config-derived args; kept for signature symmetry.
    try:
        return Pyttsx3Adapter()
    except Exception:
        if role == "fallback":
            logger.warning(
                "voice: local TTS fallback failed to construct (is the 'voice' extra installed? "
                "`uv sync --extra voice`) — the cloud TTS primary remains active",
                exc_info=True,
            )
        else:
            logger.warning(
                "voice: TTS adapter failed to construct (is the 'voice' extra installed? "
                "`uv sync --extra voice`) — voice output disabled for this boot",
                exc_info=True,
            )
        return None


def _construct_cloud_transcriber(provider: str, api_key: str, config: WombatConfig) -> Transcriber:
    """Construct the named cloud ``Transcriber`` (plain constructor args only, Q-104 — this
    module owns key/model selection; the provider classes themselves never read config/keyring).
    Raises ``ImportError`` when the ``voice-cloud`` extra is not installed (the default transport
    construction inside each provider's ``__init__``)."""
    if provider == "deepgram":
        return DeepgramTranscriber(api_key, model=config.wombat_stt_model)
    if provider == "elevenlabs":
        return ElevenLabsScribeTranscriber(api_key, model=config.wombat_stt_model)
    return FishAudioTranscriber(api_key)


def _construct_cloud_tts(
    provider: str,
    api_key: str,
    voice_id: str | None,
    config: WombatConfig,
    *,
    turn_origin_register: LastTurnOriginRegister | None = None,
    sealed_utterance_store: SealedUtteranceStore | None = None,
) -> tuple[TTSAdapter, _RemoteRouteAttempted | None]:
    """Construct the named cloud ``TTSAdapter`` (plain constructor args only, Q-104), alongside
    the TK-343 remote-attempt flag (``None`` unless the fish branch actually wired the
    remote-aware closure below). Raises ``ImportError`` when the ``voice-cloud`` extra is not
    installed. ``voice_id`` is REQUIRED for fish/elevenlabs (checked by the caller before this is
    reached) and OPTIONAL for deepgram (``DeepgramAuraTTSAdapter`` applies its own
    ``DEEPGRAM_AURA_DEFAULT_MODEL`` default).

    TK-326 (DEC-71a/DEC-72a): the fish branch also threads ``config.wombat_fish_model`` into the
    adapter's ``model`` ctor param, pinning the Fish engine version on every TTS POST — the
    elevenlabs/deepgram branches below are byte-untouched.

    TK-332 (DEC-73a/d/f): the fish branch ALSO wires a ``writer_factory`` (``StreamingAudioWriter``
    itself, a zero-arg callable) iff ``stream_playback.streaming_available()`` — otherwise ONE
    loud WARNING naming the missing extra and the adapter is built WITHOUT streaming (the buffered
    wav+winsound path, byte-identical to today). Structural, no new config (DEC-63); the
    elevenlabs/deepgram branches are byte-untouched.

    TK-343 (DEC-79, R5): when streaming IS available AND both ``turn_origin_register``/
    ``sealed_utterance_store`` are given, the ``writer_factory`` becomes the origin-aware closure
    (``_build_remote_aware_writer_factory``) instead of the bare class, and the returned flag is
    non-``None``. Either arg omitted (the default) is the EXACT TK-332 wiring above, unchanged —
    the elevenlabs/deepgram branches never receive either arg and always return ``None`` for the
    flag."""
    if provider == "fish":
        assert voice_id is not None, "fish TTS requires voice_id (checked by the caller)"
        if streaming_available():
            remote_attempt: _RemoteRouteAttempted | None = None
            writer_factory: Callable[[], StreamingAudioWriter] = StreamingAudioWriter
            if turn_origin_register is not None and sealed_utterance_store is not None:
                remote_attempt = _RemoteRouteAttempted()
                writer_factory = _build_remote_aware_writer_factory(
                    turn_origin_register, sealed_utterance_store, remote_attempt
                )
            return (
                FishAudioTTSAdapter(
                    api_key,
                    voice_id=voice_id,
                    model=config.wombat_fish_model,
                    writer_factory=writer_factory,
                ),
                remote_attempt,
            )
        logger.warning(
            "voice: fish TTS streaming playback is unavailable — install the 'voice-cloud' "
            "extra's sounddevice dependency (`uv sync --extra voice-cloud`) to enable low-latency "
            "streamed playback; using buffered playback for this boot"
        )
        return (
            FishAudioTTSAdapter(api_key, voice_id=voice_id, model=config.wombat_fish_model),
            None,
        )
    if provider == "elevenlabs":
        assert voice_id is not None, "elevenlabs TTS requires voice_id (checked by the caller)"
        return ElevenLabsTTSAdapter(api_key, voice_id=voice_id), None
    return DeepgramAuraTTSAdapter(api_key, voice_id=voice_id), None


def build_transcriber(
    config: WombatConfig, key_store: VoiceKeyStore | None = None
) -> Transcriber | None:
    """Build the ``Transcriber`` ``config.wombat_stt_provider`` selects, or ``None`` (TK-193,
    Q-105(d)). ``key_store`` defaults to ``KeyringVoiceKeyStore()`` — tests always inject an
    in-memory fake (never the real keyring)."""
    store = key_store if key_store is not None else KeyringVoiceKeyStore()
    provider = config.wombat_stt_provider
    if provider == "local":
        return _build_local_transcriber(config)

    key = resolve_provider_key(provider, _cloud_api_key_field(config, provider), store)
    if key is None:
        logger.warning(
            "voice: cloud STT provider %r selected but no API key resolved (set %s or store "
            "one in the OS keyring) — falling back to local ASR",
            provider,
            _api_key_env_var(provider),
        )
        return _build_local_transcriber(config)

    try:
        primary: Transcriber = _construct_cloud_transcriber(provider, key, config)
    except ImportError:
        logger.warning(
            "voice: cloud STT provider %r selected but the 'voice-cloud' extra is not "
            "installed (`uv sync --extra voice-cloud`) — falling back to local ASR",
            provider,
            exc_info=True,
        )
        return _build_local_transcriber(config)

    return FallbackTranscriber(
        primary, fallback=_build_local_transcriber(config, role="fallback")
    )


@dataclass(frozen=True)
class TTSBuildInfo:
    """The constructed-adapter seam's decision record (TK-328, ruling v2.187 r1):
    ``fish_primary`` is ``True`` ONLY when the ``FishAudioTTSAdapter`` PRIMARY was actually
    constructed by ``build_tts_adapter_with_info`` — every degrade path (no resolved key, blank
    ``voice_id``, ``ImportError``, a non-fish provider, or ``local``) yields ``False``.
    ``fish_model`` is the ``config.wombat_fish_model`` the primary was built with when
    ``fish_primary`` is ``True``, else ``None``. This is a structural key-gate, not a config
    toggle — ``assemble_runtime`` is the sole consumer, deciding whether Fish's bracket-marker
    expressive instruction may ever be offered."""

    fish_primary: bool
    fish_model: str | None


def build_tts_adapter_with_info(
    config: WombatConfig,
    key_store: VoiceKeyStore | None = None,
    *,
    turn_origin_register: LastTurnOriginRegister | None = None,
    sealed_utterance_store: SealedUtteranceStore | None = None,
) -> tuple[TTSAdapter | None, TTSBuildInfo]:
    """Build the ``TTSAdapter`` ``config.wombat_tts_provider`` selects, or ``None`` (TK-193,
    Q-105(d)), alongside the ``TTSBuildInfo`` recording whether that build's primary is a truly
    constructed Fish instance (TK-328, ruling v2.187 r1). ``build_tts_adapter`` below is a thin
    adapter-only wrapper over this. ``key_store`` defaults to ``KeyringVoiceKeyStore()`` — tests
    always inject an in-memory fake (never the real keyring).

    ``turn_origin_register``/``sealed_utterance_store`` (TK-343, DEC-79) are OPTIONAL and
    threaded straight through to ``_construct_cloud_tts``/``FallbackTTSAdapter`` —
    ``assemble_runtime`` passes them ONLY for the drain-graph's SpeakSink adapter, never for
    ``make_speak_callable``'s separate brief adapter. Omitting either (the default) preserves
    every existing call site byte-identically."""
    store = key_store if key_store is not None else KeyringVoiceKeyStore()
    provider = config.wombat_tts_provider
    no_info = TTSBuildInfo(fish_primary=False, fish_model=None)
    if provider == "local":
        return _build_local_tts(config), no_info

    key = resolve_provider_key(provider, _cloud_api_key_field(config, provider), store)
    if key is None:
        logger.warning(
            "voice: cloud TTS provider %r selected but no API key resolved (set %s or store "
            "one in the OS keyring) — falling back to local TTS",
            provider,
            _api_key_env_var(provider),
        )
        return _build_local_tts(config), no_info

    voice_id = config.wombat_tts_voice_id
    if provider in _TTS_PROVIDERS_REQUIRING_VOICE_ID and not (voice_id or "").strip():
        logger.warning(
            "voice: cloud TTS provider %r selected but WOMBAT_TTS_VOICE_ID is missing/blank "
            "(required for %r) — falling back to local TTS",
            provider,
            provider,
        )
        return _build_local_tts(config), no_info

    try:
        primary, remote_attempt = _construct_cloud_tts(
            provider,
            key,
            voice_id,
            config,
            turn_origin_register=turn_origin_register,
            sealed_utterance_store=sealed_utterance_store,
        )
    except ImportError:
        logger.warning(
            "voice: cloud TTS provider %r selected but the 'voice-cloud' extra is not "
            "installed (`uv sync --extra voice-cloud`) — falling back to local TTS",
            provider,
            exc_info=True,
        )
        return _build_local_tts(config), no_info

    adapter = FallbackTTSAdapter(
        primary, fallback=_build_local_tts(config, role="fallback"), remote_attempt=remote_attempt
    )
    if provider == "fish":
        return adapter, TTSBuildInfo(fish_primary=True, fish_model=config.wombat_fish_model)
    return adapter, no_info


def build_tts_adapter(
    config: WombatConfig, key_store: VoiceKeyStore | None = None
) -> TTSAdapter | None:
    """Build the ``TTSAdapter`` ``config.wombat_tts_provider`` selects, or ``None`` (TK-193,
    Q-105(d)) — a thin adapter-only wrapper over ``build_tts_adapter_with_info`` (TK-328) so its
    two existing call sites (``bootstrap.build_speak_sink``/``bootstrap.make_speak_callable``)
    stay byte-identical. ``key_store`` defaults to ``KeyringVoiceKeyStore()`` — tests always
    inject an in-memory fake (never the real keyring)."""
    adapter, _info = build_tts_adapter_with_info(config, key_store)
    return adapter


__all__ = [
    "FallbackTTSAdapter",
    "FallbackTranscriber",
    "TTSBuildInfo",
    "build_transcriber",
    "build_tts_adapter",
    "build_tts_adapter_with_info",
]
