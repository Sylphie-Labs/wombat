"""wombat.persona.feedback — TK-213 (EP-35, DEC-36/DEC-37(h), Q-112 pre-ruled): closed-lexicon
deterministic detection of explicit persona feedback.

A pure, zero-IO module (mirrors ``wombat.persona.commands``' own posture): a CLOSED, versioned
table maps a fixed set of exact observational phrases to a ``(axis, direction)`` pair — one of
the five ``PersonaMatrix`` axis names, and ``'up'``/``'down'`` along that axis's DECLARED level
order (the SAME level-order convention ``wombat.persona.commands`` documents: ``'up'`` moves
toward the LATER-declared level, ``'down'`` toward the EARLIER-declared one). No LLM, no fuzzy
matching — ``detect_feedback_token`` only ever matches a normalized transcript against a
normalized lexicon phrase with EXACT equality, over the WHOLE utterance (the TK-211 discipline).

MOTIVE-FREE (CON-6): ``FeedbackToken`` carries only ``axis``/``direction``/``phrase`` — no
motive/why field, and this module never infers one. Only OBSERVATIONAL phrasing is admitted to
the lexicon (a complaint about the assistant's current behavior), never an IMPERATIVE ("be
warmer" belongs to ``wombat.persona.commands``' grammar instead, EXACTLY once — see
``tests/persona/test_feedback.py``'s normalized-disjointness proof).

RULING (binding, struck from the ticket intent): a generic-praise phrase like "loved that" maps
to no ``(axis, direction)`` pair — the nightly tuner (TK-214) could never act on it, so recording
it would be placebo data. Every lexicon entry maps to exactly one axis+direction; there is no
"no-op" entry.

``FEEDBACK_LEXICON_VERSION`` bumps whenever the phrase table changes shape (an entry is added,
removed, or reworded), mirroring ``wombat.persona.commands.GRAMMAR_VERSION``.

Reuses ``wombat.persona.commands._normalize`` (a same-package private import, sanctioned here) —
casefold + strip + fold ASCII punctuation to whitespace + collapse whitespace — rather than a
second, independently-drifting normalization routine.
"""

from __future__ import annotations

from dataclasses import dataclass

from wombat.persona.commands import _normalize

FEEDBACK_LEXICON_VERSION = 1

_VALID_AXES = frozenset({"brevity", "warmth", "directness", "humor", "proactivity"})
_VALID_DIRECTIONS = frozenset({"up", "down"})


@dataclass(frozen=True, slots=True)
class FeedbackToken:
    """One detected explicit-feedback token: a closed-vocabulary observation against exactly one
    persona axis. ``axis`` is one of the five ``PersonaMatrix`` field names; ``direction`` is
    ``'up'`` (toward the axis's later-declared level) or ``'down'`` (toward its earlier-declared
    level) — never a raw level name, never a motive. ``phrase`` is the matched lexicon phrase
    VERBATIM (not the raw transcript — the two are equal only when the transcript itself was
    already exactly that normalized phrase).

    NO motive/why field (CON-6) — this dataclass is intentionally minimal.
    """

    axis: str
    direction: str
    phrase: str

    def __post_init__(self) -> None:
        if self.axis not in _VALID_AXES:
            raise ValueError(f"FeedbackToken: unknown axis {self.axis!r}")
        if self.direction not in _VALID_DIRECTIONS:
            raise ValueError(
                f"FeedbackToken: direction must be 'up' or 'down', got {self.direction!r}"
            )


# The CLOSED lexicon (Q-112 pre-ruled): every entry maps to exactly ONE (axis, direction).
# Observational phrasing only, NEVER imperative — an imperative belongs to
# wombat.persona.commands.GRAMMAR instead (proven disjoint by
# tests/persona/test_feedback.py).
FEEDBACK_LEXICON: tuple[tuple[str, FeedbackToken], ...] = (
    ("too chatty", FeedbackToken(axis="brevity", direction="down", phrase="too chatty")),
    ("too long", FeedbackToken(axis="brevity", direction="down", phrase="too long")),
    ("too terse", FeedbackToken(axis="brevity", direction="up", phrase="too terse")),
    ("too stiff", FeedbackToken(axis="warmth", direction="up", phrase="too stiff")),
    ("too blunt", FeedbackToken(axis="directness", direction="down", phrase="too blunt")),
    ("too pushy", FeedbackToken(axis="proactivity", direction="down", phrase="too pushy")),
)

_NORMALIZED_LEXICON: dict[str, FeedbackToken] = {
    _normalize(phrase): token for phrase, token in FEEDBACK_LEXICON
}

# Keyed by the RAW (un-normalized) lexicon phrase — this is what a durable row's outcome_label
# carries verbatim (Q-112(a)), so TK-214's reader looks it up directly, no renormalization.
_PHRASE_LEXICON: dict[str, FeedbackToken] = dict(FEEDBACK_LEXICON)


def detect_feedback_token(transcript: str) -> FeedbackToken | None:
    """Match ``transcript`` against the closed lexicon, or return ``None``.

    Normalizes ``transcript`` (casefold + strip + fold ASCII punctuation to whitespace + collapse
    whitespace, via ``wombat.persona.commands._normalize``) then matches by EXACT equality against
    the normalized lexicon phrases, over the WHOLE utterance — never substring/prefix/fuzzy
    matching. Pure: no IO, no model call.
    """
    return _NORMALIZED_LEXICON.get(_normalize(transcript))


def token_for_phrase(phrase: str) -> FeedbackToken | None:
    """Look up the ``FeedbackToken`` for a RAW lexicon phrase (TK-214's reader): the exact string
    a durable row's ``outcome_label`` carries verbatim, not a raw transcript. ``None`` if
    ``phrase`` is not one of ``FEEDBACK_LEXICON``'s own phrases."""
    return _PHRASE_LEXICON.get(phrase)


__all__ = [
    "FEEDBACK_LEXICON",
    "FEEDBACK_LEXICON_VERSION",
    "FeedbackToken",
    "detect_feedback_token",
    "token_for_phrase",
]
