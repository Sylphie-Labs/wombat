"""TK-216 — degrade-path persona deltas acceptance criteria (DEC-37(e), Q-107(b)).

The deterministic no-model degrade path honors ONLY the two axes a template can honestly
express: ``Brevity`` (``TemplateComposer``'s wrapper variants, ``BriefComposeStage``'s
``persona_degrade_wrap``) and ``Warmth`` (one fixed greeting line on the brief degrade only).
``Directness``/``Humor`` have NO degrade variant BY RULING (a template cannot honestly hedge or
joke) — this asymmetry is pinned here, never silent.

  AC1 DEFAULT matrix (and separately ``live_persona=None``) renders BYTE-IDENTICAL to today for
      both ``TemplateComposer.render`` and ``persona_degrade_wrap``.
  AC2 BALANCED then EXPANSIVE brevity (flipped via a real ``LivePersona`` between two ``render``
      calls to prove the render-time/hot-apply read) and WARM warmth each yield their documented
      wrapper variant.
  AC3 every ``Directness``/``Humor`` level, brevity/warmth held at DEFAULT, renders
      byte-UNCHANGED — the model-path-only asymmetry, parametrized.
  Also: a degraded ``ComposeStage`` run (model raising) with a wired ``live_persona`` at
      ``brevity=BALANCED`` emits the balanced template in its ``composed_output`` artifact
      (drive-level integration, cheap).
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Transition

from tests.support.stage_context_fake import FakeModel, StageContextFake
from wombat.compose.brief_template import persona_degrade_wrap, render_brief_lines
from wombat.compose.templates import TemplateComposer
from wombat.config import WombatConfig
from wombat.domain.brief_decision_artifact import BriefBucket, BriefDecisionArtifact
from wombat.gate.models import ItemKind
from wombat.persona.live import LivePersona
from wombat.persona.matrix import (
    DEFAULT_MATRIX,
    Brevity,
    Directness,
    Humor,
    PersonaMatrix,
    Warmth,
)
from wombat.stages.artifacts import (
    COMPOSE_REQUEST,
    compose_request_to_artifact_data,
    composed_output_from_artifact_data,
)
from wombat.stages.compose import ComposeStage

_FIXED_NOW = datetime(2026, 7, 3, 8, 0, tzinfo=UTC)
_PAYLOAD = {"a": 1, "b": 2}
_ITEM_KIND = ItemKind.GENERIC
_ITEM_ID = "i-1"


def _live_persona(matrix: PersonaMatrix = DEFAULT_MATRIX) -> LivePersona:
    return LivePersona(matrix, "Steward")  # store-less (TK-243), fully in-memory


def _matrix_with(
    *,
    brevity: Brevity = DEFAULT_MATRIX.brevity,
    warmth: Warmth = DEFAULT_MATRIX.warmth,
    directness: Directness = DEFAULT_MATRIX.directness,
    humor: Humor = DEFAULT_MATRIX.humor,
) -> PersonaMatrix:
    return PersonaMatrix(
        brevity=brevity,
        warmth=warmth,
        directness=directness,
        humor=humor,
        proactivity=DEFAULT_MATRIX.proactivity,
    )


def _quiet_brief_body() -> str:
    artifact = BriefDecisionArtifact(
        bucket=BriefBucket(recap=(), conflict=(), prep=()),
        calendar_unavailable=False,
        gmail_unavailable=False,
    )
    return render_brief_lines(artifact, tz=ZoneInfo("UTC"))


def _compose_request_artifact() -> Artifact:
    return Artifact(
        kind=COMPOSE_REQUEST,
        produced_by="compose_dispatch",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=compose_request_to_artifact_data(_ITEM_ID, _ITEM_KIND, _PAYLOAD),
    )


# --------------------------------------------------------------------------------------- AC1


def test_ac1_template_composer_no_live_persona_renders_byte_identical_pin() -> None:
    composer = TemplateComposer()

    assert composer.render(_ITEM_KIND, _PAYLOAD) == "[generic] a: 1; b: 2"


def test_ac1_template_composer_default_matrix_live_persona_renders_byte_identical() -> None:
    composer = TemplateComposer(live_persona=_live_persona())

    assert composer.render(_ITEM_KIND, _PAYLOAD) == "[generic] a: 1; b: 2"


def test_ac1_persona_degrade_wrap_default_matrix_is_identity() -> None:
    body = _quiet_brief_body()

    assert persona_degrade_wrap(body, DEFAULT_MATRIX) == body


# --------------------------------------------------------------------------------------- AC2


def test_ac2_template_composer_balanced_then_expansive_via_live_persona_flip() -> None:
    """Same composer instance, matrix flipped between two ``render`` calls — proves the
    render-time (hot-apply) read, not just a constructor-frozen snapshot."""
    live_persona = _live_persona(_matrix_with(brevity=Brevity.BALANCED))
    composer = TemplateComposer(live_persona=live_persona)

    balanced = composer.render(_ITEM_KIND, _PAYLOAD)
    assert balanced == "[generic]\na: 1\nb: 2"

    live_persona.set(_matrix_with(brevity=Brevity.EXPANSIVE))
    expansive = composer.render(_ITEM_KIND, _PAYLOAD)
    assert expansive == "[generic]\na: 1\nb: 2\nThat's everything for this item."

    # BALANCED's rendering stands unchanged for the prior call — no retroactive mutation.
    assert balanced == "[generic]\na: 1\nb: 2"


def test_ac2_persona_degrade_wrap_balanced_prepends_one_fixed_header_line() -> None:
    body = _quiet_brief_body()

    wrapped = persona_degrade_wrap(body, _matrix_with(brevity=Brevity.BALANCED))

    lines = wrapped.splitlines()
    assert lines[0] == "Here's the morning brief:"
    assert "\n".join(lines[1:]) == body
    assert len(lines) == len(body.splitlines()) + 1


def test_ac2_persona_degrade_wrap_expansive_prepends_header_and_appends_closing_line() -> None:
    body = _quiet_brief_body()

    wrapped = persona_degrade_wrap(body, _matrix_with(brevity=Brevity.EXPANSIVE))

    lines = wrapped.splitlines()
    assert lines[0] == "Here's the morning brief:"
    assert lines[-1] == "That's everything for this morning."
    assert "\n".join(lines[1:-1]) == body
    assert len(lines) == len(body.splitlines()) + 2


def test_ac2_persona_degrade_wrap_warm_prepends_one_greeting_line_ahead_of_everything() -> None:
    body = _quiet_brief_body()

    wrapped = persona_degrade_wrap(body, _matrix_with(warmth=Warmth.WARM))

    lines = wrapped.splitlines()
    assert lines[0] == "Good morning!"
    assert "\n".join(lines[1:]) == body
    assert len(lines) == len(body.splitlines()) + 1


def test_ac2_persona_degrade_wrap_warm_greeting_lands_ahead_of_a_balanced_header() -> None:
    body = _quiet_brief_body()

    wrapped = persona_degrade_wrap(
        body, _matrix_with(brevity=Brevity.BALANCED, warmth=Warmth.WARM)
    )

    lines = wrapped.splitlines()
    assert lines[0] == "Good morning!"
    assert lines[1] == "Here's the morning brief:"
    assert "\n".join(lines[2:]) == body
    assert len(lines) == len(body.splitlines()) + 2


@pytest.mark.parametrize("warmth", [Warmth.RESERVED, Warmth.NEUTRAL])
def test_ac2_persona_degrade_wrap_reserved_and_neutral_add_nothing(warmth: Warmth) -> None:
    body = _quiet_brief_body()

    wrapped = persona_degrade_wrap(body, _matrix_with(warmth=warmth))

    assert wrapped == body


# --------------------------------------------------------------------------------------- AC3


_DIRECTNESS_HUMOR_SWEEP = list(itertools.product(Directness, Humor))


@pytest.mark.parametrize(("directness", "humor"), _DIRECTNESS_HUMOR_SWEEP)
def test_ac3_template_composer_directness_and_humor_never_change_output(
    directness: Directness, humor: Humor
) -> None:
    matrix = _matrix_with(directness=directness, humor=humor)
    composer = TemplateComposer(live_persona=_live_persona(matrix))

    assert composer.render(_ITEM_KIND, _PAYLOAD) == "[generic] a: 1; b: 2"


@pytest.mark.parametrize(("directness", "humor"), _DIRECTNESS_HUMOR_SWEEP)
def test_ac3_persona_degrade_wrap_directness_and_humor_never_change_output(
    directness: Directness, humor: Humor
) -> None:
    body = _quiet_brief_body()
    matrix = _matrix_with(directness=directness, humor=humor)

    assert persona_degrade_wrap(body, matrix) == body


# ----------------------------------------------------------------------- drive-level integration


async def test_degraded_compose_stage_run_with_balanced_live_persona_emits_balanced_template() -> (
    None
):
    """Cheap drive-level proof: a degraded ``ComposeStage`` run, wired with the SAME
    ``live_persona`` bootstrap shares between the stage and its ``TemplateComposer`` (Q-107(b)),
    emits the BALANCED wrapper variant in its ``composed_output`` artifact."""
    live_persona = _live_persona(_matrix_with(brevity=Brevity.BALANCED))
    model = FakeModel(raises=ConnectionError("503 Service Unavailable"))
    ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={"compose_dispatch": _compose_request_artifact()},
        model_fake=model,
    )
    config = WombatConfig(deepseek_api_key="sk-test", deepseek_base_url="https://api.deepseek.com")
    stage = ComposeStage(
        config=config,
        template_composer=TemplateComposer(live_persona=live_persona),
        live_persona=live_persona,
    )

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    text, item_id, item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert degraded is True
    assert text == "[generic]\na: 1\nb: 2"
    assert item_id == _ITEM_ID
    assert item_kind is _ITEM_KIND
