"""wombat.persona.commands — TK-211 (EP-34, DEC-35): closed voice-command grammar for the
persona matrix.

A pure, zero-IO module: it maps a FIXED, versioned table of exact utterances to persona-matrix
deltas, and applies those deltas as a pure, saturating (never-wrapping) clamp. No LLM, no fuzzy
matching, no substring/prefix matching — ``parse_persona_command`` only ever matches a
normalized transcript against a normalized grammar template with exact equality (Q-109(b)).

This module imports nothing but the stdlib and ``wombat.persona.matrix`` (CON-1/CON-2: no IO,
no config reads, no model calls) — enforced by ``tests/persona/test_commands.py`` AC3, which
inspects this module's own AST.

``GRAMMAR_VERSION`` bumps whenever the utterance table changes shape (an entry is added,
removed, or reworded) so callers (TK-212) can detect a grammar change without diffing the table
by hand.

Level order for every axis is its enum's DECLARATION order (Q-109(b)): a +1 step moves toward
the LATER-declared level, a -1 step moves toward the EARLIER-declared level. Concretely:
    - brevity:     terse(0) -> balanced(1) -> expansive(2)  "more detailed" = +1, "more brief" = -1
    - warmth:      reserved(0) -> neutral(1) -> warm(2)     "warmer" = +1, "more reserved" = -1
    - directness:  gentle(0) -> plain(1) -> blunt(2)        "more direct" = +1, "gentler" = -1
    - humor:       none(0) -> dry(1)                        "funnier" = +1, "no jokes" = -1
    - proactivity: minimal(0) -> balanced(1) -> forward(2)  "more proactive" = +1, "less
      proactive" = -1

Stepping past either end of an axis's level order SATURATES at that end — clamped, never wraps,
never raises.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import StrEnum

from wombat.persona.matrix import (
    DEFAULT_MATRIX,
    Brevity,
    Directness,
    Humor,
    PersonaMatrix,
    Proactivity,
    Warmth,
)

GRAMMAR_VERSION = 1

_AXIS_ENUMS: dict[str, type[StrEnum]] = {
    "brevity": Brevity,
    "warmth": Warmth,
    "directness": Directness,
    "humor": Humor,
    "proactivity": Proactivity,
}


@dataclass(frozen=True, slots=True)
class PersonaCommand:
    """One parsed voice command: a delta against exactly one axis, or a reset of the whole
    matrix (Q-109(b)). Exactly one of ``step``, ``set_level``, ``reset`` is populated; the other
    two stay at their falsy default. ``axis`` is one of the five ``PersonaMatrix`` field names
    for a step/set-level command, or ``None`` for a ``reset`` command.
    """

    axis: str | None
    step: int | None = None
    set_level: str | None = None
    reset: bool = False

    def __post_init__(self) -> None:
        populated = sum([self.step is not None, self.set_level is not None, self.reset])
        if populated != 1:
            raise ValueError("PersonaCommand must carry exactly one of step/set_level/reset")

        if self.reset:
            if self.axis is not None:
                raise ValueError("PersonaCommand: reset must not carry an axis")
            return

        if self.axis is None or self.axis not in _AXIS_ENUMS:
            raise ValueError(f"PersonaCommand: unknown axis {self.axis!r}")

        if self.step is not None and self.step not in (1, -1):
            raise ValueError("PersonaCommand: step must be +1 or -1")

        if self.set_level is not None:
            enum_cls = _AXIS_ENUMS[self.axis]
            if self.set_level not in {member.value for member in enum_cls}:
                raise ValueError(
                    f"PersonaCommand: {self.set_level!r} is not a level of axis {self.axis!r}"
                )


def _step(axis: str, direction: int) -> PersonaCommand:
    return PersonaCommand(axis=axis, step=direction)


def _set(axis: str, level: str) -> PersonaCommand:
    return PersonaCommand(axis=axis, set_level=level)


_RESET = PersonaCommand(axis=None, reset=True)

# Fixed, versioned utterance table (Q-109(b)). Minimum coverage per axis: one "up" (+1) and one
# "down" (-1) step utterance, plus one set-level utterance per declared level. Plus one
# whole-matrix reset. ``parse_persona_command`` matches these by exact equality only, after
# normalization — never substring/prefix/fuzzy matching.
GRAMMAR: tuple[tuple[str, PersonaCommand], ...] = (
    # brevity — step
    ("be more brief", _step("brevity", -1)),
    ("be more detailed", _step("brevity", 1)),
    # brevity — set-level (one per declared level)
    ("set brevity to terse", _set("brevity", Brevity.TERSE.value)),
    ("set brevity to balanced", _set("brevity", Brevity.BALANCED.value)),
    ("set brevity to expansive", _set("brevity", Brevity.EXPANSIVE.value)),
    ("set brevity to exhaustive", _set("brevity", Brevity.EXHAUSTIVE.value)),
    # warmth — step
    ("be warmer", _step("warmth", 1)),
    ("be more reserved", _step("warmth", -1)),
    # warmth — set-level
    ("set warmth to reserved", _set("warmth", Warmth.RESERVED.value)),
    ("set warmth to neutral", _set("warmth", Warmth.NEUTRAL.value)),
    ("set warmth to warm", _set("warmth", Warmth.WARM.value)),
    ("set warmth to affectionate", _set("warmth", Warmth.AFFECTIONATE.value)),
    # directness — step
    ("be more direct", _step("directness", 1)),
    ("be gentler", _step("directness", -1)),
    # directness — set-level
    ("set directness to gentle", _set("directness", Directness.GENTLE.value)),
    ("set directness to plain", _set("directness", Directness.PLAIN.value)),
    ("set directness to blunt", _set("directness", Directness.BLUNT.value)),
    # humor — step (exactly two levels)
    ("be funnier", _step("humor", 1)),
    ("no jokes", _step("humor", -1)),
    # humor — set-level
    ("set humor to none", _set("humor", Humor.NONE.value)),
    ("set humor to dry", _set("humor", Humor.DRY.value)),
    ("set humor to playful", _set("humor", Humor.PLAYFUL.value)),
    ("set humor to comedian", _set("humor", Humor.COMEDIAN.value)),
    # proactivity — step
    ("be more proactive", _step("proactivity", 1)),
    ("be less proactive", _step("proactivity", -1)),
    # proactivity — set-level
    ("set proactivity to minimal", _set("proactivity", Proactivity.MINIMAL.value)),
    ("set proactivity to balanced", _set("proactivity", Proactivity.BALANCED.value)),
    ("set proactivity to forward", _set("proactivity", Proactivity.FORWARD.value)),
    ("set proactivity to eager", _set("proactivity", Proactivity.EAGER.value)),
    # whole-matrix reset
    ("reset persona", _RESET),
)

# ASCII punctuation folded to whitespace during normalization (keeps word boundaries when a
# transcript drops a space around punctuation, e.g. "brevity,to").
_PUNCTUATION_RE = re.compile(r"[!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~]")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Casefold + strip + fold ASCII punctuation to whitespace + collapse whitespace."""

    folded = _PUNCTUATION_RE.sub(" ", text).casefold()
    return _WHITESPACE_RE.sub(" ", folded).strip()


_NORMALIZED_GRAMMAR: dict[str, PersonaCommand] = {
    _normalize(utterance): command for utterance, command in GRAMMAR
}


def parse_persona_command(transcript: str) -> PersonaCommand | None:
    """Parse ``transcript`` against the closed grammar table, or return ``None``.

    Normalizes ``transcript`` (casefold + strip + fold ASCII punctuation to whitespace +
    collapse whitespace) then matches by EXACT equality against the normalized grammar
    templates — never substring/prefix/fuzzy matching (Q-109(b)). No LLM, no fuzz.
    """

    return _NORMALIZED_GRAMMAR.get(_normalize(transcript))


def _resolve_level[E: StrEnum](enum_cls: type[E], current: E, command: PersonaCommand) -> E:
    """Resolve the new level for one axis: either the commanded ``set_level``, or ``current``
    stepped by ``command.step`` and clamped (saturating, never wrapping) to the enum's declared
    level order.
    """

    if command.set_level is not None:
        return enum_cls(command.set_level)

    assert command.step is not None
    levels = list(enum_cls)
    new_index = max(0, min(len(levels) - 1, levels.index(current) + command.step))
    return levels[new_index]


def apply(matrix: PersonaMatrix, command: PersonaCommand) -> PersonaMatrix:
    """Pure, saturating apply of ``command`` to ``matrix``.

    Stepping past either end of an axis's level order clamps to that end — never wraps, never
    raises. Setting a level sets that one axis outright. ``reset`` returns ``DEFAULT_MATRIX``
    unconditionally, regardless of ``matrix``.
    """

    if command.reset:
        return DEFAULT_MATRIX

    match command.axis:
        case "brevity":
            return replace(matrix, brevity=_resolve_level(Brevity, matrix.brevity, command))
        case "warmth":
            return replace(matrix, warmth=_resolve_level(Warmth, matrix.warmth, command))
        case "directness":
            return replace(
                matrix, directness=_resolve_level(Directness, matrix.directness, command)
            )
        case "humor":
            return replace(matrix, humor=_resolve_level(Humor, matrix.humor, command))
        case "proactivity":
            return replace(
                matrix, proactivity=_resolve_level(Proactivity, matrix.proactivity, command)
            )
        case _:
            raise AssertionError(f"unreachable: unknown axis {command.axis!r}")
