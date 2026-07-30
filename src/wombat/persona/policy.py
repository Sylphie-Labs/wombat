"""wombat.persona.policy — human-edited ``PersonaPolicy`` custody (TK-220, EP-33,
DEC-38(1)/(4), Q-108(b)).

ONE versioned, human-edited home for the per-mouth axis applicability (DEC-38(1) — which
prompt axes render for which mouth) and the per-axis-level clause text ``builder.py`` used to
hard-code in module-level tables (TK-207/TK-219). Mirrors ``wombat.params``'s custody
discipline exactly: ``importlib.resources`` loads the packaged, versioned
``persona_policy.yaml``; there is no mutation API and no runtime writer here, so this store is
structurally incapable of being written at runtime (restart-to-apply v1 — no hot-reload, a
non_goal for this ticket).

CLOSED VOCABULARIES enforced at load, each failing LOUD (a dedicated ``PersonaPolicyError``
naming the offense) rather than silently falling back to built-in text:
    - mouths — exactly the five ``wombat.persona.builder.Mouth`` values (compose/brief/draft/
      reflection/chat — chat added by TK-292, DEC-65a/c), Q-106(a). Duplicated here as plain
      strings rather than importing ``Mouth`` — ``builder.py`` imports THIS module for
      ``ClauseAlgebraStrategy``'s default policy, so importing ``Mouth`` back here would cycle.
    - axes — exactly the FOUR prompt axes: brevity, warmth, directness, humor. Proactivity is
      NOT a policy axis (TK-215 owns its gate-side actuation as a designed prompt-layer
      no-op) — the loader REJECTS ``proactivity`` appearing anywhere in ``mouth_axes`` or
      ``clauses``.
    - levels — the named levels of ``wombat.persona.matrix``'s ``Brevity``/``Warmth``/
      ``Directness``/``Humor`` enums, exactly.

``PERSONA_POLICY_VERSION`` is bumped in lock-step with the YAML's ``version`` field; a
mismatch fails loud rather than silently reconciling (AC5-style auditability, ``wombat.params``
precedent).

DEC-38(5) BYTE-IDENTITY INVARIANT (RULED): every DEFAULT-level clause text (the level
``DEFAULT_MATRIX`` uses for that axis) MUST be the empty string. The loader enforces this
structurally at load — an operator may retune non-default levels freely, but the default level
always renders zero added bytes, which is what keeps ``DEFAULT_MATRIX`` byte-identical to the
four live oracles ``tests/persona/test_builder.py`` measures against.

``load_policy`` accepts an explicit ``path`` for test fixtures (``wombat.params`` precedent).
``default_policy()`` is the lazily-loaded, process-lifetime-cached singleton
``wombat.persona.builder.ClauseAlgebraStrategy``'s compatibility delegate (``instruction_for``)
constructs its strategy with by default — loaded on first use, then reused for the rest of the
process's life (restart-to-apply v1).
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from wombat.persona.matrix import DEFAULT_MATRIX, Brevity, Directness, Humor, Warmth

# Bump in lock-step with persona_policy.yaml's `version` field whenever a mouth, axis, or
# level is added, removed, or renamed, so a persisted file can be reconciled against the
# code's expectation (wombat.params.OPERATING_PARAMS_VERSION precedent).
# v2 (TK-292, DEC-65a/c): the chat mouth was added to mouth_axes.
# v3 (TK-300, DEC-67b/c): brevity gained exhaustive, warmth gained affectionate, humor gained
# playful + comedian.
PERSONA_POLICY_VERSION = 3

_POLICY_FILENAME = "persona_policy.yaml"

# The five mouths (Q-106(a), extended to include CHAT by TK-292/DEC-65a/c) — duplicated from
# wombat.persona.builder.Mouth as plain strings; see the module docstring for why this module
# does not import Mouth.
_MOUTHS: tuple[str, ...] = ("compose", "brief", "draft", "reflection", "chat")

# The four policy-governed prompt axes (DEC-38(1)) and their closed level vocabularies, taken
# from wombat.persona.matrix's enums. Proactivity is deliberately absent — TK-215 owns it,
# gate-side, and it is not a rendered clause at all.
_AXIS_LEVELS: dict[str, tuple[str, ...]] = {
    "brevity": tuple(level.value for level in Brevity),
    "warmth": tuple(level.value for level in Warmth),
    "directness": tuple(level.value for level in Directness),
    "humor": tuple(level.value for level in Humor),
}
_AXES: tuple[str, ...] = tuple(_AXIS_LEVELS)

# The DEFAULT level per axis (DEFAULT_MATRIX) — its clause text must be "" (DEC-38(5)).
_DEFAULT_LEVEL: dict[str, str] = {
    "brevity": DEFAULT_MATRIX.brevity.value,
    "warmth": DEFAULT_MATRIX.warmth.value,
    "directness": DEFAULT_MATRIX.directness.value,
    "humor": DEFAULT_MATRIX.humor.value,
}

_TOP_LEVEL_KEYS = {"version", "mouth_axes", "clauses"}


class PersonaPolicyError(RuntimeError):
    """Raised when ``persona_policy.yaml`` is absent, unparseable, or invalid. The message
    NAMES the specific offense (unreadable/missing file, malformed YAML, an unknown or missing
    top-level/mouth/axis/level key, a ``proactivity`` reference, a version mismatch, or a
    non-empty default-level clause) — never a silent fallback to built-in text."""


@dataclass(frozen=True, slots=True)
class PersonaPolicy:
    """A loaded, validated persona policy (TK-220).

    ``mouth_axes`` maps each mouth name to the set of policy axis names that render for it;
    ``clauses`` maps axis name -> level name -> the fixed additive clause text. Both are
    closed-vocabulary-validated at load (see the module docstring) — a
    ``wombat.persona.builder.ClauseAlgebraStrategy`` consumes an instance of this at
    construction and performs no further validation or IO in ``render()``.
    """

    version: int
    mouth_axes: dict[str, tuple[str, ...]]
    clauses: dict[str, dict[str, str]]


def _default_policy_path() -> Path:
    """Resolve the packaged ``persona_policy.yaml`` (works editable and from a wheel)."""
    return Path(str(resources.files("wombat.persona").joinpath(_POLICY_FILENAME)))


def load_policy(path: Path | None = None) -> PersonaPolicy:
    """Load + validate the persona policy from the versioned YAML, or fail LOUD.

    Reads the human-edited source-of-truth (the packaged ``persona_policy.yaml`` unless an
    explicit ``path`` is given, mirroring ``wombat.params.load_operating_params`` — test
    fixtures pass ``path``). Raises ``PersonaPolicyError`` naming the offense for: a missing or
    unreadable file; non-mapping or unparseable YAML; an unknown or missing top-level key; an
    unknown or missing mouth key in ``mouth_axes``; a ``proactivity`` reference anywhere; an
    unknown axis name in a mouth's axis list; an unknown or missing axis key in ``clauses``; an
    unknown or missing level key within an axis; a non-string clause text; a version mismatch
    against ``PERSONA_POLICY_VERSION``; or a non-empty DEFAULT-level clause.
    """

    src = path or _default_policy_path()
    try:
        text = src.read_text(encoding="utf-8")
    except OSError as exc:
        raise PersonaPolicyError(f"persona policy file not readable: {src}") from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PersonaPolicyError(f"persona policy file {src} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise PersonaPolicyError(
            f"persona policy file {src} must contain a YAML mapping, got {type(raw).__name__}"
        )

    unknown_top = set(raw) - _TOP_LEVEL_KEYS
    if unknown_top:
        raise PersonaPolicyError(
            f"persona policy file {src} has unknown top-level key(s): {sorted(unknown_top)}"
        )
    missing_top = _TOP_LEVEL_KEYS - set(raw)
    if missing_top:
        raise PersonaPolicyError(
            f"persona policy file {src} is missing top-level key(s): {sorted(missing_top)}"
        )

    version = raw["version"]
    if version != PERSONA_POLICY_VERSION:
        raise PersonaPolicyError(
            f"persona policy file {src} has version {version!r}, expected "
            f"{PERSONA_POLICY_VERSION!r} (PERSONA_POLICY_VERSION)"
        )

    mouth_axes = _validate_mouth_axes(raw["mouth_axes"], src)
    clauses = _validate_clauses(raw["clauses"], src)

    return PersonaPolicy(version=version, mouth_axes=mouth_axes, clauses=clauses)


def _validate_mouth_axes(raw: Any, src: Path) -> dict[str, tuple[str, ...]]:
    """Validate the ``mouth_axes`` block: exactly the four mouths, each mapped to a list drawn
    only from the four policy axes (never ``proactivity``)."""

    if not isinstance(raw, dict):
        raise PersonaPolicyError(
            f"persona policy file {src}: mouth_axes must be a mapping, got {type(raw).__name__}"
        )

    unknown_mouths = set(raw) - set(_MOUTHS)
    if unknown_mouths:
        raise PersonaPolicyError(
            f"persona policy file {src}: mouth_axes has unknown mouth key(s): "
            f"{sorted(unknown_mouths)}"
        )
    missing_mouths = set(_MOUTHS) - set(raw)
    if missing_mouths:
        raise PersonaPolicyError(
            f"persona policy file {src}: mouth_axes is missing mouth key(s): "
            f"{sorted(missing_mouths)}"
        )

    result: dict[str, tuple[str, ...]] = {}
    for mouth, axes in raw.items():
        if not isinstance(axes, list) or not all(isinstance(axis, str) for axis in axes):
            raise PersonaPolicyError(
                f"persona policy file {src}: mouth_axes[{mouth!r}] must be a list of axis "
                f"names, got {axes!r}"
            )
        if "proactivity" in axes:
            raise PersonaPolicyError(
                f"persona policy file {src}: mouth_axes[{mouth!r}] lists 'proactivity', which "
                "is not a policy axis (its actuation is gate-side, TK-215)"
            )
        unknown_axes = set(axes) - set(_AXES)
        if unknown_axes:
            raise PersonaPolicyError(
                f"persona policy file {src}: mouth_axes[{mouth!r}] has unknown axis name(s): "
                f"{sorted(unknown_axes)}"
            )
        result[mouth] = tuple(axes)
    return result


def _validate_clauses(raw: Any, src: Path) -> dict[str, dict[str, str]]:
    """Validate the ``clauses`` block: exactly the four policy axes, each mapped to exactly
    that axis's closed level vocabulary, with the DEFAULT level's text always ``""``."""

    if not isinstance(raw, dict):
        raise PersonaPolicyError(
            f"persona policy file {src}: clauses must be a mapping, got {type(raw).__name__}"
        )

    if "proactivity" in raw:
        raise PersonaPolicyError(
            f"persona policy file {src}: clauses has a 'proactivity' entry, which is not a "
            "policy axis (its actuation is gate-side, TK-215)"
        )
    unknown_axes = set(raw) - set(_AXES)
    if unknown_axes:
        raise PersonaPolicyError(
            f"persona policy file {src}: clauses has unknown axis key(s): {sorted(unknown_axes)}"
        )
    missing_axes = set(_AXES) - set(raw)
    if missing_axes:
        raise PersonaPolicyError(
            f"persona policy file {src}: clauses is missing axis key(s): {sorted(missing_axes)}"
        )

    result: dict[str, dict[str, str]] = {}
    for axis, levels in raw.items():
        expected_levels = set(_AXIS_LEVELS[axis])
        if not isinstance(levels, dict):
            raise PersonaPolicyError(
                f"persona policy file {src}: clauses[{axis!r}] must be a mapping, got "
                f"{type(levels).__name__}"
            )
        unknown_levels = set(levels) - expected_levels
        if unknown_levels:
            raise PersonaPolicyError(
                f"persona policy file {src}: clauses[{axis!r}] has unknown level key(s): "
                f"{sorted(unknown_levels)}"
            )
        missing_levels = expected_levels - set(levels)
        if missing_levels:
            raise PersonaPolicyError(
                f"persona policy file {src}: clauses[{axis!r}] is missing level key(s): "
                f"{sorted(missing_levels)}"
            )
        for level, text in levels.items():
            if not isinstance(text, str):
                raise PersonaPolicyError(
                    f"persona policy file {src}: clauses[{axis!r}][{level!r}] must be a "
                    f"string, got {type(text).__name__}"
                )

        default_level = _DEFAULT_LEVEL[axis]
        default_text = levels[default_level]
        if default_text != "":
            raise PersonaPolicyError(
                f"persona policy file {src}: clauses[{axis!r}][{default_level!r}] (the "
                f"DEFAULT level) must be the empty string, got {default_text!r}"
            )
        result[axis] = dict(levels)
    return result


_DEFAULT_POLICY: PersonaPolicy | None = None


def default_policy() -> PersonaPolicy:
    """The lazily-loaded, process-lifetime-cached default policy (restart-to-apply v1) — loaded
    from the packaged ``persona_policy.yaml`` on first use and reused thereafter. This is what
    ``wombat.persona.builder.instruction_for``'s compatibility delegate constructs its
    ``ClauseAlgebraStrategy`` with by default (via that dataclass field's default factory)."""

    global _DEFAULT_POLICY
    if _DEFAULT_POLICY is None:
        _DEFAULT_POLICY = load_policy()
    return _DEFAULT_POLICY


__all__ = [
    "PERSONA_POLICY_VERSION",
    "PersonaPolicy",
    "PersonaPolicyError",
    "default_policy",
    "load_policy",
]
