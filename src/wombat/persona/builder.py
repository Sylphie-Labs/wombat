"""wombat.persona.builder — ``ClauseAlgebraStrategy``: the v1 :class:`~wombat.persona.expression.
RenderStrategy`, assembling ONE mouth's instruction BODY from a
:class:`~wombat.persona.matrix.PersonaMatrix` (TK-207, refactored into a strategy by TK-219, EP-33,
DEC-33/DEC-37/DEC-38 per Jim's frame: the clause algebra is a PURE FUNCTION, "math algo" — no IO,
no config reads, no model calls; every clause is fixed text in a module-level table).

``instruction_for(mouth, matrix, assistant_name) -> str`` STAYS as a thin compatibility delegate
(TK-219, Q-108(a)): it builds a ``ClauseAlgebraStrategy(assistant_name)`` and routes it through
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

COMPOSITION: ``ClauseAlgebraStrategy.render`` returns ``body = base_role + clauses`` — the guard
suffix is NOT part of the strategy's output (Q-108(a) ruling: strategies never emit guard text;
the seam in ``expression.py`` appends it unconditionally, outside the strategy). Every DEFAULT-level
clause renders the EMPTY STRING (zero added bytes) so at ``DEFAULT_MATRIX`` the body degenerates to
exactly ``base_role``, and the seam's ``body + " " + guard_suffix`` join reproduces today's four
live strings byte-for-byte (the oracles this ticket is measured against; they are NOT edited here).

CLAUSE TABLES (module-level, fixed additive sentences — DEC-33/DEC-37 axes):
    - ``_LENGTH_CLAUSES``   (Brevity)    — brevity/length guidance, all mouths.
    - ``_REGISTER_CLAUSES`` (Warmth)     — tone/warmth guidance, all mouths.
    - ``_HEDGING_CLAUSES``  (Directness) — hedging/bluntness guidance, all mouths.
    - ``_HUMOR_CLAUSES``    (Humor)      — consulted ONLY for COMPOSE/BRIEF (DEC-37(c)); never
      for DRAFT (the user's outbound voice) or REFLECTION (NG-2 adjacency), at ANY level.
    - Proactivity renders NO text at any level, for any mouth — a DESIGNED no-op at the prompt
      layer (actuation is gate-side, TK-215), not a placebo; there is deliberately no clause
      table for it.

GUARD SUFFIX (verbatim substring of the output for every mouth/matrix combination, Q-106(a)) now
lives in ``wombat.persona.expression`` (TK-219) — consumed ONLY by the seam, never by this module:
    - compose    = ``"No preamble."``
    - brief      = ``"No preamble."`` plus the DEC-27 quoted-data sentence through the end.
    - draft      = ``"No preamble, no signature."``
    - reflection = the FULL "Never use clinical..." through "No preamble." block — its
      CON-6/NG-1 bars ARE the guard (DEC-37(d)).

PURITY (AC3): this module imports nothing beyond stdlib ``enum``/``dataclasses`` plus
``wombat.persona.matrix`` and ``wombat.persona.expression`` (the TK-219 seam types) — no IO, no
config reads, no model calls, no other wombat modules. NO call-site rewiring lives here (TK-209
owns that) and no output-effect measurement (TK-210). The four live mouth modules are NOT touched
by this ticket — they remain the DEFAULT-identity oracles ``tests/persona/test_builder.py``
measures this module against.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from wombat.persona.expression import EMPTY_CUES, Cues, Expression, render_expression
from wombat.persona.matrix import Brevity, Directness, Humor, PersonaMatrix, Warmth


class Mouth(StrEnum):
    """Closed enum — the four mouths this builder renders for (Q-106(a)). No other value is
    valid."""

    COMPOSE = "compose"
    BRIEF = "brief"
    DRAFT = "draft"
    REFLECTION = "reflection"


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
# Clause tables — fixed additive sentences per non-default axis level. The DEFAULT level of every
# axis maps to "" (zero added bytes), which is what makes DEFAULT_MATRIX byte-identical to the
# live oracles. No clause table exists for Proactivity — it is a designed no-op (TK-215 owns
# actuation).
# --------------------------------------------------------------------------------------------

_LENGTH_CLAUSES: dict[Brevity, str] = {
    Brevity.TERSE: "",
    Brevity.BALANCED: "A sentence or two is fine if it helps clarity.",
    Brevity.EXPANSIVE: "Feel free to add a bit more detail and context.",
}

_REGISTER_CLAUSES: dict[Warmth, str] = {
    Warmth.RESERVED: "",
    Warmth.NEUTRAL: "Keep the tone even and matter-of-fact.",
    Warmth.WARM: "Let the tone feel warm and friendly.",
}

_HEDGING_CLAUSES: dict[Directness, str] = {
    Directness.GENTLE: "Soften the phrasing and hedge gently.",
    Directness.PLAIN: "",
    Directness.BLUNT: "Be direct and blunt, without hedging.",
}

# Consulted ONLY for COMPOSE/BRIEF (DEC-37(c)) — see instruction_for below. Never applied to
# DRAFT or REFLECTION at any level, regardless of matrix.humor.
_HUMOR_CLAUSES: dict[Humor, str] = {
    Humor.NONE: "",
    Humor.DRY: "A touch of dry humor is welcome.",
}


@dataclass(frozen=True, slots=True)
class ClauseAlgebraStrategy:
    """The v1 :class:`~wombat.persona.expression.RenderStrategy` (TK-219) — the TK-207 clause
    algebra. ``assistant_name`` is held at CONSTRUCTION, not read from ``cues`` (RULED: the name
    is boot-static config, not a per-render cue — DEC-38's ``render(mouth, matrix, cues)`` seam
    signature stays verbatim; a name never becomes a ``Cues`` field).

    ``render`` returns ONLY the body — ``base_role + non-default clauses`` — and never the guard
    suffix (Q-108(a)): ``wombat.persona.expression.render_expression`` appends it unconditionally,
    outside every strategy, so no strategy (however adversarial) and no ``cues`` value can remove
    or shadow it. v1 never reads ``cues`` — see the module docstring's byte-identity note.
    """

    assistant_name: str

    def render(self, mouth: Mouth, matrix: PersonaMatrix, cues: Cues) -> Expression:
        """Render ``mouth``'s BODY from ``matrix`` (pure function, TK-207). ``cues`` is accepted
        for ``RenderStrategy`` conformance but never read — v1 wires no live cue producer.

        ``body = base_role + clauses``: every non-default axis level contributes a fixed additive
        sentence (single-space-joined) after the base role; every default-level axis contributes
        nothing. At ``DEFAULT_MATRIX`` this degenerates to exactly ``base_role`` — the seam then
        joins it with the guard suffix, reproducing the live TK-194 builder byte-for-byte for
        COMPOSE/BRIEF/DRAFT, and ``reflection_compose._SYSTEM_INSTRUCTION`` for REFLECTION.

        REFLECTION has no name slot (Q-106(a)): ``assistant_name`` is never rendered for this
        mouth, for any input.

        Humor is consulted ONLY for COMPOSE/BRIEF (DEC-37(c)) — DRAFT and REFLECTION never render
        a humor clause, at any ``matrix.humor`` level. Proactivity never renders any text, at any
        level, for any mouth (a designed no-op — actuation is gate-side, TK-215).
        """

        if mouth is Mouth.REFLECTION:
            base = _REFLECTION_BASE
        else:
            base = _BASE_ROLE_BUILDERS[mouth](self.assistant_name)

        clauses = [
            _LENGTH_CLAUSES[matrix.brevity],
            _REGISTER_CLAUSES[matrix.warmth],
            _HEDGING_CLAUSES[matrix.directness],
        ]
        if mouth in (Mouth.COMPOSE, Mouth.BRIEF):
            clauses.append(_HUMOR_CLAUSES[matrix.humor])
        # Proactivity: deliberately no clause appended — see module docstring.

        non_empty_clauses = [clause for clause in clauses if clause]
        return Expression(instruction=" ".join([base, *non_empty_clauses]))


def instruction_for(mouth: Mouth, matrix: PersonaMatrix, assistant_name: str) -> str:
    """Thin TK-219 compatibility delegate (Q-108(a)): build a ``ClauseAlgebraStrategy`` bound to
    ``assistant_name`` and route it through ``wombat.persona.expression.render_expression`` with
    ``EMPTY_CUES``, returning the resulting ``Expression.instruction``. Every existing call site
    and test is byte-unaffected — see ``ClauseAlgebraStrategy.render`` and the module docstring
    for the composition rules this reproduces exactly.
    """

    strategy = ClauseAlgebraStrategy(assistant_name)
    return render_expression(strategy, mouth, matrix, EMPTY_CUES).instruction
