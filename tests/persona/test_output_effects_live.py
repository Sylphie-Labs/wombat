"""TK-210 — Output-EFFECT verification (EP-33's DONE-BAR, DEC-37, Q-107(c)).

Jim's frame: verify the personality by its OUTPUT EFFECTS, not prompt deltas — prompt-delta
verification is TK-207's own layer (``tests/persona/test_builder.py``), a non_goal here. This is
a LIVE harness: it samples REAL DeepSeek mouth completions per matrix level over the FIXED
payload set in ``eval_fixtures.py`` and asserts MEASURABLE properties under the recorded verdict
rule (``eval_fixtures.SAMPLES_PER_LEVEL`` / ``majority_verdict`` / ``median_response_length``).

GATE (AC1, mirrors ``tests/integrations/gmail/test_auth.py``'s ``_requires_live_gmail`` idiom):
every LIVE test below is skipped loud, naming exactly what's missing, unless
``WOMBAT_TEST_PERSONA_EVAL_LIVE=1`` AND real ``DEEPSEEK_API_KEY``/``DEEPSEEK_BASE_URL`` creds are
resolvable (via ``load_config()`` — env or repo-root ``.env``, mirroring ``ComposeStage``'s AC3
construction-time check so a blank-string value is caught too, not just an absent var). The
default CI lane (plain ``pytest`` without the flag) never executes a network call.

TWO tests are deliberately UNGATED (always run, zero network) because they are non-live proofs
the briefing calls out explicitly:
  - the AC3 no-placebo TRIP-WIRE proof: a verdict-rule helper fed data it cannot honestly
    separate MUST raise, naming the axis — proven here as a plain unit test of the helper, per
    the briefing's own "this proof can live as a small non-live unit test" instruction.
  - the proactivity EXEMPTION pin: proactivity is exempt from live sampling (its effect is
    deterministic at the gate, proven by TK-215's tests, not this harness) — a trivial,
    deterministic assertion that ``instruction_for`` output is byte-identical across proactivity
    levels stands in as its (non-live) proof here.

NO automatic tuning from results (EP-35/DEF-8 non_goal) — this module only asserts; it never
writes ``planning/contract.yaml`` or raises a governance issue itself. Any axis that fails its
measurability bar on a live run is the architect's follow-up act, not this harness's.
"""

from __future__ import annotations

import os
from dataclasses import replace

import pytest
from cogworx.cost.budget import BudgetPolicy
from cogworx.model.base import ChatMessage, Model
from cogworx.model.registry import build_model

from tests.persona.eval_fixtures import (
    BRIEF_NON_URGENT_FIXTURES,
    COMPOSE_NON_URGENT_FIXTURES,
    COMPOSE_URGENT_FIXTURE,
    DRAFT_TASK_TEXT,
    REFLECTION_TASK_TEXT,
    SAMPLES_PER_LEVEL,
    assert_axis_separates_by_majority,
    assert_lengths_monotone_increasing,
    assert_majority_absent_at_every_level,
    assert_no_forbidden_terms,
    has_greeting,
    has_hedge,
    has_humor_aside,
)
from wombat.bootstrap import _deepseek_spec
from wombat.compose.templates import format_payload_fields
from wombat.config import ConfigurationError, load_config
from wombat.gate.models import ItemKind
from wombat.persona.builder import Mouth, instruction_for
from wombat.persona.matrix import (
    DEFAULT_MATRIX,
    Brevity,
    Directness,
    Humor,
    PersonaMatrix,
    Proactivity,
    Warmth,
)

_ASSISTANT_NAME = "Steward"

# --------------------------------------------------------------------------------------------
# The gate (AC1).
# --------------------------------------------------------------------------------------------

_LIVE_ENV = "WOMBAT_TEST_PERSONA_EVAL_LIVE"


def _missing_live_requirements() -> tuple[str, ...]:
    """What's missing to arm the live eval, resolved once at collection time. Creds are resolved
    via ``load_config()`` (env or repo-root ``.env``, TK-1's precedence) rather than a raw
    ``os.environ`` probe — mirroring ``ComposeStage``'s AC3 construction-time check, this also
    catches a blank-string value pydantic-settings would otherwise accept."""
    missing: list[str] = []
    if not os.environ.get(_LIVE_ENV):
        missing.append(_LIVE_ENV)
    try:
        config = load_config()
    except ConfigurationError:
        missing.append("DEEPSEEK_API_KEY/DEEPSEEK_BASE_URL (load_config() failed)")
    else:
        if not config.deepseek_api_key.get_secret_value().strip():
            missing.append("DEEPSEEK_API_KEY")
        if not config.deepseek_base_url.strip():
            missing.append("DEEPSEEK_BASE_URL")
    return tuple(missing)


_MISSING_LIVE_REQUIREMENTS = _missing_live_requirements()

_requires_live_persona_eval = pytest.mark.skipif(
    bool(_MISSING_LIVE_REQUIREMENTS),
    reason=(
        f"missing: {', '.join(_MISSING_LIVE_REQUIREMENTS)} — skipping the live persona "
        f"output-effect eval (TK-210). Export {_LIVE_ENV}=1 plus real DEEPSEEK_API_KEY/"
        "DEEPSEEK_BASE_URL creds (env or repo-root .env) to arm this harness."
    ),
)


@pytest.fixture(scope="session")
def live_model() -> Model:
    """The real DeepSeek model, built ONCE per test session via the SAME descriptor
    ``bootstrap.py`` registers for the drain-side profile (``_deepseek_spec``) — the briefing's
    verified seam. A permissive, default ``BudgetPolicy`` guard (layer-1 cog-worx policy; this
    harness sets no ceiling of its own)."""
    config = load_config()
    spec = _deepseek_spec(config)
    return build_model(spec, guard=BudgetPolicy().new_guard())


async def _sample(model: Model, mouth: Mouth, matrix: PersonaMatrix, user_content: str) -> str:
    """One live completion for ``mouth`` at ``matrix``, over ``user_content`` (mirrors each live
    mouth's own ``ChatMessage`` shape — system=``instruction_for``, user=the mouth-specific
    content built by the caller)."""
    system = instruction_for(mouth, matrix, _ASSISTANT_NAME)
    response = await model.complete(
        messages=[
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=user_content),
        ]
    )
    return response.text or ""


def _compose_user_content(kind: ItemKind, payload: dict[str, object]) -> str:
    """Byte-for-byte the same shape ``ComposeStage.run`` builds (compose.py:131-137)."""
    return f"item_kind: {kind.value}\n{format_payload_fields(payload)}"


async def _sample_compose_fixtures(
    model: Model,
    matrix: PersonaMatrix,
    fixtures: tuple[tuple[ItemKind, dict[str, object]], ...] = COMPOSE_NON_URGENT_FIXTURES,
) -> tuple[str, ...]:
    """One live COMPOSE completion per fixture in ``fixtures`` (default: the three non-urgent
    fixtures — ``SAMPLES_PER_LEVEL`` samples), at ``matrix``."""
    samples = []
    for kind, payload in fixtures:
        content = _compose_user_content(kind, payload)
        samples.append(await _sample(model, Mouth.COMPOSE, matrix, content))
    return tuple(samples)


async def _sample_brief_fixtures(model: Model, matrix: PersonaMatrix) -> tuple[str, ...]:
    """One live BRIEF completion per ``BRIEF_NON_URGENT_FIXTURES`` body, at ``matrix``."""
    samples = []
    for body in BRIEF_NON_URGENT_FIXTURES:
        samples.append(await _sample(model, Mouth.BRIEF, matrix, body))
    return tuple(samples)


async def _sample_repeated(
    model: Model, mouth: Mouth, matrix: PersonaMatrix, user_content: str
) -> tuple[str, ...]:
    """``SAMPLES_PER_LEVEL`` repeated live completions of the SAME fixed ``user_content`` — the
    DRAFT/REFLECTION shape (one representative task text, sampled ``SAMPLES_PER_LEVEL`` times)."""
    samples = []
    for _ in range(SAMPLES_PER_LEVEL):
        samples.append(await _sample(model, mouth, matrix, user_content))
    return tuple(samples)


# --------------------------------------------------------------------------------------------
# AC3 no-placebo trip-wire proof — NON-live, zero network (proves the mechanism, not an axis).
# --------------------------------------------------------------------------------------------


def test_tripwire_fires_and_names_the_axis_when_the_verdict_rule_cannot_separate() -> None:
    """Force one axis unmeasurable by neutering its samples (both levels render identically) and
    observe the failure NAMES the axis (TK-210 AC3) — never a skip, never a silent pass."""
    identical_samples = ("the reply is the same either way", "still the same", "unchanged again")

    with pytest.raises(AssertionError, match="humor axis UNMEASURED") as exc_info:
        assert_axis_separates_by_majority(
            axis="humor",
            present_level="dry",
            present_samples=identical_samples,
            absent_level="none",
            absent_samples=identical_samples,
            predicate=has_humor_aside,
        )
    assert "humor" in str(exc_info.value)

    with pytest.raises(AssertionError, match="brevity axis UNMEASURED") as exc_info:
        assert_lengths_monotone_increasing(
            axis="brevity",
            levels_in_order=["terse", "balanced", "expansive"],
            samples_by_level=[identical_samples, identical_samples, identical_samples],
        )
    assert "brevity" in str(exc_info.value)

    with pytest.raises(AssertionError, match="humor axis UNMEASURED") as exc_info:
        assert_majority_absent_at_every_level(
            axis="humor",
            mouth="draft",
            samples_by_level={
                "none": ("(a wry aside)", "(another one)", "plain text"),
                "dry": ("(a wry aside)", "(another one)", "plain text"),
            },
            predicate=has_humor_aside,
        )
    assert "humor" in str(exc_info.value) and "draft" in str(exc_info.value)


# --------------------------------------------------------------------------------------------
# Proactivity exemption pin — NON-live, zero network (TK-210 build step 2, "proactivity" bullet).
# --------------------------------------------------------------------------------------------


def test_proactivity_is_exempt_prompt_text_is_byte_identical_across_levels() -> None:
    """Proactivity is EXEMPT from live output-effect sampling (TK-210 build step 2): it renders
    NO text at any level, for any mouth (a designed no-op — actuation is gate-side, proven by
    TK-215's deterministic gate tests, not this harness). This trivial, deterministic assertion
    pins that ``instruction_for`` claim without spending a single live model call."""
    for mouth in Mouth:
        rendered = {
            level: instruction_for(
                mouth, replace(DEFAULT_MATRIX, proactivity=level), _ASSISTANT_NAME
            )
            for level in Proactivity
        }
        distinct = set(rendered.values())
        assert len(distinct) == 1, (
            f"proactivity axis: instruction_for(mouth={mouth!r}, ...) differed across levels "
            f"{rendered!r} — expected byte-identical text (TK-215 owns actuation, not this "
            "harness)."
        )


# --------------------------------------------------------------------------------------------
# Brevity (COMPOSE): median length strictly increases terse -> expansive.
# --------------------------------------------------------------------------------------------


@_requires_live_persona_eval
async def test_brevity_median_length_terse_lt_balanced_lt_expansive(
    live_model: Model,
) -> None:
    """Repeated sampling of ONE representative, substantial fixture (TK-210 repair round 1):
    pooling one sample each from three heterogeneous fixtures let between-fixture content
    variance swamp the between-level brevity signal (armed re-derivation: non-monotone medians,
    e.g. [42, 42, 61]). Holding the input fixed and taking ``SAMPLES_PER_LEVEL`` repeats per
    level isolates the brevity effect — see ``eval_fixtures.py``'s "REPAIR ROUND 1" note.

    CLAUSE-STRENGTH FIX (TK-221, ISS-7, DEC-38, Q-108(c)): TK-210's repair round 2 found the
    literal three-way chain (terse < balanced < expansive) NOT reliably separable at any
    practical N under the ORIGINAL clause texts ("A sentence or two is fine if it helps
    clarity." / "Feel free to add a bit more detail and context.") and narrowed this
    assertion to strict terse < expansive only, flagging BALANCED's ordinal position as an
    unmeasurable-under-current-clauses governance finding. That was a CLAUSE-STRENGTH problem,
    not a concept problem: ``persona_policy.yaml``'s non-default brevity clauses were
    strengthened (TK-221) to explicitly request two-to-three full sentences (BALANCED) and a
    short four-to-six-sentence paragraph (EXPANSIVE) instead of the original, weaker hedged
    phrasing. A live re-derivation (N=9, this exact fixture) with the strengthened clauses
    confirmed the literal three-level chain now holds with a wide margin (medians ~98/171/334
    chars) — so the assertion below is RESTORED to the ticket's literal spec, not narrowed."""
    kind, payload = COMPOSE_NON_URGENT_FIXTURES[2]
    content = _compose_user_content(kind, payload)

    samples_by_level: dict[Brevity, tuple[str, ...]] = {}
    for brevity in (Brevity.TERSE, Brevity.BALANCED, Brevity.EXPANSIVE):
        matrix = replace(DEFAULT_MATRIX, brevity=brevity)
        samples = await _sample_repeated(live_model, Mouth.COMPOSE, matrix, content)
        for sample in samples:
            assert_no_forbidden_terms(sample, context=f"compose/brevity={brevity.value}")
        samples_by_level[brevity] = samples

    assert_lengths_monotone_increasing(
        axis="brevity",
        levels_in_order=["terse", "balanced", "expansive"],
        samples_by_level=[
            samples_by_level[Brevity.TERSE],
            samples_by_level[Brevity.BALANCED],
            samples_by_level[Brevity.EXPANSIVE],
        ],
    )


# --------------------------------------------------------------------------------------------
# Warmth (BRIEF): greeting-lexicon present (majority) at WARM, absent (majority) at RESERVED.
# --------------------------------------------------------------------------------------------


@_requires_live_persona_eval
async def test_warmth_greeting_lexicon_present_at_warm_absent_at_reserved(
    live_model: Model,
) -> None:
    warm_matrix = replace(DEFAULT_MATRIX, warmth=Warmth.WARM)
    reserved_matrix = DEFAULT_MATRIX  # DEFAULT_MATRIX.warmth is already RESERVED

    warm_samples = await _sample_brief_fixtures(live_model, warm_matrix)
    reserved_samples = await _sample_brief_fixtures(live_model, reserved_matrix)
    for sample in (*warm_samples, *reserved_samples):
        assert_no_forbidden_terms(sample, context="brief/warmth")

    assert_axis_separates_by_majority(
        axis="warmth",
        present_level="warm",
        present_samples=warm_samples,
        absent_level="reserved",
        absent_samples=reserved_samples,
        predicate=has_greeting,
    )


# --------------------------------------------------------------------------------------------
# Directness (COMPOSE): hedge-lexicon present (majority) at GENTLE, majority-absent at PLAIN and
# BLUNT.
# --------------------------------------------------------------------------------------------


@_requires_live_persona_eval
async def test_directness_hedge_lexicon_present_at_gentle_absent_at_plain_and_blunt(
    live_model: Model,
) -> None:
    """Repeated sampling of ONE representative fixture (TK-210 repair round 1, same rationale as
    brevity): pooling one sample each from three heterogeneous fixtures let idiosyncratic
    per-fixture hedging tendencies flip the majority verdict between live runs (armed
    re-derivation: PASS in one run, FAIL in another, with no code change). Holding the input
    fixed isolates the directness effect."""
    kind, payload = COMPOSE_NON_URGENT_FIXTURES[0]
    content = _compose_user_content(kind, payload)

    samples_by_directness: dict[Directness, tuple[str, ...]] = {}
    for directness in (Directness.GENTLE, Directness.PLAIN, Directness.BLUNT):
        matrix = replace(DEFAULT_MATRIX, directness=directness)
        samples = await _sample_repeated(live_model, Mouth.COMPOSE, matrix, content)
        for sample in samples:
            assert_no_forbidden_terms(sample, context=f"compose/directness={directness.value}")
        samples_by_directness[directness] = samples

    assert_axis_separates_by_majority(
        axis="directness (gentle vs plain)",
        present_level="gentle",
        present_samples=samples_by_directness[Directness.GENTLE],
        absent_level="plain",
        absent_samples=samples_by_directness[Directness.PLAIN],
        predicate=has_hedge,
    )
    assert_axis_separates_by_majority(
        axis="directness (gentle vs blunt)",
        present_level="gentle",
        present_samples=samples_by_directness[Directness.GENTLE],
        absent_level="blunt",
        absent_samples=samples_by_directness[Directness.BLUNT],
        predicate=has_hedge,
    )


# --------------------------------------------------------------------------------------------
# Humor (COMPOSE + BRIEF, non-urgent fixtures ONLY): the aside-heuristic separates DRY from NONE.
# --------------------------------------------------------------------------------------------


@_requires_live_persona_eval
async def test_humor_aside_heuristic_separates_dry_from_none_compose_and_brief(
    live_model: Model,
) -> None:
    """CLAUSE-STRENGTH FIX (TK-221, ISS-7, DEC-38, Q-108(c)): TK-210's repair round 2 confirmed
    this assertion was genuinely RED for the compose half — majority=False at BOTH dry and none
    (N=3): the ORIGINAL DRY clause ("A touch of dry humor is welcome") did not reliably make the
    live model produce a structurally-detectable prose aside on this fixture set. That was the
    no-placebo trip-wire (TK-210 AC3) firing exactly as designed, reported (not hidden or
    unilaterally patched) for the architect's adjudication, which ruled it a CLAUSE-STRENGTH
    problem, not a concept problem. ``persona_policy.yaml``'s non-default humor.dry clause was
    strengthened (TK-221) to name the mechanism explicitly ("one dry, understated aside, set off
    in parentheses or dashes") instead of the vaguer original phrasing. A live re-derivation with
    the strengthened clause confirmed the aside heuristic (``eval_fixtures.has_humor_aside``) now
    reliably fires on the DRY samples and stays absent on NONE, for both compose and brief — so
    this assertion is left AS SPECIFIED (the ticket's literal separation claim), now green."""
    dry_matrix = replace(DEFAULT_MATRIX, humor=Humor.DRY)
    none_matrix = DEFAULT_MATRIX  # DEFAULT_MATRIX.humor is already NONE

    # Sampled FIRST (TK-210 repair round 1): the urgent fixture is sampled here ONLY to prove it
    # never leaks into the majority computations below (kept OUT of the humor set, per
    # eval_fixtures.py's module docstring). Ordering this ahead of the separation asserts means
    # the exclusion proof still runs even when humor fails to separate (previously it sat after
    # both asserts, so it never executed on a failing run — dead code exactly when it mattered).
    urgent_kind, urgent_payload = COMPOSE_URGENT_FIXTURE
    urgent_sample = await _sample(
        live_model, Mouth.COMPOSE, dry_matrix, _compose_user_content(urgent_kind, urgent_payload)
    )
    assert_no_forbidden_terms(urgent_sample, context="humor(compose)/urgent-excluded")

    compose_dry = await _sample_compose_fixtures(live_model, dry_matrix)
    compose_none = await _sample_compose_fixtures(live_model, none_matrix)
    brief_dry = await _sample_brief_fixtures(live_model, dry_matrix)
    brief_none = await _sample_brief_fixtures(live_model, none_matrix)
    for sample in (*compose_dry, *compose_none, *brief_dry, *brief_none):
        assert_no_forbidden_terms(sample, context="humor(compose/brief)")

    assert_axis_separates_by_majority(
        axis="humor (compose)",
        present_level="dry",
        present_samples=compose_dry,
        absent_level="none",
        absent_samples=compose_none,
        predicate=has_humor_aside,
    )
    assert_axis_separates_by_majority(
        axis="humor (brief)",
        present_level="dry",
        present_samples=brief_dry,
        absent_level="none",
        absent_samples=brief_none,
        predicate=has_humor_aside,
    )


# --------------------------------------------------------------------------------------------
# Humor (DEC-37): humor markers asserted ABSENT from DRAFT and REFLECTION at BOTH humor levels.
# --------------------------------------------------------------------------------------------


@_requires_live_persona_eval
async def test_humor_markers_absent_from_draft_and_reflection_at_both_levels(
    live_model: Model,
) -> None:
    dry_matrix = replace(DEFAULT_MATRIX, humor=Humor.DRY)
    none_matrix = DEFAULT_MATRIX

    draft_samples_by_level = {
        "none": await _sample_repeated(live_model, Mouth.DRAFT, none_matrix, DRAFT_TASK_TEXT),
        "dry": await _sample_repeated(live_model, Mouth.DRAFT, dry_matrix, DRAFT_TASK_TEXT),
    }
    reflection_samples_by_level = {
        "none": await _sample_repeated(
            live_model, Mouth.REFLECTION, none_matrix, REFLECTION_TASK_TEXT
        ),
        "dry": await _sample_repeated(
            live_model, Mouth.REFLECTION, dry_matrix, REFLECTION_TASK_TEXT
        ),
    }
    for samples_by_level, mouth_name in (
        (draft_samples_by_level, "draft"),
        (reflection_samples_by_level, "reflection"),
    ):
        for level, samples in samples_by_level.items():
            for sample in samples:
                assert_no_forbidden_terms(sample, context=f"{mouth_name}/humor={level}")

    assert_majority_absent_at_every_level(
        axis="humor",
        mouth="draft",
        samples_by_level=draft_samples_by_level,
        predicate=has_humor_aside,
    )
    assert_majority_absent_at_every_level(
        axis="humor",
        mouth="reflection",
        samples_by_level=reflection_samples_by_level,
        predicate=has_humor_aside,
    )
