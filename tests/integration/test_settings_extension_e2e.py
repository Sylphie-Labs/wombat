"""tests/integration/test_settings_extension_e2e.py — TK-307 (DEC-67 proof ticket, EP-32).

The TK-291/TK-298 proof-module shape: ONE NEW module, ZERO src changes, pg-gated on
``WOMBAT_TEST_PG_DSN`` (module-level skip absent it — NEVER the live wombat DB, mirroring
``test_voice_conversation_context_e2e.py``'s own discipline).

AC1(a): every new/widened DEC-67 field PUT through the REAL FastAPI app (``settings_app.api.
create_app`` over a real ``SettingsStore``) round-trips: ``load_operating_params(overlay=<the
eight wombat_param_* rows read back from the store>)`` carries the eight overlaid values, and
``load_config()`` over the SAME table carries the config-tier fields (quiet pair, reply window,
spoken cap, asr model, persona axes, user name) while ``WombatConfig`` never even has a
``wombat_param_*`` field to receive the other eight rows (the admitted-schema filter, DEC-43's
``APP_EDITABLE_FIELDS`` precedent).

AC1(b): a ``LivePersona`` poll picks up humor=comedian/brevity=exhaustive/warmth=affectionate/
proactivity=eager off the SAME table; ``instruction_for``/``LivePersona.instruction`` render the
pinned DEC-67(b) comedian clause on chat/compose while draft/reflection stay humor-free at every
``Humor`` level; ``effective_urgency_threshold`` clamps eager at the floor (0.75 base -> 0.60).

AC1(c): a real ``ComposeStage`` with a ``FakeModel`` spy under the comedian matrix captures the
comedian clause verbatim in the system message for a chat-kind (``ItemKind.CHAT``) turn.

AC1(d): the untouched-pins sweep — ``DEFAULT_MATRIX`` renders byte-identical to the pre-arc live
oracles on all five mouths; ``PARAMS_APP_EDITABLE``'s key set is asserted VERBATIM and no
rating_tuner/personality_band-floor-cap/flush/presence/sweeper/dream key is reachable via any
admitted settings field; the quiet-hours ``gate_stage`` wrapper holds an immediate-voice
surfacing in-window (byte-transparent passthrough out of it); the ``LastSpokenRegister``/
``SpeechShapeStage`` construction sites inside a real ``assemble_runtime`` carry the configured
``ttl_seconds``/``max_chars`` values.

AC2: every existing pinned suite runs green alongside this module, unmodified (see the ticket's
runnable bar — this module makes zero src changes and touches no other test file).

AC3: zero live model/network calls — every model call in this module goes through ``FakeModel``;
no ANTHROPIC/provider env is read.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, time
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import psycopg
import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.model.base import ModelResponse
from fastapi.testclient import TestClient

import wombat.bootstrap as bootstrap
from tests.support.stage_context_fake import FakeModel, StageContextFake
from wombat.behavior.stages.reflection_compose import _SYSTEM_INSTRUCTION as _REFLECTION_LIVE
from wombat.compose.brief_template import brief_system_instruction as _brief_live
from wombat.compose.templates import TemplateComposer
from wombat.config import APP_EDITABLE_FIELDS, WombatConfig, load_config
from wombat.gate.models import GateAction, GateDecision, GateItem, ItemKind
from wombat.gate.trigger import effective_urgency_threshold
from wombat.integrations.gmail.draft_composer import _system_instruction as _draft_live
from wombat.params import PARAMS_APP_EDITABLE, load_operating_params
from wombat.persona.builder import Mouth, instruction_for
from wombat.persona.live import LivePersona
from wombat.persona.matrix import (
    DEFAULT_MATRIX,
    Brevity,
    Directness,
    Humor,
    PersonaMatrix,
    Proactivity,
    Warmth,
)
from wombat.settings_app.api import ADMITTED_SETTINGS_FIELDS, create_app
from wombat.settings_store import SettingsStore, ensure_schema
from wombat.sources.presence import PresenceSnapshot, PresenceState
from wombat.stages.artifacts import COMPOSE_REQUEST, compose_request_to_artifact_data
from wombat.stages.compose import ComposeStage
from wombat.stages.compose import _chat_system_instruction as _chat_live
from wombat.stages.compose import _system_instruction as _compose_live
from wombat.stages.gate_stage import GateStage
from wombat.voice.reply_context import LastSpokenRegister

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

if not _DSN:
    pytest.skip(
        "WOMBAT_TEST_PG_DSN is not set — skipping the TK-307 settings-extension e2e proof, "
        "which requires a real throwaway Postgres. Start one with:\n"
        "  docker run --rm -d --name wombat-build-tk-307 -p 5440:5432 "
        "-e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5440/postgres",
        allow_module_level=True,
    )

TOKEN = "tk-307-test-token"

_FAKE_DSN = "postgresql://fake-host/fake-db"  # never connected to — see (d)'s bootstrap checks

# DEC-67(b), persona_policy.yaml's comedian clause, pinned verbatim.
_COMEDIAN_CLAUSE = (
    "Be a constant comedian: every reply must carry at least one joke, pun, or comic riff on "
    "the subject at hand, and playful exaggeration is welcome - as long as the actual "
    "information still comes through clearly."
)

# The DEC-67(d) eight-key overlay spec, pinned verbatim (AC1(d)).
_EXPECTED_PARAMS_KEYS = frozenset(
    {
        "wombat_param_morning_brief_time",
        "wombat_param_nightly_dream_time",
        "wombat_param_urgency_threshold",
        "wombat_param_per_class_daily_ceiling",
        "wombat_param_decay_ttl_seconds",
        "wombat_param_mouth_model_timeout_seconds",
        "wombat_param_mouth_daily_token_ceiling",
        "wombat_param_mouth_max_usd_per_drive",
    }
)

# Substrings naming the custody-pinned fields DEC-67(d) deliberately left unreachable via any
# settings path (rating_tuner, personality_band's floor/cap, flush mechanics, presence hold,
# sweeper cadence, dream substrate budget).
_UNREACHABLE_SUBSTRINGS = (
    "rating_tuner",
    "personality_band",
    "floor",
    "cap",
    "flush",
    "presence",
    "sweeper",
    "dream_budget",
)


class _FakeVoiceKeyStore:
    """In-memory fake (mirrors ``tests/settings_app/test_api.py``'s own) — this module never
    exercises the voice-key vault, only the settings PUT/GET routes."""

    def get(self, provider: str) -> str | None:
        return None

    def set(self, provider: str, key: str) -> None:
        return None

    def delete(self, provider: str) -> None:
        return None


@pytest.fixture
def fresh_settings_table() -> None:
    """Drop + recreate ``wombat_settings`` on the throwaway pg, empty, for one test (mirrors
    ``tests/unit/test_config.py``/``tests/settings_app/test_api.py``'s own fixture)."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS wombat_settings CASCADE")
        conn.commit()
        ensure_schema(conn)


def _deepseek_config(**overrides: object) -> WombatConfig:
    return WombatConfig(
        deepseek_api_key="dummy-not-real-key",
        deepseek_base_url="https://x.test",
        **overrides,  # type: ignore[arg-type]
    )


# ==================================================================================== AC1(a)


def test_ac1a_put_roundtrips_the_dec67_fields_into_params_overlay_and_config(
    fresh_settings_table: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _DSN is not None
    dsn = _DSN

    put_body: dict[str, object] = {
        "wombat_persona_brevity": "exhaustive",
        "wombat_persona_warmth": "affectionate",
        "wombat_persona_humor": "comedian",
        "wombat_persona_proactivity": "eager",
        "wombat_user_name": "Jim",
        "wombat_reply_window_seconds": 250.0,
        "wombat_spoken_reply_max_chars": 900,
        "wombat_asr_model": "small",
        "wombat_quiet_start": "22:00",
        "wombat_quiet_end": "06:30",
        "wombat_param_morning_brief_time": "08:15:00",
        "wombat_param_nightly_dream_time": "03:30:00",
        "wombat_param_urgency_threshold": 0.80,
        "wombat_param_per_class_daily_ceiling": 5,
        "wombat_param_decay_ttl_seconds": 7200.0,
        "wombat_param_mouth_model_timeout_seconds": 15.0,
        "wombat_param_mouth_daily_token_ceiling": 50000,
        "wombat_param_mouth_max_usd_per_drive": 1.25,
    }

    app_store = SettingsStore(dsn)
    try:
        app = create_app(app_store, _FakeVoiceKeyStore(), TOKEN)
        client = TestClient(app)
        put_response = client.put(
            "/settings", json=put_body, headers={"X-Wombat-Token": TOKEN}
        )
        assert put_response.status_code == 200
        for key, value in put_body.items():
            assert put_response.json()["settings"][key] == value
    finally:
        app_store.close()

    # --- read the eight wombat_param_* rows back from the store (TK-302 r3's overlay-read shape)
    reader_store = SettingsStore(dsn)
    try:
        existing = reader_store.get_all()
    finally:
        reader_store.close()
    overlay = {key: existing[key] for key in PARAMS_APP_EDITABLE if key in existing}
    assert set(overlay) == set(PARAMS_APP_EDITABLE)  # every one of the eight rows landed

    op = load_operating_params(overlay=overlay)
    assert op.morning_brief_time == time(8, 15, 0)
    assert op.nightly_dream_time == time(3, 30, 0)
    assert op.urgency_threshold == pytest.approx(0.80)
    assert op.per_class_daily_ceiling == 5
    assert op.decay_ttl_seconds == pytest.approx(7200.0)
    assert op.mouth_model_timeout_seconds == pytest.approx(15.0)
    assert op.mouth_daily_token_ceiling == 50000
    assert op.mouth_max_usd_per_drive == pytest.approx(1.25)

    # --- load_config over the SAME table: config-tier fields carry, wombat_param_* rows drop ---
    monkeypatch.chdir(tmp_path)  # no repo-root .env can leak an override in
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://example.test")
    monkeypatch.setenv("WOMBAT_PG_DSN", dsn)

    config = load_config()  # must not raise despite the eight non-admitted rows sitting in-table
    assert config.wombat_persona_brevity == "exhaustive"
    assert config.wombat_persona_warmth == "affectionate"
    assert config.wombat_persona_humor == "comedian"
    assert config.wombat_persona_proactivity == "eager"
    assert config.wombat_user_name == "Jim"
    assert config.wombat_reply_window_seconds == pytest.approx(250.0)
    assert config.wombat_spoken_reply_max_chars == 900
    assert config.wombat_asr_model == "small"
    assert config.wombat_quiet_start == "22:00"
    assert config.wombat_quiet_end == "06:30"
    # The admitted-schema filter: WombatConfig has no wombat_param_* field AT ALL — the eight
    # rows sitting in the SAME table never reach it (config._SettingsTableSource filters to
    # APP_EDITABLE_FIELDS, which does not name them).
    assert "wombat_param_urgency_threshold" not in type(config).model_fields
    assert "wombat_param_urgency_threshold" not in APP_EDITABLE_FIELDS


# ==================================================================================== AC1(b)


def test_ac1b_live_persona_poll_picks_up_widened_axes_and_renders_pinned_clauses(
    fresh_settings_table: None,
) -> None:
    assert _DSN is not None
    dsn = _DSN

    setup_store = SettingsStore(dsn)
    try:
        setup_store.put(
            {
                "wombat_persona_brevity": "exhaustive",
                "wombat_persona_warmth": "affectionate",
                "wombat_persona_humor": "comedian",
                "wombat_persona_proactivity": "eager",
            }
        )
    finally:
        setup_store.close()

    poll_store = SettingsStore(dsn)
    try:
        live_persona = LivePersona(DEFAULT_MATRIX, "Steward", store=poll_store, user_name="Jim")
        live_persona.poll_settings()  # first-beat hydrate (BINDING v2.61 ruling 2)

        assert live_persona.matrix == PersonaMatrix(
            brevity=Brevity.EXHAUSTIVE,
            warmth=Warmth.AFFECTIONATE,
            directness=Directness.PLAIN,  # never PUT — stays at DEFAULT_MATRIX
            humor=Humor.COMEDIAN,
            proactivity=Proactivity.EAGER,
        )

        chat_instruction = live_persona.instruction(Mouth.CHAT)
        compose_instruction = live_persona.instruction(Mouth.COMPOSE)
        assert _COMEDIAN_CLAUSE in chat_instruction
        assert _COMEDIAN_CLAUSE in compose_instruction

        # draft/reflection stay humor-free at EVERY humor level, not just comedian.
        default_draft = instruction_for(Mouth.DRAFT, DEFAULT_MATRIX, "Steward")
        default_reflection = instruction_for(Mouth.REFLECTION, DEFAULT_MATRIX, "Steward")
        for humor_level in Humor:
            matrix = PersonaMatrix(
                brevity=DEFAULT_MATRIX.brevity,
                warmth=DEFAULT_MATRIX.warmth,
                directness=DEFAULT_MATRIX.directness,
                humor=humor_level,
                proactivity=DEFAULT_MATRIX.proactivity,
            )
            assert instruction_for(Mouth.DRAFT, matrix, "Steward") == default_draft
            assert instruction_for(Mouth.REFLECTION, matrix, "Steward") == default_reflection
    finally:
        poll_store.close()

    # effective_urgency_threshold clamps EAGER at the floor (0.75 base -> 0.60) — the file's own
    # personality_band (untouched by the overlay, which never names this field).
    file_only_op = load_operating_params()
    band = file_only_op.personality_band
    assert effective_urgency_threshold(0.75, Proactivity.EAGER, band) == pytest.approx(0.60)


# ==================================================================================== AC1(c)


async def test_ac1c_compose_stage_with_fake_model_captures_comedian_clause_for_chat_turn() -> None:
    comedian_matrix = PersonaMatrix(
        brevity=DEFAULT_MATRIX.brevity,
        warmth=DEFAULT_MATRIX.warmth,
        directness=DEFAULT_MATRIX.directness,
        humor=Humor.COMEDIAN,
        proactivity=DEFAULT_MATRIX.proactivity,
    )
    live_persona = LivePersona(comedian_matrix, "Steward", user_name="Jim")  # store-less, in-memory
    expected_instruction = instruction_for(Mouth.CHAT, comedian_matrix, "Steward", user_name="Jim")
    assert _COMEDIAN_CLAUSE in expected_instruction

    compose_stage = ComposeStage(
        config=_deepseek_config(), template_composer=TemplateComposer(), live_persona=live_persona
    )
    chat_artifact = Artifact(
        kind=COMPOSE_REQUEST,
        produced_by="compose_dispatch",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=datetime.now(UTC)),
        data=compose_request_to_artifact_data(
            "chat-1", ItemKind.CHAT, {"transcript": "tell me something"}
        ),
    )
    model = FakeModel(
        response=ModelResponse(text="haha, sure thing!", model_id="fake", finish_reason="stop")
    )
    ctx = StageContextFake(
        now_fn=lambda: datetime.now(UTC),
        last_output_map={"compose_dispatch": chat_artifact},
        model_fake=model,
    )

    await compose_stage.run(ctx)

    assert len(model.calls) == 1  # AC3: exactly one FakeModel call, zero live network calls
    system_msg, _user_msg = model.calls[0]
    assert system_msg.content == expected_instruction
    assert _COMEDIAN_CLAUSE in system_msg.content


# ==================================================================================== AC1(d)


def test_ac1d_default_matrix_byte_identity_and_params_key_set() -> None:
    assert instruction_for(Mouth.COMPOSE, DEFAULT_MATRIX, "Steward") == _compose_live("Steward")
    assert instruction_for(Mouth.BRIEF, DEFAULT_MATRIX, "Steward") == _brief_live("Steward")
    assert instruction_for(Mouth.DRAFT, DEFAULT_MATRIX, "Steward") == _draft_live("Steward")
    assert instruction_for(Mouth.REFLECTION, DEFAULT_MATRIX, "Steward") == _REFLECTION_LIVE
    assert instruction_for(
        Mouth.CHAT, DEFAULT_MATRIX, "Steward", user_name="Jim"
    ) == _chat_live("Steward", "Jim")

    assert set(PARAMS_APP_EDITABLE) == _EXPECTED_PARAMS_KEYS
    for key in ADMITTED_SETTINGS_FIELDS:
        for substring in _UNREACHABLE_SUBSTRINGS:
            assert substring not in key, (
                f"{key!r} unexpectedly reachable via settings (matches {substring!r})"
            )


class _FixedNow(datetime):
    """A ``datetime`` stand-in whose ``now(tz)`` always returns the same fixed instant —
    monkeypatched over ``bootstrap.datetime`` so the quiet-hours wrapper's own ``datetime.now(tz)``
    call resolves deterministically (mirrors ``tests/unit/test_bootstrap.py``'s own technique for
    the SAME wrapper)."""

    _fixed: datetime

    @classmethod
    def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
        return cls._fixed


def _fixed_now_at(hour: int, minute: int) -> type[_FixedNow]:
    fixed = _FixedNow(2026, 1, 1, hour, minute, tzinfo=UTC)

    class _Bound(_FixedNow):
        pass

    _Bound._fixed = fixed
    return _Bound


async def test_ac1d_quiet_hours_wrapper_and_construction_sites_carry_configured_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received_presence: list[PresenceSnapshot | None] = []
    canned_decision = GateDecision(action=GateAction.HOLD, items=())

    def _fake_make_gate_evaluator(**kwargs: object) -> object:
        async def _evaluate(
            items: list[GateItem], presence: PresenceSnapshot | None
        ) -> GateDecision:
            received_presence.append(presence)
            return canned_decision

        return _evaluate

    monkeypatch.setattr(bootstrap, "make_gate_evaluator", _fake_make_gate_evaluator)

    op = load_operating_params()
    config = WombatConfig(
        deepseek_api_key="dummy-not-real-key",
        deepseek_base_url="https://x.test",
        wombat_quiet_start="22:00",
        wombat_quiet_end="07:00",
        wombat_reply_window_seconds=333.0,
        wombat_spoken_reply_max_chars=777,
    )
    bundle = bootstrap.assemble_runtime(
        config=config,
        dsn=_FAKE_DSN,  # never connected to — replay_pending=False keeps this connection-free
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )
    graph = bundle.pathways.get(bundle.drain_pathway_id)

    # --- the quiet-hours gate_stage wrapper (RULING v2.172 r6): holds in-window, passes through
    # out of it ------------------------------------------------------------------------------
    gate_stage = cast(GateStage, graph.get("gate"))
    item = GateItem(item_id="i1", item_kind=ItemKind.GENERIC, created_at=0.0, payload={})
    active_presence = PresenceSnapshot(
        state=PresenceState.ACTIVE, confidence=1.0, idle_ms=0, taken_at=0.0
    )

    monkeypatch.setattr(bootstrap, "datetime", _fixed_now_at(23, 30))  # inside 22:00-07:00
    in_window_decision = await gate_stage._evaluate([item], active_presence)
    assert received_presence[-1] is None  # forced to None -> the canonical presence-hold path
    assert in_window_decision == canned_decision

    monkeypatch.setattr(bootstrap, "datetime", _fixed_now_at(12, 0))  # outside the window
    out_of_window_decision = await gate_stage._evaluate([item], active_presence)
    assert received_presence[-1] is active_presence  # byte-transparent passthrough
    assert out_of_window_decision == canned_decision

    # --- LastSpokenRegister/SpeechShapeStage construction sites carry the configured values ---
    speech_shape_stage = graph.get("speech_shape")
    assert speech_shape_stage._max_chars == 777

    speak_stage = graph.get("speak")
    register = speak_stage._on_spoken.__self__
    assert isinstance(register, LastSpokenRegister)
    assert register._ttl_seconds == pytest.approx(333.0)
