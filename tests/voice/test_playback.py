"""TK-191 acceptance criteria (playback half) — ``AudioPlayer`` protocol + ``WinsoundPlayer``
(EP-31, CST-1).

AC3 (clean-checkout import bar / lazy winsound; construction + play on win32):
``test_playback_module_imports_without_winsound_installed``,
``test_winsound_player_constructs_on_win32``,
``test_winsound_player_play_calls_winsound_playsound_with_snd_memory``.

No audible playback in tests — ``winsound.PlaySound`` itself is monkeypatched in the play test.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Sequence
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from types import ModuleType

import pytest

from wombat.voice.playback import AudioPlayer, WinsoundPlayer


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


def test_playback_module_imports_without_winsound_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: importing ``wombat.voice.playback`` never touches ``winsound`` — only constructing
    ``WinsoundPlayer`` does."""
    _simulate_absent(monkeypatch, "winsound")
    assert "winsound" not in sys.modules
    importlib.reload(importlib.import_module("wombat.voice.playback"))
    assert "winsound" not in sys.modules


@pytest.mark.skipif(sys.platform != "win32", reason="winsound is Windows-only (CST-1)")
def test_winsound_player_constructs_on_win32() -> None:
    """AC3: on Windows, ``winsound`` is stdlib-satisfied, so construction succeeds."""
    player: AudioPlayer = WinsoundPlayer()
    assert isinstance(player, WinsoundPlayer)


@pytest.mark.skipif(sys.platform != "win32", reason="winsound is Windows-only (CST-1)")
def test_winsound_player_play_calls_winsound_playsound_with_snd_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: ``play()`` reaches ``winsound.PlaySound`` with the ``SND_MEMORY`` flag and the raw
    bytes — monkeypatched, so no audible playback happens in tests."""
    import winsound

    calls: list[tuple[bytes, int]] = []
    monkeypatch.setattr(
        winsound, "PlaySound", lambda sound, flags: calls.append((sound, flags))
    )

    player = WinsoundPlayer()
    player.play(b"RIFF....WAVEfmt ")

    assert calls == [(b"RIFF....WAVEfmt ", winsound.SND_MEMORY)]
