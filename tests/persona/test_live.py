"""TK-209 — LivePersona acceptance criteria (EP-33, DEC-34 Jim authority + DEC-37(g)).

  AC1 identity-through-reroute (the mouth-level byte-identity itself is TK-207's own test): a
      default-config LivePersona's instruction() matches the live oracles for all four mouths.
  AC2 hot-apply: set() swaps the in-memory matrix immediately — instruction() reflects it on the
      very next call, no restart — AND persists the five persona keys to the settings file via a
      read-modify-write that preserves every other pre-existing key.
  AC3 degrade: a persistence failure leaves the in-memory matrix applied, logs exactly ONE loud
      WARNING, and set() never raises.
  AC4 beat pickup: poll_settings_file() reloads + swaps the matrix on a changed mtime; an
      unchanged mtime never even re-reads the file; ANY exception is caught, logged loud, and
      never raised, leaving the current in-memory matrix standing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from wombat.behavior.stages.reflection_compose import _SYSTEM_INSTRUCTION as REFLECTION_LIVE
from wombat.compose.brief_template import brief_system_instruction as brief_live
from wombat.integrations.gmail.draft_composer import _system_instruction as draft_live
from wombat.persona.builder import Mouth
from wombat.persona.live import LivePersona
from wombat.persona.matrix import DEFAULT_MATRIX, Directness, Humor, PersonaMatrix, Warmth
from wombat.stages.compose import _system_instruction as compose_live

_DRY_MATRIX = PersonaMatrix(
    brevity=DEFAULT_MATRIX.brevity,
    warmth=DEFAULT_MATRIX.warmth,
    directness=DEFAULT_MATRIX.directness,
    humor=Humor.DRY,
    proactivity=DEFAULT_MATRIX.proactivity,
)


def _live_persona(tmp_path: Path, name: str = "Steward") -> LivePersona:
    return LivePersona(DEFAULT_MATRIX, name, settings_path=str(tmp_path / "wombat.settings.json"))


# --------------------------------------------------------------------------------------- AC1


def test_default_matrix_renders_byte_identical_to_live_oracles_for_all_four_mouths(
    tmp_path: Path,
) -> None:
    live_persona = _live_persona(tmp_path)

    assert live_persona.instruction(Mouth.COMPOSE) == compose_live("Steward")
    assert live_persona.instruction(Mouth.BRIEF) == brief_live("Steward")
    assert live_persona.instruction(Mouth.DRAFT) == draft_live("Steward")
    assert live_persona.instruction(Mouth.REFLECTION) == REFLECTION_LIVE


# --------------------------------------------------------------------------------------- AC2


def test_set_applies_the_new_matrix_immediately_no_restart(tmp_path: Path) -> None:
    live_persona = _live_persona(tmp_path)

    before = live_persona.instruction(Mouth.COMPOSE)
    live_persona.set(_DRY_MATRIX)
    after = live_persona.instruction(Mouth.COMPOSE)

    assert before != after
    assert live_persona.matrix == _DRY_MATRIX


def test_set_persists_the_five_persona_keys_preserving_other_keys(tmp_path: Path) -> None:
    settings_path = tmp_path / "wombat.settings.json"
    settings_path.write_text(
        json.dumps({"wombat_assistant_name": "Marvin", "wombat_tts_provider": "fish"}),
        encoding="utf-8",
    )
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", settings_path=str(settings_path))
    matrix = PersonaMatrix(
        brevity=DEFAULT_MATRIX.brevity,
        warmth=Warmth.WARM,
        directness=DEFAULT_MATRIX.directness,
        humor=Humor.DRY,
        proactivity=DEFAULT_MATRIX.proactivity,
    )

    live_persona.set(matrix)

    saved = json.loads(settings_path.read_text(encoding="utf-8"))
    assert saved["wombat_assistant_name"] == "Marvin"  # pre-existing key preserved verbatim
    assert saved["wombat_tts_provider"] == "fish"
    assert saved["wombat_persona_brevity"] == "terse"
    assert saved["wombat_persona_warmth"] == "warm"
    assert saved["wombat_persona_directness"] == "plain"
    assert saved["wombat_persona_humor"] == "dry"
    assert saved["wombat_persona_proactivity"] == "balanced"


def test_set_creates_the_settings_file_when_absent(tmp_path: Path) -> None:
    settings_path = tmp_path / "wombat.settings.json"
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", settings_path=str(settings_path))

    live_persona.set(DEFAULT_MATRIX)

    assert settings_path.exists()
    saved = json.loads(settings_path.read_text(encoding="utf-8"))
    assert saved["wombat_persona_humor"] == "none"


# --------------------------------------------------------------------------------------- AC3


def test_set_write_failure_still_applies_in_memory_one_warning_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    live_persona = _live_persona(tmp_path)

    def _boom(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError("read-only filesystem (simulated)")

    monkeypatch.setattr(Path, "write_text", _boom)

    with caplog.at_level(logging.WARNING, logger="wombat.persona.live"):
        live_persona.set(_DRY_MATRIX)  # must not raise

    assert live_persona.matrix == _DRY_MATRIX  # in-memory still applied
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_set_malformed_existing_file_still_applies_in_memory_one_warning_never_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings_path = tmp_path / "wombat.settings.json"
    settings_path.write_text("{not valid json", encoding="utf-8")
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", settings_path=str(settings_path))

    with caplog.at_level(logging.WARNING, logger="wombat.persona.live"):
        live_persona.set(_DRY_MATRIX)  # must not raise

    assert live_persona.matrix == _DRY_MATRIX
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


# --------------------------------------------------------------------------------------- AC4


def test_poll_settings_file_reloads_and_swaps_on_changed_mtime(tmp_path: Path) -> None:
    settings_path = tmp_path / "wombat.settings.json"
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", settings_path=str(settings_path))

    # External write AFTER construction (the settings-app path, TK-197/TK-200) — mtime goes from
    # None (file absent at construction) to a real value.
    settings_path.write_text(
        json.dumps(
            {
                "wombat_persona_brevity": "terse",
                "wombat_persona_warmth": "reserved",
                "wombat_persona_directness": "gentle",
                "wombat_persona_humor": "dry",
                "wombat_persona_proactivity": "balanced",
            }
        ),
        encoding="utf-8",
    )

    live_persona.poll_settings_file()

    assert live_persona.matrix.humor is Humor.DRY
    assert live_persona.matrix.directness is Directness.GENTLE
    # AC4: the NEXT render uses the reloaded matrix.
    assert live_persona.instruction(Mouth.COMPOSE) != compose_live("Steward")


def test_poll_settings_file_partial_keys_keep_the_current_value_for_the_rest(
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / "wombat.settings.json"
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", settings_path=str(settings_path))

    settings_path.write_text(json.dumps({"wombat_persona_humor": "dry"}), encoding="utf-8")

    live_persona.poll_settings_file()

    assert live_persona.matrix.humor is Humor.DRY
    assert live_persona.matrix.warmth is DEFAULT_MATRIX.warmth  # untouched, stays default


def test_poll_settings_file_no_mtime_change_never_reads_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = tmp_path / "wombat.settings.json"
    settings_path.write_text(json.dumps({"wombat_persona_humor": "dry"}), encoding="utf-8")
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", settings_path=str(settings_path))

    def _boom(*args: object, **kwargs: object) -> str:
        raise AssertionError("must not read the file when mtime is unchanged")

    monkeypatch.setattr(Path, "read_text", _boom)

    live_persona.poll_settings_file()  # no exception proves the early-return path fired

    assert live_persona.matrix == DEFAULT_MATRIX  # unread, unchanged


def test_poll_settings_file_malformed_json_logs_one_warning_never_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings_path = tmp_path / "wombat.settings.json"
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", settings_path=str(settings_path))

    settings_path.write_text("{not valid json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="wombat.persona.live"):
        live_persona.poll_settings_file()  # must not raise

    assert live_persona.matrix == DEFAULT_MATRIX
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_poll_settings_file_unknown_axis_value_logs_one_warning_never_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings_path = tmp_path / "wombat.settings.json"
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", settings_path=str(settings_path))

    settings_path.write_text(json.dumps({"wombat_persona_humor": "nonsense"}), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="wombat.persona.live"):
        live_persona.poll_settings_file()  # must not raise

    assert live_persona.matrix == DEFAULT_MATRIX  # unchanged — the bad reload never applied
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_poll_settings_file_absent_file_after_a_prior_write_never_raises(tmp_path: Path) -> None:
    """The file existed at construction, then vanished — mtime goes from a real value to None."""
    settings_path = tmp_path / "wombat.settings.json"
    settings_path.write_text(json.dumps({"wombat_persona_humor": "dry"}), encoding="utf-8")
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", settings_path=str(settings_path))
    settings_path.unlink()

    live_persona.poll_settings_file()  # must not raise

    assert live_persona.matrix == DEFAULT_MATRIX  # unchanged — no crash on a vanished file
