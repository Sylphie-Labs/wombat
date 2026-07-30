"""wombat.persona.builder — ``ClauseAlgebraStrategy``: the v1 :class:`~wombat.persona.expression.
RenderStrategy`, assembling ONE mouth's instruction BODY from a
:class:`~wombat.persona.matrix.PersonaMatrix` (TK-207, refactored into a strategy by TK-219, and
made policy-DATA-DRIVEN by TK-220, EP-33, DEC-33/DEC-37/DEC-38 per Jim's frame: the clause algebra
is a PURE FUNCTION, "math algo" — ``render()`` itself does no IO, no config reads, no model calls;
every clause and every mouth's axis applicability lives in the versioned, human-edited
``wombat.persona.policy`` custody, not in a module-level table here).

``instruction_for(mouth, matrix, assistant_name) -> str`` STAYS as a thin compatibility delegate
(TK-219, Q-108(a)): it builds a ``ClauseAlgebraStrategy(assistant_name)`` — which resolves its
``policy`` field to ``wombat.persona.policy.default_policy()``'s lazily-loaded, process-cached
default (TK-220) unless one is passed explicitly — and routes it through
``wombat.persona.expression.render_expression`` with ``EMPTY_CUES``, returning the resulting
``Expression.instruction``. Every existing call site and test is byte-unaffected by this refactor.

FOUR MOUTHS (closed enum ``Mouth``, Q-106(a)) mirror the four live, hand-written system-instruction
builders this module is measured against byte-for-byte at ``DEFAULT_MATRIX``:
    - COMPOSE    -> ``wombat.stages.compose._system_instruction``
    - BRIEF      -> ``wombat.compose.brief_template.brief_system_instruction``
    - DRAFT      -> ``wombat.integrations.gmail.draft_composer._system_instruction``
    - REFLECTION -> ``wombat.behavior.stages.reflection_compose._SYSTEM_INSTRUCTION`` (a
      CONSTANT, no name slot — ``instruction_for`` never renders ``assistant_name`` for this
      mouth, for ANY input, matching the live text exactly)

TK-292 (DEC-65a/c) adds a FIFTH mouth, CHAT — the warm companion register, with NO live oracle
to match (it is new, not a byte-identity port). It is the only mouth with a SECOND name slot
(``user_name``, construction-held on ``ClauseAlgebraStrategy`` exactly like ``assistant_name`` —
never a Cue), so its base-role dispatch is special-cased in ``render`` rather than living in the
one-arg ``_BASE_ROLE_BUILDERS`` dict the other four mouths share.

COMPOSITION: ``ClauseAlgebraStrategy.render`` returns ``body = base_role + clauses`` — the guard
suffix is NOT part of the strategy's output (Q-108(a) ruling: strategies never emit guard text;
the seam in ``expression.py`` appends it unconditionally, outside the strategy). Every DEFAULT-level
clause renders the EMPTY STRING (zero added bytes) so at ``DEFAULT_MATRIX`` the body degenerates to
exactly ``base_role``, and the seam's ``body + " " + guard_suffix`` join reproduces today's four
live strings byte-for-byte (the oracles this ticket is measured against; they are NOT edited here).

PER-MOUTH AXIS APPLICABILITY + CLAUSE TEXT (TK-220, DEC-38(1)/(4), Q-108(b)) now live in
``wombat.persona.policy.PersonaPolicy``, loaded from the versioned, human-edited
``persona_policy.yaml`` (restart-to-apply v1, no hot-reload):
    - ``policy.mouth_axes[mouth]`` — which of the FOUR prompt axes (brevity, warmth,
      directness, humor) render for ``mouth``. The render ORDER is fixed here as brevity,
      warmth, directness, humor, filtered to whichever axes the policy lists for that mouth.
      The SHIPPED DEFAULT keeps today's placement EXACTLY (humor for compose/brief only,
      DEC-37(c)'s original placement) — but DEC-38(1) unbars per-mouth placement structurally,
      superseding-in-part DEC-37(c)'s code walls, so an operator MAY grant/withhold any axis
      for any mouth by editing the YAML, with zero code changes.
    - ``policy.clauses[axis][level]`` — the fixed additive sentence for that axis/level.
    - Proactivity renders NO text at any level, for any mouth — a DESIGNED no-op at the prompt
      layer (actuation is gate-side, TK-215), not a placebo. It is NOT a policy axis at all —
      ``wombat.persona.policy``'s loader REJECTS it appearing in ``mouth_axes`` or ``clauses``.

GUARD SUFFIX (verbatim substring of the output for every mouth/matrix combination, Q-106(a)) now
lives in ``wombat.persona.expression`` (TK-219) — consumed ONLY by the seam, never by this module,
and it is NOT policy (TK-220): it stays seam-owned, never editable via ``persona_policy.yaml``:
    - compose    = ``"No preamble."``
    - brief      = ``"No preamble."`` plus the DEC-27 quoted-data sentence through the end.
    - draft      = ``"No preamble, no signature."``
    - reflection = the FULL "Never use clinical..." through "No preamble." block — its
      CON-6/NG-1 bars ARE the guard (DEC-37(d)).

PURITY (AC3): this module imports nothing beyond stdlib ``enum``/``dataclasses`` plus
``wombat.persona.matrix``, ``wombat.persona.expression`` (the TK-219 seam types), and
``wombat.persona.policy`` (TK-220 — used only to type/default-construct
``ClauseAlgebraStrategy.policy``; ``render()`` itself performs no IO — the policy load, if any,
happens at STRATEGY CONSTRUCTION via that field's default factory, never inside ``render()``). NO
call-site rewiring lives here (TK-209 owns that) and no output-effect measurement (TK-210). The
four live mouth modules are NOT touched by this ticket — they remain the DEFAULT-identity oracles
``tests/persona/test_builder.py`` measures this module against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from wombat.persona.expression import EMPTY_CUES, Cues, Expression, render_expression
from wombat.persona.matrix import PersonaMatrix
from wombat.persona.policy import PersonaPolicy, default_policy


class Mouth(StrEnum):
    """Closed enum — the FIVE mouths this builder renders for. No other value is valid.

    TK-292 (DEC-65a/c) adds CHAT, the companion register: a warm, conversational voice at
    DEFAULT_MATRIX, distinct from the four original stewardly mouths (Q-106(a))."""

    COMPOSE = "compose"
    BRIEF = "brief"
    DRAFT = "draft"
    REFLECTION = "reflection"
    CHAT = "chat"


# --------------------------------------------------------------------------------------------
# Base role sentences — the byte-identical prefix of each live mouth's DEFAULT-level text, name
# slot preserved exactly where the live builder has one (TK-194, Q-105e). REFLECTION has NO name
# slot (it is a plain module constant upstream) so its base role is a fixed string, never
# f-string-interpolated with a name.
# --------------------------------------------------------------------------------------------


def _compose_base(name: str) -> str:
    return (
        f"You are {name}, a quiet steward. Phrase this one item for the user in one terse, "
        "calm line."
    )


def _brief_base(name: str) -> str:
    return (
        f"You are {name}, a quiet steward delivering this morning's brief. The lines below are "
        "the already-decided brief contents. Phrase them for the user in a few terse, calm "
        "lines — do not add, omit, or invent anything beyond what is given."
    )


def _draft_base(name: str) -> str:
    return (
        f"You are {name}, a quiet steward drafting a reply on the user's behalf. Phrase one "
        "terse, calm reply body responding to the quoted excerpt."
    )


def _chat_base(assistant_name: str, user_display: str) -> str:
    """CHAT's base role (TK-292, DEC-65a/c) — the ONLY mouth with a SECOND name slot
    (``user_display``), so it does not fit the one-arg ``_BASE_ROLE_BUILDERS`` dict shape and is
    dispatched via a special case in ``ClauseAlgebraStrategy.render`` instead."""
    return (
        f"You are {assistant_name}, {user_display}'s personal assistant and companion, chatting "
        f"with {user_display}. Reply naturally and conversationally in a warm, familiar voice - "
        "match the user's tone, and roll with jokes, banter, and playfulness when the user brings "
        "them. Casual conversation is welcome for its own sake; do not steer the chat back to "
        "schedules, email, or duties unless asked. Ground anything factual in what you are given, "
        "and keep replies short and human - a sentence or two unless more is clearly wanted."
    )


# REFLECTION's base role — fixed, no name slot (the live source has none; Q-106(a)).
_REFLECTION_BASE = (
    "You are a quiet steward reflecting one gentle behavioral observation back to the user. "
    "Phrase it in ONE terse, calm sentence."
)

_BASE_ROLE_BUILDERS = {
    Mouth.COMPOSE: _compose_base,
    Mouth.BRIEF: _brief_base,
    Mouth.DRAFT: _draft_base,
}


# --------------------------------------------------------------------------------------------
# Axis rendering order (TK-220) — FIXED here, independent of the data-driven policy. A policy's
# mouth_axes[mouth] is a SET of which of these axes apply; this tuple is the ORDER they are
# consulted and joined in, matching the pre-TK-220 length/register/hedging/humor join order
# exactly (byte-identity, AC1).
# --------------------------------------------------------------------------------------------

_AXIS_ORDER: tuple[str, ...] = ("brevity", "warmth", "directness", "humor")


def _axis_value(matrix: PersonaMatrix, axis: str) -> str:
    """The matrix's current level, as a plain string, for a policy axis name."""

    return {
        "brevity": matrix.brevity.value,
        "warmth": matrix.warmth.value,
        "directness": matrix.directness.value,
        "humor": matrix.humor.value,
    }[axis]


@dataclass(frozen=True, slots=True)
class ClauseAlgebraStrategy:
    """The v1 :class:`~wombat.persona.expression.RenderStrategy` (TK-219) — the TK-207 clause
    algebra, made policy-DATA-DRIVEN by TK-220. ``assistant_name`` is held at CONSTRUCTION, not
    read from ``cues`` (RULED: the name is boot-static config, not a per-render cue — DEC-38's
    ``render(mouth, matrix, cues)`` seam signature stays verbatim; a name never becomes a
    ``Cues`` field).

    ``policy`` (TK-220) is ALSO held at CONSTRUCTION — a
    :class:`~wombat.persona.policy.PersonaPolicy` supplying per-mouth axis applicability
    (``mouth_axes``) and per-axis-level clause text (``clauses``). It defaults to
    ``wombat.persona.policy.default_policy()`` (the lazily-loaded, process-cached packaged
    ``persona_policy.yaml``) via this field's default factory — resolved at CONSTRUCTION time,
    never inside ``render()``, which performs no IO at all.

    ``render`` returns ONLY the body — ``base_role + non-default clauses`` — and never the guard
    suffix (Q-108(a)): ``wombat.persona.expression.render_expression`` appends it unconditionally,
    outside every strategy, so no strategy (however adversarial) and no ``cues`` value can remove
    or shadow it. v1 never reads ``cues`` — see the module docstring's byte-identity note.
    """

    assistant_name: str
    policy: PersonaPolicy = field(default_factory=default_policy)
    # TK-292 (DEC-65a/c): the CHAT mouth's second name slot — construction-held, like
    # assistant_name, never a Cue (same DEC-38 rationale: boot-static config, not a per-render
    # cue). Only CHAT ever reads this field; ``user_display`` falls back to "the user" when this
    # is None or blank.
    user_name: str | None = None

    def render(self, mouth: Mouth, matrix: PersonaMatrix, cues: Cues) -> Expression:
        """Render ``mouth``'s BODY from ``matrix`` and ``self.policy`` (TK-207, made
        policy-data-driven by TK-220 — this method itself performs NO IO; ``self.policy`` was
        already a loaded value by the time this runs). ``cues`` is accepted for
        ``RenderStrategy`` conformance but never read — v1 wires no live cue producer.

        ``body = base_role + clauses``: for each axis in ``self.policy.mouth_axes[mouth]``
        (consulted in the FIXED order brevity, warmth, directness, humor — ``_AXIS_ORDER``),
        the matrix's current level for that axis is looked up in
        ``self.policy.clauses[axis]`` and, if non-empty, single-space-joined after the base
        role. Every DEFAULT-level clause is the empty string (enforced at policy load,
        DEC-38(5)), so at ``DEFAULT_MATRIX`` this degenerates to exactly ``base_role`` — the
        seam then joins it with the guard suffix, reproducing the live TK-194 builder
        byte-for-byte for COMPOSE/BRIEF/DRAFT, and ``reflection_compose._SYSTEM_INSTRUCTION``
        for REFLECTION.

        REFLECTION has no name slot (Q-106(a)): ``assistant_name`` is never rendered for this
        mouth, for any input.

        Which axes render for which mouth is entirely policy-data-driven (DEC-38(1)) — the
        SHIPPED DEFAULT policy grants humor to COMPOSE/BRIEF only (DEC-37(c)'s original
        placement), never DRAFT or REFLECTION, but an operator may retune this per-mouth via
        ``persona_policy.yaml`` with zero code changes. Proactivity never renders any text, at
        any level, for any mouth — it is not a policy axis at all (a designed no-op, actuation
        gate-side, TK-215).
        """

        if mouth is Mouth.REFLECTION:
            base = _REFLECTION_BASE
        elif mouth is Mouth.CHAT:
            # CHAT is the only mouth with a second name slot (the user's) — it does not fit the
            # one-arg _BASE_ROLE_BUILDERS dict shape, so it is special-cased here (TK-292).
            user_display = self.user_name if self.user_name else "the user"
            base = _chat_base(self.assistant_name, user_display)
        else:
            base = _BASE_ROLE_BUILDERS[mouth](self.assistant_name)

        axes_for_mouth = self.policy.mouth_axes[mouth.value]
        clauses = [
            self.policy.clauses[axis][_axis_value(matrix, axis)]
            for axis in _AXIS_ORDER
            if axis in axes_for_mouth
        ]

        non_empty_clauses = [clause for clause in clauses if clause]
        return Expression(instruction=" ".join([base, *non_empty_clauses]))


def instruction_for(
    mouth: Mouth, matrix: PersonaMatrix, assistant_name: str, user_name: str | None = None
) -> str:
    """Thin TK-219 compatibility delegate (Q-108(a)): build a ``ClauseAlgebraStrategy`` bound to
    ``assistant_name`` (its ``policy`` field resolves to
    ``wombat.persona.policy.default_policy()`` — TK-220's lazily-loaded default) and route it
    through ``wombat.persona.expression.render_expression`` with ``EMPTY_CUES``, returning the
    resulting ``Expression.instruction``. Every existing call site and test is byte-unaffected —
    see ``ClauseAlgebraStrategy.render`` and the module docstring for the composition rules this
    reproduces exactly.

    ``user_name`` (TK-292, DEC-65a/c) is optional and defaults to ``None`` — every existing call
    site stays byte-unaffected; only ``Mouth.CHAT`` ever reads it (via ``ClauseAlgebraStrategy``'s
    same-named field).
    """

    strategy = ClauseAlgebraStrategy(assistant_name, user_name=user_name)
    return render_expression(strategy, mouth, matrix, EMPTY_CUES).instruction
