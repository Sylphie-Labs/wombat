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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import SecretStr

from wombat.config import WombatConfig
from wombat.sinks.tts_adapter import Pyttsx3Adapter, TTSAdapter
from wombat.sources.asr import FasterWhisperTranscriber, Transcriber
from wombat.voice.expressive import strip_allowed_tags
from wombat.voice.key_store import KeyringVoiceKeyStore, VoiceKeyStore, resolve_provider_key
from wombat.voice.stt import DeepgramTranscriber, ElevenLabsScribeTranscriber, FishAudioTranscriber
from wombat.voice.tts import DeepgramAuraTTSAdapter, ElevenLabsTTSAdapter, FishAudioTTSAdapter

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
    ``primary.speak`` always receives ``text`` byte-identical/verbatim."""

    def __init__(self, primary: TTSAdapter, *, fallback: TTSAdapter | None) -> None:
        self._primary = primary
        self._fallback = fallback

    def speak(self, text: str) -> None:
        try:
            self._primary.speak(text)
        except Exception:
            logger.warning(
                "voice: cloud TTS provider failed; falling back to local TTS", exc_info=True
            )
            if self._fallback is None:
                raise
            self._fallback.speak(strip_allowed_tags(text))


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
    provider: str, api_key: str, voice_id: str | None, config: WombatConfig
) -> TTSAdapter:
    """Construct the named cloud ``TTSAdapter`` (plain constructor args only, Q-104). Raises
    ``ImportError`` when the ``voice-cloud`` extra is not installed. ``voice_id`` is REQUIRED for
    fish/elevenlabs (checked by the caller before this is reached) and OPTIONAL for deepgram
    (``DeepgramAuraTTSAdapter`` applies its own ``DEEPGRAM_AURA_DEFAULT_MODEL`` default).

    TK-326 (DEC-71a/DEC-72a): the fish branch also threads ``config.wombat_fish_model`` into the
    adapter's ``model`` ctor param, pinning the Fish engine version on every TTS POST — the
    elevenlabs/deepgram branches below are byte-untouched."""
    if provider == "fish":
        assert voice_id is not None, "fish TTS requires voice_id (checked by the caller)"
        return FishAudioTTSAdapter(api_key, voice_id=voice_id, model=config.wombat_fish_model)
    if provider == "elevenlabs":
        assert voice_id is not None, "elevenlabs TTS requires voice_id (checked by the caller)"
        return ElevenLabsTTSAdapter(api_key, voice_id=voice_id)
    return DeepgramAuraTTSAdapter(api_key, voice_id=voice_id)


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
    config: WombatConfig, key_store: VoiceKeyStore | None = None
) -> tuple[TTSAdapter | None, TTSBuildInfo]:
    """Build the ``TTSAdapter`` ``config.wombat_tts_provider`` selects, or ``None`` (TK-193,
    Q-105(d)), alongside the ``TTSBuildInfo`` recording whether that build's primary is a truly
    constructed Fish instance (TK-328, ruling v2.187 r1). ``build_tts_adapter`` below is a thin
    adapter-only wrapper over this. ``key_store`` defaults to ``KeyringVoiceKeyStore()`` — tests
    always inject an in-memory fake (never the real keyring)."""
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
        primary: TTSAdapter = _construct_cloud_tts(provider, key, voice_id, config)
    except ImportError:
        logger.warning(
            "voice: cloud TTS provider %r selected but the 'voice-cloud' extra is not "
            "installed (`uv sync --extra voice-cloud`) — falling back to local TTS",
            provider,
            exc_info=True,
        )
        return _build_local_tts(config), no_info

    adapter = FallbackTTSAdapter(primary, fallback=_build_local_tts(config, role="fallback"))
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
