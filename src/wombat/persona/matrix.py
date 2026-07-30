"""wombat.persona.matrix — ``PersonaMatrix``: five closed axes as named enum levels.

TK-206 (EP-33, DEC-33 as amended by DEC-37). A pure domain module: zero IO, zero config/env
reads, zero new dependencies. No prompt text lives here (that's TK-207) and no ``WombatConfig``
fields live here (TK-208 puts them on ``WombatConfig`` itself). TK-208 does add
``matrix_from_config`` here — it builds a matrix from anything structurally shaped like the five
``wombat_persona_*`` fields (a ``Protocol``, Q-106(c)), so this module still never imports
``wombat.config`` (no cycle).

FIVE AXES, each a closed named-level enum (DEC-33, amended by DEC-37, WIDENED by DEC-67(b)/(c)
per TK-300):
    - ``Brevity``:     terse | balanced | expansive | exhaustive
    - ``Warmth``:      reserved | neutral | warm | affectionate
    - ``Directness``:  gentle | plain | blunt
    - ``Humor``:       none | dry | playful | comedian. DEC-37(c) originally struck the
      recorder-added level "playful" as unapproved at EXACTLY TWO levels; DEC-67(b) supersedes
      DEC-37(c) IN PART — widening the closed set back to include ``playful`` and adding a new
      ``comedian`` level, while the set stays CLOSED (still no arbitrary/free-form humor). Humor
      renders only for the compose/brief/chat mouths, never for Gmail drafts or reflection
      (TK-207/TK-220 concern, noted here for context only).

      Humor-x-chat composition note (DEC-67(b)): the CHAT base role is already permissive
      toward user-initiated banter at every humor level — a user who jokes gets rolled with
      regardless of the humor axis. What the humor axis adds on top is INITIATIVE — whether the
      mouth originates humor unprompted: ``none`` never initiates a joke; ``dry`` allows one
      understated aside; ``playful`` jokes when it comes naturally; ``comedian`` always works in
      a joke, pun, or comic riff.
    - ``Proactivity``: minimal | balanced | forward | eager. DEC-67(c) supersedes DEC-33 IN
      PART — widening the closed set from three to four levels by adding ``eager``, while the
      set stays CLOSED (still no arbitrary/free-form proactivity). ``Proactivity`` remains the
      ONE persona axis with gate-side actuation (TK-215/DEC-37(a)); ``eager`` is a bounded
      deterministic ``urgency_threshold`` offset like its siblings, not a new mechanism.

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
from typing import Protocol


class Brevity(StrEnum):
    """Closed enum — EXACTLY four levels (DEC-33, widened by DEC-67(b) per TK-300). No other
    value is valid."""

    TERSE = "terse"
    BALANCED = "balanced"
    EXPANSIVE = "expansive"
    EXHAUSTIVE = "exhaustive"


class Warmth(StrEnum):
    """Closed enum — EXACTLY four levels (DEC-33, widened by DEC-67(c) per TK-300). No other
    value is valid."""

    RESERVED = "reserved"
    NEUTRAL = "neutral"
    WARM = "warm"
    AFFECTIONATE = "affectionate"


class Directness(StrEnum):
    """Closed enum — EXACTLY three levels (DEC-33). No other value is valid."""

    GENTLE = "gentle"
    PLAIN = "plain"
    BLUNT = "blunt"


class Humor(StrEnum):
    """Closed enum — EXACTLY four levels (DEC-37(c) originally struck "playful" at two levels;
    DEC-67(b) supersedes DEC-37(c) in part, widening the closed set to four). No other value is
    valid."""

    NONE = "none"
    DRY = "dry"
    PLAYFUL = "playful"
    COMEDIAN = "comedian"


class Proactivity(StrEnum):
    """Closed enum — EXACTLY four levels (DEC-33, widened to four by DEC-67(c) supersession-in-
    part). No other value is valid."""

    MINIMAL = "minimal"
    BALANCED = "balanced"
    FORWARD = "forward"
    EAGER = "eager"


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


class _PersonaConfigLike(Protocol):
    """Structural shape ``matrix_from_config`` needs (TK-208, Q-106(c)) — duck-typed so this
    module never imports ``wombat.config`` (no cycle; persona stays a pure domain module).

    Read-only properties (rather than plain attributes) so narrower types — e.g.
    ``WombatConfig``'s ``Literal["terse", "balanced", "expansive"]`` fields — still satisfy this
    Protocol structurally (plain attributes are invariant; properties are covariant).
    """

    @property
    def wombat_persona_brevity(self) -> str: ...
    @property
    def wombat_persona_warmth(self) -> str: ...
    @property
    def wombat_persona_directness(self) -> str: ...
    @property
    def wombat_persona_humor(self) -> str: ...
    @property
    def wombat_persona_proactivity(self) -> str: ...


def matrix_from_config(config: _PersonaConfigLike) -> PersonaMatrix:
    """Build a ``PersonaMatrix`` from the five ``wombat_persona_*`` fields on ``config``.

    ``config`` is anything exposing the five ``wombat_persona_*`` string attributes (typically a
    ``WombatConfig``) — matched structurally via ``_PersonaConfigLike`` so this module never
    imports ``wombat.config`` (TK-208, Q-106(c): no cycle). Delegates to ``from_strings``, so an
    unrecognized value on any axis raises ``ValueError`` naming both the axis and the offending
    value, exactly as ``from_strings`` does.
    """

    return from_strings(
        {
            "brevity": config.wombat_persona_brevity,
            "warmth": config.wombat_persona_warmth,
            "directness": config.wombat_persona_directness,
            "humor": config.wombat_persona_humor,
            "proactivity": config.wombat_persona_proactivity,
        }
    )
