"""TK-191 acceptance criteria — Fish Audio cloud TTS pattern-setter (EP-31, Q-100, Q-104).

AC1 (success + request shape + exactly-once playback): ``test_speak_sends_expected_request_and_
plays_returned_bytes_exactly_once``, ``test_fish_audio_tts_adapter_satisfies_ttsadapter_
protocol``.
AC2 (transport/player failure raises, then SpeakSink end-to-end degrade, TK-165/CON-3 parity):
``test_speak_raises_on_transport_or_player_failure``,
``test_speak_sink_degrades_to_terminal_on_adapter_failure_text_unaffected``.
AC3 (clean-checkout import bar / lazy httpx+winsound): ``test_tts_module_imports_without_httpx_
installed``, ``test_fish_audio_tts_adapter_construction_with_default_transport_raises_without_
httpx``.

TK-326 (DEC-71a/DEC-72a): every construction now passes ``model="s2.1-pro"`` — the request-shape
assertion in ``test_speak_sends_expected_request_and_plays_returned_bytes_exactly_once`` proves the
``model`` HTTP header rides alongside the untouched ``Authorization`` header and the JSON body
stays byte-identical.

TK-329 (DEC-72f) adds ONE arming-var-gated LIVE ear-proof:
``test_live_fish_speaks_one_pinned_expressive_utterance`` — armed ONLY when
``WOMBAT_TEST_FISH_LIVE=1`` AND a real ``WOMBAT_FISH_API_KEY``/``WOMBAT_TTS_VOICE_ID`` (Jim's
reference id) resolve via ``load_config()``; LOUD-SKIPS otherwise (the ``_LIVE_ENV`` idiom
precedent: ``tests/integration/test_capability_honesty_live.py``). Speaks exactly one pinned
utterance through the REAL transport/player — costs API credit, NEVER runs in the plain suite.

Every OTHER test rides a fake ``VoiceTransport`` + fake ``AudioPlayer`` — ZERO live network calls
and ZERO real audio playback (DEF-7).
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from types import ModuleType

import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Degraded

from tests.support.stage_context_fake import StageContextFake
from wombat.config import ConfigurationError, load_config
from wombat.gate.models import ItemKind
from wombat.sinks.speak import SpeakSink
from wombat.sinks.tts_adapter import TTSAdapter
from wombat.stages.artifacts import (
    COMPOSED_OUTPUT,
    composed_output_to_artifact_data,
    spoken_output_from_artifact_data,
)
from wombat.voice.transport import VoiceTransport, VoiceTransportError
from wombat.voice.tts import FISH_AUDIO_TTS_URL, FishAudioTTSAdapter

_WAV_BYTES = b"RIFF....WAVEfmt returned-audio"


class _RecordingFakeTransport:
    """A fake ``VoiceTransport`` that records the ONE call made to it and returns canned WAV
    bytes (AC1) — never touches the network (DEF-7)."""

    def __init__(self, *, status_code: int = 200, body: bytes = _WAV_BYTES) -> None:
        self._status_code = status_code
        self._body = body
        self.calls: list[dict[str, object]] = []

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
        self.calls.append({"url": url, "headers": headers, "content": content, "json": json})
        return self._status_code, self._body


class _RaisingFakeTransport:
    """A fake ``VoiceTransport`` that simulates the real ``HttpxVoiceTransport`` non-2xx
    contract: raises ``VoiceTransportError`` rather than returning a failure status (AC2)."""

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
        raise VoiceTransportError(f"voice transport POST {url} returned 401: 'unauthorized'")


class _RecordingFakePlayer:
    """A fake ``AudioPlayer`` that records every ``play()`` call (AC1) — never touches real audio
    hardware."""

    def __init__(self) -> None:
        self.calls: list[bytes] = []

    def play(self, wav_bytes: bytes) -> None:
        self.calls.append(wav_bytes)


class _RaisingFakePlayer:
    """A fake ``AudioPlayer`` whose ``play()`` always raises (AC2)."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.calls: list[bytes] = []

    def play(self, wav_bytes: bytes) -> None:
        self.calls.append(wav_bytes)
        raise self._exc


# --- AC1: success + request shape + exactly-once playback --------------------------------------


def test_speak_sends_expected_request_and_plays_returned_bytes_exactly_once() -> None:
    transport = _RecordingFakeTransport()
    player = _RecordingFakePlayer()
    adapter = FishAudioTTSAdapter(
        "fish-secret",
        voice_id="voice-abc123",
        model="s2.1-pro",
        transport=transport,
        player=player,
    )

    adapter.speak("You have a new alert.")

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == FISH_AUDIO_TTS_URL
    assert call["headers"] == {"Authorization": "Bearer fish-secret", "model": "s2.1-pro"}
    assert call["json"] == {
        "text": "You have a new alert.",
        "reference_id": "voice-abc123",
        "format": "wav",
    }
    assert player.calls == [_WAV_BYTES]


def test_fish_audio_tts_adapter_satisfies_ttsadapter_protocol() -> None:
    """Structural ``TTSAdapter`` conformance via a typed assignment (mypy-checked) — the same
    idiom TK-189's ``DeepgramTranscriber`` test uses."""
    transport = _RecordingFakeTransport()
    player = _RecordingFakePlayer()
    adapter: TTSAdapter = FishAudioTTSAdapter(
        "fish-secret",
        voice_id="voice-abc123",
        model="s2.1-pro",
        transport=transport,
        player=player,
    )
    adapter.speak("hello")
    assert player.calls == [_WAV_BYTES]


# --- AC2: transport/player failure raises, then SpeakSink end-to-end degrade -------------------


@pytest.mark.parametrize(
    ("transport", "player"),
    [
        pytest.param(_RaisingFakeTransport(), _RecordingFakePlayer(), id="transport-failure"),
        pytest.param(
            _RecordingFakeTransport(),
            _RaisingFakePlayer(RuntimeError("playback device busy")),
            id="player-failure",
        ),
    ],
)
def test_speak_raises_on_transport_or_player_failure(
    transport: VoiceTransport, player: _RecordingFakePlayer | _RaisingFakePlayer
) -> None:
    adapter = FishAudioTTSAdapter(
        "fish-secret",
        voice_id="voice-abc123",
        model="s2.1-pro",
        transport=transport,
        player=player,
    )
    with pytest.raises(Exception):  # noqa: B017 — either VoiceTransportError or RuntimeError
        adapter.speak("hello")


_FIXED_NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)
_ITEM_ID = "gate-item-1"
_ITEM_KIND = ItemKind.GENERIC
_TEXT = "You have a new alert."


def _composed_output_artifact() -> Artifact:
    return Artifact(
        kind=COMPOSED_OUTPUT,
        produced_by="compose",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=composed_output_to_artifact_data(_TEXT, _ITEM_ID, _ITEM_KIND, False),
    )


@pytest.mark.parametrize(
    ("transport", "player"),
    [
        pytest.param(_RaisingFakeTransport(), _RecordingFakePlayer(), id="transport-failure"),
        pytest.param(
            _RecordingFakeTransport(),
            _RaisingFakePlayer(RuntimeError("playback device busy")),
            id="player-failure",
        ),
    ],
)
async def test_speak_sink_degrades_to_terminal_on_adapter_failure_text_unaffected(
    transport: VoiceTransport, player: _RecordingFakePlayer | _RaisingFakePlayer
) -> None:
    """AC2 end-to-end: the real ``SpeakSink`` wired to a ``FishAudioTTSAdapter`` whose transport
    or player fails degrades to a terminal ``Degraded(to=None)`` carrying ``spoken=False,
    degraded=True`` — the composed text itself is untouched (TK-165 parity, CON-3)."""
    adapter = FishAudioTTSAdapter(
        "fish-secret",
        voice_id="voice-abc123",
        model="s2.1-pro",
        transport=transport,
        player=player,
    )
    stage = SpeakSink(voice_enabled=True, adapter=adapter)
    compose_artifact = _composed_output_artifact()
    snapshot = compose_artifact.model_copy(deep=True)
    ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW, last_output_map={"compose": compose_artifact}
    )

    result = await stage.run(ctx)

    assert isinstance(result, Degraded)
    assert result.to is None
    assert result.reason
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is False
    assert degraded is True
    assert compose_artifact == snapshot  # the composed text wire artifact is untouched


# --- AC3: clean-checkout import bar --------------------------------------------------------------


class _BlockedFinder(MetaPathFinder):
    """A meta-path finder that fails the import of one named module (and its submodules)."""

    def __init__(self, blocked: str) -> None:
        self._blocked = blocked

    def find_spec(
        self, fullname: str, path: Sequence[str] | None, target: ModuleType | None = None
    ) -> ModuleSpec | None:
        if fullname == self._blocked or fullname.startswith(f"{self._blocked}."):
            raise ModuleNotFoundError(f"No module named {fullname!r} (simulated absence, TK-202)")
        return None


def _simulate_absent(monkeypatch: pytest.MonkeyPatch, module_name: str) -> None:
    """Simulate ``module_name`` being genuinely not installed (TK-202/Q-103), robust to the
    module actually being present."""
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockedFinder(module_name), *sys.meta_path])


def test_tts_module_imports_without_httpx_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3: importing ``wombat.voice.tts`` never touches ``httpx`` — only constructing the
    default ``HttpxVoiceTransport`` does."""
    _simulate_absent(monkeypatch, "httpx")
    assert "httpx" not in sys.modules
    importlib.reload(importlib.import_module("wombat.voice.tts"))
    assert "httpx" not in sys.modules


def test_fish_audio_tts_adapter_construction_with_default_transport_raises_without_httpx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: constructing a ``FishAudioTTSAdapter`` WITHOUT an explicit ``transport`` (the default
    arg, which lazily builds a real ``HttpxVoiceTransport``) raises ``ImportError`` when the
    ``voice-cloud`` extra is absent — the real, unmocked lazy-import-failure path. An explicit
    ``player`` fake is supplied so only the transport's lazy import is exercised."""
    _simulate_absent(monkeypatch, "httpx")
    with pytest.raises(ImportError):
        FishAudioTTSAdapter(
            "fish-secret",
            voice_id="voice-abc123",
            model="s2.1-pro",
            player=_RecordingFakePlayer(),
        )


# --- TK-329 (DEC-72f): the armed LIVE ear-proof --------------------------------------------------

_LIVE_ENV = "WOMBAT_TEST_FISH_LIVE"

# Pinned per DEC-72f — Jim's operator ear-check judges [break]/[long-break] efficacy on s2.1-pro
# by listening to exactly this utterance; if the pause markers prove inert by ear they drop at
# recalibration (recorded, not guessed).
_LIVE_UTTERANCE = (
    "[soft tone] Your first meeting is at nine. [break] Nothing else needs you before then."
)


def _missing_fish_live_requirements() -> tuple[str, ...]:
    """What's missing to arm the live smoke, resolved LAZILY at each test's SETUP time via the
    ``skipif`` STRING condition below — never at import/collection time (mirrors ``tests/
    integration/test_capability_honesty_live.py``'s ``_missing_live_requirements`` exactly).
    Short-circuits before ever calling ``load_config()`` when ``WOMBAT_TEST_FISH_LIVE`` itself is
    unset (the default, unarmed case)."""
    if not os.environ.get(_LIVE_ENV):
        return (_LIVE_ENV,)
    missing: list[str] = []
    try:
        config = load_config()
    except ConfigurationError:
        missing.append("WOMBAT_FISH_API_KEY/WOMBAT_TTS_VOICE_ID (load_config() failed)")
    else:
        if config.wombat_fish_api_key is None or not (
            config.wombat_fish_api_key.get_secret_value().strip()
        ):
            missing.append("WOMBAT_FISH_API_KEY")
        if not (config.wombat_tts_voice_id or "").strip():
            missing.append("WOMBAT_TTS_VOICE_ID")
    return tuple(missing)


def _fish_live_unarmed() -> bool:
    """The ``skipif`` condition, evaluated by pytest as a STRING at each item's SETUP time — runs
    strictly before any fixture is instantiated."""
    return bool(_missing_fish_live_requirements())


_requires_fish_live = pytest.mark.skipif(
    "_fish_live_unarmed()",
    reason=(
        f"missing {_LIVE_ENV} and/or WOMBAT_FISH_API_KEY/WOMBAT_TTS_VOICE_ID — skipping the live "
        "Fish ear-proof (TK-329, DEC-72f). Export WOMBAT_TEST_FISH_LIVE=1 plus real creds (env "
        "or repo-root .env) to arm this harness — costs API credit, NEVER runs in the plain suite."
    ),
)


@_requires_fish_live
def test_live_fish_speaks_one_pinned_expressive_utterance() -> None:
    """DEC-72f: ONE armed live speak of the pinned utterance through the REAL
    ``FishAudioTTSAdapter`` (real transport, real playback, ``config.wombat_fish_model`` — the
    pinned ``s2.1-pro`` default unless overridden). Jim's ear-check on [break]/[long-break]
    efficacy on s2.1-pro is the operator step; this smoke only proves the call completes without
    raising. Costs API credit — gated behind ``WOMBAT_TEST_FISH_LIVE``, never in the plain suite."""
    config = load_config()
    api_key = config.wombat_fish_api_key
    assert api_key is not None
    adapter = FishAudioTTSAdapter(
        api_key.get_secret_value(),
        voice_id=config.wombat_tts_voice_id or "",
        model=config.wombat_fish_model,
    )

    adapter.speak(_LIVE_UTTERANCE)
