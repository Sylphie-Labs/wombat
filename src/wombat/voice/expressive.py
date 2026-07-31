"""wombat.voice.expressive — the Fish Audio expressive-marker EMISSION POLICY (TK-327, EP-31,
DEC-71b/c/d/e as revised by DEC-72b/c/h/i, further revised by DEC-74).

Jim's steer (DEC-72, verbatim): "i saw square brackets being used? i dont think its supposed to
use parenthesis?" — wombat's spoken channel targets the Fish s2 engine family, whose marker
grammar is SQUARE BRACKETS (``[calm]``), not S1's parenthesized closed vocabulary. Because s2
accepts FREE-FORM bracket descriptions (``[warm, slightly amused]``), a vocabulary check against
the *engine's* grammar would be meaningless — so the closed set here is WOMBAT'S OWN EMISSION
POLICY, not a transcription of what Fish accepts: the shaping instruction offers ONLY the 8-tag
steward subset, and the deterministic validator rejects ANY bracketed token that is not EXACTLY
one of them (fixed, free-form, or invented alike) — reject-to-silence, the DEC-55f no-placebo
posture extended.

``TAG_DEFINITIONS`` is the SINGLE SOURCE for both halves (Jim pin, DEC-72h — "template/
definitions being passed to deepseek to appropriately add the brackets"): the instruction's
definitions block and ``ALLOWED_TAGS`` (the validator's whole policy) both derive from this ONE
ordered mapping, so instruction and guarantee structurally cannot drift apart. Each entry is one
terse line naming WHEN the tag fits, not merely that it exists — Jim's "appropriately" pin.

This module carries NO S1 parenthesized vocabulary and no 69-tag engine list (DEC-72c) — the
closed set IS the whole policy. Pure and deterministic throughout: no IO, no config reads, no
model calls.

``EXPRESSIVE_FISH_MODELS`` (recorded ruling, contract v2.187 r1) is the enumerated Fish s2 bracket
family — homed here beside ``TAG_DEFINITIONS`` because it is the other half of the SAME emission
policy (which engines this policy may ever apply to); TK-328 is its consumer, this ticket only
defines and structurally tests it.
"""

from __future__ import annotations

import re

# DEC-72b/h: the FIXED 8-tag steward subset, square-bracket form, ordered — each entry pairs the
# tag with ONE terse semantic + placement guidance line (Jim's "appropriately" pin: WHEN it fits,
# not just that it exists). This is the entire emission policy (DEC-72c) — no broader vocabulary
# lives anywhere in this module.
TAG_DEFINITIONS: dict[str, str] = {
    "[calm]": "a calm, settled feeling — start a sentence with it when reassuring the user.",
    "[curious]": (
        "a curious, inquisitive feeling — start a sentence that wonders or asks something."
    ),
    "[sympathetic]": (
        "a warm, sympathetic feeling — start a sentence acknowledging something hard for the user."
    ),
    "[soft tone]": "a gentler, quieter delivery — use for sensitive or low-key moments.",
    "[chuckling]": "a brief warm laugh — only when the content is genuinely light.",
    "[sighing]": "a soft sigh — only for mildly deflating or resigned news.",
    "[break]": "a short natural pause — place it where a person would pause.",
    "[long-break]": "a longer pause — place it where a person would pause meaningfully longer.",
}

# DEC-72c: ALLOWED_TAGS is derived from TAG_DEFINITIONS' key set — single source, so the
# instruction and the validator's guarantee can never drift apart.
ALLOWED_TAGS: frozenset[str] = frozenset(TAG_DEFINITIONS)

# Recorded ruling, contract v2.187 r1: the ENUMERATED Fish s2 bracket-family model set (not
# prefix-matched, DEC-72d) — lives here beside TAG_DEFINITIONS as the other half of the emission
# policy. TK-328 consumes it to decide expressive_tags at the constructed-adapter seam; this
# ticket only defines and structurally tests it.
EXPRESSIVE_FISH_MODELS: frozenset[str] = frozenset({"s2-pro", "s2.1-pro", "s2.1-pro-free"})

# One bracketed-token finder, shared by find_disallowed_token and strip_allowed_tags — matches any
# "[...]" span (fixed steward tag, free-form description, or an invented/prose-shaped token alike).
_BRACKET_TOKEN_RE = re.compile(r"\[[^\]]+\]")

# Collapsed whitespace after a tag strip (strip_allowed_tags) — mirrors the full-reply sanitize's
# own whitespace-run collapse (speech_shape._FULL_REPLY_WHITESPACE_RUN_RE).
_WHITESPACE_RUN_RE = re.compile(r"\s+")


def render_expressive_instruction() -> str:
    """Render the instruction EXTENSION offered only when ``expressive_tags`` is on: one
    definitions line per ``TAG_DEFINITIONS`` entry (Jim's template/definitions pin, DEC-72h),
    followed by the fixed placement rules — feeling/tone markers at sentence start, effects right
    after the word they punctuate, pauses where the silence goes, at most 3 markers per reply,
    markers in square brackets and never quoted, never invent a marker outside this list, and
    never place a marker directly before an opening parenthesis (the DEC-72c adjacency hazard —
    this instruction line is the belt; the guarantee is DEC-74's explicit whitespace-tolerant
    adjacency reject, homed in ``speech_shape._FORBIDDEN_PATTERNS`` — NOT, as an earlier revision
    of this docstring claimed, the pre-existing zero-space markdown-link pattern, which DEC-74
    proved never matched the spaced form '[break] (see below)' in the first place). Pure and
    deterministic — same output every call."""
    definitions = " ".join(f"{tag} means {guidance}" for tag, guidance in TAG_DEFINITIONS.items())
    placement_rules = (
        "You may add expressive markers from this exact set only. Place feeling or tone markers "
        "at the start of a sentence. Place effect markers right after the word they punctuate. "
        "Place pause markers where the silence actually goes. Use at most 3 markers in one reply. "
        "Markers are written in square brackets and are never quoted. Never invent a marker "
        "outside this list. Never place a marker directly before an opening parenthesis."
    )
    return f"{placement_rules} {definitions}"


def find_disallowed_token(text: str, allowed: frozenset[str]) -> str | None:
    """The first bracketed ``[...]`` token in ``text`` that is not EXACTLY a member of
    ``allowed``, else ``None`` — exact-token equality only (case-sensitive, brackets included),
    never a shape/prefix match. Prose parentheses are never bracketed tokens and are never
    inspected here."""
    for match in _BRACKET_TOKEN_RE.finditer(text):
        token = match.group(0)
        if token not in allowed:
            return token
    return None


def strip_allowed_tags(text: str) -> str:
    """Remove every occurrence of an ``ALLOWED_TAGS`` member from ``text`` and collapse the
    resulting whitespace runs to one space (trimmed) — deterministic and idempotent (a second
    pass over the output is a no-op since no allowed tag survives the first). Prose parentheses
    and any bracketed token outside ``ALLOWED_TAGS`` are left untouched."""
    stripped = text
    for tag in ALLOWED_TAGS:
        stripped = stripped.replace(tag, "")
    return _WHITESPACE_RUN_RE.sub(" ", stripped).strip()


__all__ = [
    "ALLOWED_TAGS",
    "EXPRESSIVE_FISH_MODELS",
    "TAG_DEFINITIONS",
    "find_disallowed_token",
    "render_expressive_instruction",
    "strip_allowed_tags",
]
