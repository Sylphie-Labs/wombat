"""wombat.persona.matrix — ``PersonaMatrix``: five closed axes as named enum levels.

TK-206 (EP-33, DEC-33 as amended by DEC-37). A pure domain module: zero IO, zero config/env
reads, zero new dependencies. No prompt text lives here (that's TK-207) and no ``WombatConfig``
fields live here (that's TK-208).

FIVE AXES, each a closed named-level enum (DEC-33, amended by DEC-37):
    - ``Brevity``:     terse | balanced | expansive
    - ``Warmth``:      reserved | neutral | warm
    - ``Directness``:  gentle | plain | blunt
    - ``Humor``:       none | dry — EXACTLY TWO levels. DEC-37(c) struck the recorder-added
      third level "playful" as unapproved; humor renders only for the compose/brief mouths,
      never for Gmail drafts or reflection (TK-207 concern, noted here for context only).
    - ``Proactivity``: minimal | balanced | forward

``DEFAULT_MATRIX`` = (terse, reserved, plain, none, balanced). Proactivity's default is
BALANCED — zero gate ``urgency_threshold`` offset, i.e. today's gate behavior exactly — per
DEC-37(a), which supersedes DEC-33's original "minimal" default text.

NAMED EXCLUSIONS (deliberately not axes; recorded so their absence is a decision, not an
oversight — DEC-33, exclusion list completed by DEC-37(b)):
    - interruption-eagerness / chattiness-as-frequency (CON-2/CON-3) — the gate alone owns
      whether/when to surface; no axis creates or times a surfacing.
    - empathy-as-motive-inference (CON-6) — never model or act on the user's "why".
    - persuasion/flattery/sycophancy (CON-1) — the mouth renders pre-decided output; it does
      not steer the user.
    - action-initiative (CON-5) — persona never widens action authority past
      review-before-send.
    - coaching/clinical register (NG-2) — no diagnosis, no therapy framing.
    - persistence/reminder-frequency (NG-3) — nagging by another name.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class Brevity(StrEnum):
    """Closed enum — EXACTLY three levels (DEC-33). No other value is valid."""

    TERSE = "terse"
    BALANCED = "balanced"
    EXPANSIVE = "expansive"


class Warmth(StrEnum):
    """Closed enum — EXACTLY three levels (DEC-33). No other value is valid."""

    RESERVED = "reserved"
    NEUTRAL = "neutral"
    WARM = "warm"


class Directness(StrEnum):
    """Closed enum — EXACTLY three levels (DEC-33). No other value is valid."""

    GENTLE = "gentle"
    PLAIN = "plain"
    BLUNT = "blunt"


class Humor(StrEnum):
    """Closed enum — EXACTLY two levels (DEC-37(c) struck "playful"). No other value is valid."""

    NONE = "none"
    DRY = "dry"


class Proactivity(StrEnum):
    """Closed enum — EXACTLY three levels (DEC-33). No other value is valid."""

    MINIMAL = "minimal"
    BALANCED = "balanced"
    FORWARD = "forward"


@dataclass(frozen=True, slots=True)
class PersonaMatrix:
    """The five-axis persona matrix (DEC-33/DEC-37) — one closed level per axis."""

    brevity: Brevity
    warmth: Warmth
    directness: Directness
    humor: Humor
    proactivity: Proactivity


DEFAULT_MATRIX = PersonaMatrix(
    brevity=Brevity.TERSE,
    warmth=Warmth.RESERVED,
    directness=Directness.PLAIN,
    humor=Humor.NONE,
    proactivity=Proactivity.BALANCED,
)


def _parse_axis[E: StrEnum](values: Mapping[str, str], axis: str, enum_cls: type[E]) -> E:
    """Look up ``axis`` in ``values`` and parse it as ``enum_cls``, or raise ``ValueError``."""

    if axis not in values:
        raise ValueError(f"PersonaMatrix.from_strings: missing axis {axis!r}")
    raw = values[axis]
    try:
        return enum_cls(raw)
    except ValueError as exc:
        raise ValueError(
            f"PersonaMatrix.from_strings: axis {axis!r} has unknown value {raw!r}"
        ) from exc


def from_strings(values: Mapping[str, str]) -> PersonaMatrix:
    """Parse the plain-lowercase-string wire format (config/settings.json) into a matrix.

    Raises ``ValueError`` naming both the axis and the offending value for any axis whose value
    is not one of that axis's closed levels (or is missing entirely).
    """

    return PersonaMatrix(
        brevity=_parse_axis(values, "brevity", Brevity),
        warmth=_parse_axis(values, "warmth", Warmth),
        directness=_parse_axis(values, "directness", Directness),
        humor=_parse_axis(values, "humor", Humor),
        proactivity=_parse_axis(values, "proactivity", Proactivity),
    )


def to_strings(matrix: PersonaMatrix) -> dict[str, str]:
    """Serialize a matrix to the plain-lowercase-string wire format (config/settings.json)."""

    return {
        "brevity": matrix.brevity.value,
        "warmth": matrix.warmth.value,
        "directness": matrix.directness.value,
        "humor": matrix.humor.value,
        "proactivity": matrix.proactivity.value,
    }
