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
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import SecretStr

from wombat.config import WombatConfig
from wombat.sinks.tts_adapter import Pyttsx3Adapter, TTSAdapter
from wombat.sources.asr import FasterWhisperTranscriber, Transcriber
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
    lying success."""

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
            self._fallback.speak(text)


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


def _build_local_transcriber(config: WombatConfig) -> Transcriber | None:
    """Construct the local ``FasterWhisperTranscriber`` (today's exact wiring, byte-preserved).
    An ``ImportError`` (the ``voice`` extra not installed) is caught, logged LOUD, and degrades to
    ``None`` — never blocks boot."""
    try:
        return FasterWhisperTranscriber(model_name=config.wombat_asr_model)
    except ImportError:
        logger.warning(
            "voice: local STT (faster-whisper) is not installed — install the 'voice' extra "
            "(`uv sync --extra voice`) to enable local ASR (boot continues without it)",
            exc_info=True,
        )
        return None


def _build_local_tts(config: WombatConfig) -> TTSAdapter | None:
    """Construct the local ``Pyttsx3Adapter`` (today's exact wiring, byte-preserved). ANY
    construction failure (missing ``voice`` extra or an OS TTS engine-init failure) is caught,
    logged LOUD, and degrades to ``None`` — never blocks boot."""
    del config  # Pyttsx3Adapter takes no config-derived args; kept for signature symmetry.
    try:
        return Pyttsx3Adapter()
    except Exception:
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


def _construct_cloud_tts(provider: str, api_key: str, voice_id: str | None) -> TTSAdapter:
    """Construct the named cloud ``TTSAdapter`` (plain constructor args only, Q-104). Raises
    ``ImportError`` when the ``voice-cloud`` extra is not installed. ``voice_id`` is REQUIRED for
    fish/elevenlabs (checked by the caller before this is reached) and OPTIONAL for deepgram
    (``DeepgramAuraTTSAdapter`` applies its own ``DEEPGRAM_AURA_DEFAULT_MODEL`` default)."""
    if provider == "fish":
        assert voice_id is not None, "fish TTS requires voice_id (checked by the caller)"
        return FishAudioTTSAdapter(api_key, voice_id=voice_id)
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

    return FallbackTranscriber(primary, fallback=_build_local_transcriber(config))


def build_tts_adapter(
    config: WombatConfig, key_store: VoiceKeyStore | None = None
) -> TTSAdapter | None:
    """Build the ``TTSAdapter`` ``config.wombat_tts_provider`` selects, or ``None`` (TK-193,
    Q-105(d)). ``key_store`` defaults to ``KeyringVoiceKeyStore()`` — tests always inject an
    in-memory fake (never the real keyring)."""
    store = key_store if key_store is not None else KeyringVoiceKeyStore()
    provider = config.wombat_tts_provider
    if provider == "local":
        return _build_local_tts(config)

    key = resolve_provider_key(provider, _cloud_api_key_field(config, provider), store)
    if key is None:
        logger.warning(
            "voice: cloud TTS provider %r selected but no API key resolved (set %s or store "
            "one in the OS keyring) — falling back to local TTS",
            provider,
            _api_key_env_var(provider),
        )
        return _build_local_tts(config)

    voice_id = config.wombat_tts_voice_id
    if provider in _TTS_PROVIDERS_REQUIRING_VOICE_ID and not (voice_id or "").strip():
        logger.warning(
            "voice: cloud TTS provider %r selected but WOMBAT_TTS_VOICE_ID is missing/blank "
            "(required for %r) — falling back to local TTS",
            provider,
            provider,
        )
        return _build_local_tts(config)

    try:
        primary: TTSAdapter = _construct_cloud_tts(provider, key, voice_id)
    except ImportError:
        logger.warning(
            "voice: cloud TTS provider %r selected but the 'voice-cloud' extra is not "
            "installed (`uv sync --extra voice-cloud`) — falling back to local TTS",
            provider,
            exc_info=True,
        )
        return _build_local_tts(config)

    return FallbackTTSAdapter(primary, fallback=_build_local_tts(config))


__all__ = [
    "FallbackTTSAdapter",
    "FallbackTranscriber",
    "build_transcriber",
    "build_tts_adapter",
]
