"""tests.persona.eval_fixtures — the FIXED, versioned payload set + recorded verdict rule for
TK-210's live output-effect harness (EP-33's DONE-BAR, DEC-37, Q-107(c)).

This module holds only data + pure verdict-rule helpers — no model call, no I/O, no env read.
Everything here is importable and exercisable without live creds (the AC3 no-placebo trip-wire
proof in ``test_output_effects_live.py`` exercises the helpers below directly, with no network).

FIXTURE SET (v1 — versioned by this comment; bump the comment, never silently reshape the tuples,
if the payload set changes): COMPOSE items mirror ``wombat.stages.compose``'s real payload shape
(calendar/gmail-shaped ``dict`` fields), BRIEF bodies mirror ``render_brief_lines``'s rendered
shape (``wombat.compose.brief_template``), DRAFT/REFLECTION use one representative fixed task
text each (mirroring ``draft_composer.py``'s ``quoted_excerpt`` block and
``reflection_compose.py``'s ``_task_text`` line respectively) — TK-210's briefing rules these two
mouths need only a representative fixed text, not a payload family.

``COMPOSE_NON_URGENT_FIXTURES`` (exactly three, everyday, non-urgent) feeds every compose-mouth
axis test. ``COMPOSE_URGENT_FIXTURE`` is a fourth, urgent-toned item that is deliberately KEPT
OUT of every humor sample set — it is sampled only where an axis test needs to prove that
exclusion holds (never folded into a humor majority computation).

REPAIR ROUND 1 (TK-210): the fixtures below carry more substantial per-item content than the
first attempt. An armed re-derivation showed the original trivial one-liners (e.g. a bare
"Team sync 09:00-09:30, Conference Room B") gave the model too little material for a real
terse-vs-balanced-vs-expansive length delta to appear — medians came back non-monotone
(e.g. [42, 42, 61] chars) — and the SAME triviality made hedge-lexicon presence/absence for
directness idiosyncratic per fixture (a majority-verdict flip between two live runs). The
brevity and directness axis tests (``test_output_effects_live.py``) now hold the input FIXED —
repeated ``SAMPLES_PER_LEVEL`` completions of ONE representative fixture per level — rather than
pooling one sample each from three heterogeneous fixtures, so between-fixture content variance
can no longer swamp the between-level signal. Warmth (brief) and humor continue to sample across
the full fixture set for lexical breadth, unchanged.

A deeper re-derivation during this repair (30 live samples per fixture — N=10 at each of the
three brevity levels — across all three ``COMPOSE_NON_URGENT_FIXTURES``, on top of the armed
pytest runs) found a
FURTHER, more precise result for brevity: TERSE-vs-EXPANSIVE median length is a strong, reliable
effect (>=99% separation across every fixture tested at N=3), but the middle BALANCED level is
NOT reliably ordered relative to either neighbor at any practical N (bootstrap: ~55-70% even at
N=15) — the model does not consistently treat "a sentence or two is fine" (BALANCED's clause) as
meaningfully longer than TERSE or meaningfully shorter than EXPANSIVE's "a bit more detail and
context". This is a real property of the current prompt clauses (``_LENGTH_CLAUSES`` in
``wombat.persona.builder``, out of this tests-only ticket's reach), not a fixture artifact — it
held across fixtures of very different sizes, including a deliberately larger one used only for
this investigation. The brevity axis test therefore asserts the CLAIM THAT IS ACTUALLY MEASURABLE
— strict terse < expansive — while still sampling and forbidden-terms-checking BALANCED so its
behavior is on record; its ordinal position is a narrower, separate governance note, not folded
into a coin-flip assertion (see the test's own docstring for the full rationale). This narrowing
(dropping BALANCED's ordinal position from the assertion) is a reduction of the ticket's literal
brevity spec ("terse < balanced < expansive") to its measurable sub-claim — it is flagged here,
and in the build report, for the architect to record as a governance decision/deferral naming
BALANCED's median ordering as unmeasurable under the current prompt clauses; this tests-only
harness does not write that decision itself (``planning/contract.yaml`` is out of reach here).

REPAIR ROUND 2 (TK-210): an independent verifier confirmed the humor aside-heuristic's bare
structural pattern (any parenthetical/dash-set-off span, with no content check) was over-broad —
it matched ordinary, non-humorous parentheticals ("invoice total (USD 4,200)", "end of day
(EOD)", "attached file (invoice.pdf)", "three days (Mon, Tue, Wed)") that DRAFT/REFLECTION
samples routinely contain, and could equally pollute NONE-level compose/brief samples. This both
false-fired the DEC-37 absence check on innocent content and risked defeating the DRY-vs-NONE
separation check. ``has_humor_aside`` now also requires the matched content to read as PROSE, not
DATA (see its docstring below) — the same structural pattern, narrowed to actual dry-aside prose.
A live re-derivation after this fix confirmed both halves: the DEC-37 absence check now passes
cleanly, AND the humor (compose) DRY-vs-NONE separation check still comes back UNMEASURED at
N=3 (a genuine model/prompt property, not a heuristic bug — see ``HUMOR_ASIDE_PATTERN``'s
docstring below). That is a real measurability finding for the architect to adjudicate (record a
decision/deferral naming the humor axis) — not something this harness resolves by further
narrowing the assertion unilaterally.

TK-221 CLAUSE-STRENGTH FIX (ISS-7, DEC-38, Q-108(c)): the architect adjudicated both repair-round
findings above as a CLAUSE-STRENGTH problem, not a concept problem — the shipped humor.dry and
brevity.balanced/expansive clause TEXTS in ``persona_policy.yaml`` were too weak to move a mouth
also instructed to be terse. TK-221 iterated those non-default clause texts (data edits only, no
harness or builder code change) until a live re-derivation showed BOTH previously-unmeasured
claims now separate under the SAME recorded verdict rule below, unweakened: humor DRY-vs-NONE
majority-separates on both compose and brief, and brevity's median length now orders strictly
terse < balanced < expansive (not just terse < expansive). The heuristics/fixtures in this module
(``HUMOR_ASIDE_PATTERN``, ``has_humor_aside``, ``SAMPLES_PER_LEVEL``, the fixture payloads) were
NOT changed for TK-221 — the clause-text strengthening alone was sufficient. See
``test_output_effects_live.py``'s brevity and humor test docstrings for the live re-derivation
detail.

VERDICT RULE (recorded here as named constants, not buried in test bodies): ``SAMPLES_PER_LEVEL``
= 9 live completions per matrix level (bumped from 3 during repair round 1, via 5 as an
intermediate step that armed re-derivation showed was STILL not reliable enough). Directness's
observed per-sample hedge rate at GENTLE is real but moderate (~72%, measured directly against
the live model across 25 samples during this repair) — with a bare majority rule, N=3 gives only
an ~82% chance of a majority hedge-hit at GENTLE (an ~18% false-negative rate: real signal, but a
knife-edge live run), and N=5 only reaches ~88%. N=9 pushes the same real, unchanged effect to a
computed ~92% (confirmed against 7 further armed runs of the directness test during this repair:
6/7 passed, consistent with that estimate) — a materially lower false-negative rate than N=3's
knife-edge, though still probabilistic (an inherent property of sampling a stochastic live model
at a genuinely moderate, not saturated, effect size — no live-model test can be 100%
deterministic) — at a still-modest live-run cost (a full run of this module takes low-single-digit
minutes). ``majority_verdict`` decides lexicon/marker presence-or-absence (a strict majority of
the N samples); ``median_response_length`` decides length-based comparisons (the median, not the
mean, so one unusually long/short completion can't flip a verdict). ``assert_*`` below are the
NO-PLACEBO TRIP-WIRE (TK-210 AC3): when the recorded rule cannot honestly separate two levels,
they raise ``AssertionError`` naming the axis as UNMEASURED — never a skip, never a silent pass.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from wombat.gate.models import ItemKind

# --------------------------------------------------------------------------------------------
# The recorded verdict rule — named constants, on the record (TK-210 build step 1).
# --------------------------------------------------------------------------------------------

SAMPLES_PER_LEVEL = 9

# Warmth axis (BRIEF mouth): a plain, case-insensitive greeting-word lexicon.
GREETING_LEXICON: tuple[str, ...] = ("good morning", "morning", "hello", "hi there", "hey")

# Directness axis (COMPOSE mouth): the hedge-word lexicon named in the briefing verbatim.
HEDGE_LEXICON: tuple[str, ...] = ("might", "perhaps", "maybe", "could", "possibly", "consider")

# The reflection CON-6/NG-2 forbidden-term bars (reused verbatim from
# ``wombat.persona.builder._GUARD_SUFFIX[Mouth.REFLECTION]`` — the classifier every collected
# sample, at every level, every mouth, must pass).
FORBIDDEN_TERMS: tuple[str, ...] = (
    "diagnosis",
    "disorder",
    "symptom",
    "you seem to",
    "you tend to",
    "because you",
    "due to your",
)

# Humor axis: a short aside set off by parentheses or a pair of em dashes is the recorded,
# honest operationalization of "a touch of dry humor" (``_HUMOR_CLAUSES[Humor.DRY]`` in
# ``wombat.persona.builder``) — dry humor in a terse one-line reply typically surfaces as a brief
# aside ("(unusually, no meetings before ten)" / "— against all odds — the inbox is empty"),
# rather than as sentiment words a plain lexicon could reliably catch. This IS the documented
# humor aside-heuristic the verdict rule uses; if live sampling shows it cannot separate DRY from
# NONE, that is the humor axis failing its measurability bar (AC3) — reported, not hidden.
#
# REPAIR ROUND 2 (TK-210): the bare STRUCTURAL pattern (any parenthetical/dash-set-off span) was
# too broad — an independent verifier confirmed it matches ordinary, non-humorous parentheticals
# a DRAFT/REFLECTION mouth routinely emits ("invoice total (USD 4,200)", "end of day (EOD)",
# "attached file (invoice.pdf)", "three days (Mon, Tue, Wed)"), which both (a) false-fired the
# DEC-37 absence check on innocent content and (b) could pollute a NONE-level majority for the
# DRY-vs-NONE separation check with incidental, non-humorous asides. ``has_humor_aside`` now
# requires the structural match AND that the matched content itself reads as PROSE rather than
# DATA: at least ``_MIN_PROSE_WORDS`` lowercase word-tokens, and not a bare filename. This
# excludes the four confirmed false positives above while still matching this module's own
# genuine dry-aside examples ("unusually, no meetings before ten", "against all odds"). A
# subsequent armed re-derivation confirmed the fix: the DEC-37 draft/reflection absence check now
# passes cleanly (no more false fires on innocent parentheticals). It ALSO surfaced a separate,
# genuine finding — not a heuristic bug — the humor (compose) DRY-vs-NONE separation check still
# came back UNMEASURED (majority=False at BOTH dry and none, N=3): at N=3 the DRY clause ("A
# touch of dry humor is welcome") did not reliably make the live model produce a
# structurally-detectable prose aside on this fixture set. This is the trip-wire firing exactly
# as designed (TK-210 AC3), reported (not hidden or unilaterally narrowed) for the architect to
# adjudicate — record a decision/deferral naming the humor axis, or scope a follow-up ticket
# (e.g. a higher N, in the style of brevity/directness's round-1 bump) to re-attempt measurement.
HUMOR_ASIDE_PATTERN = re.compile(r"\(([^()]{2,80})\)|—([^—]{2,80})—")

# The prose-vs-data discriminator (REPAIR ROUND 2, named constants per the recorded-verdict-rule
# convention). A candidate aside counts as humor only if it is NOT a bare filename and contains at
# least this many lowercase word-tokens — plain data (acronyms, currency/numeric values, short
# lists of capitalized abbreviations) has none; ordinary dry-humor prose has several.
_MIN_PROSE_WORDS = 2
_FILENAME_PATTERN = re.compile(r"^[\w\-]+\.[A-Za-z0-9]{2,4}$")
_PROSE_WORD_PATTERN = re.compile(r"\b[a-z]+\b")


def _is_prose_aside(content: str) -> bool:
    """True if ``content`` (the text captured inside a candidate aside) reads as prose rather
    than plain data — the REPAIR ROUND 2 discriminator documented on ``HUMOR_ASIDE_PATTERN``."""
    trimmed = content.strip()
    if _FILENAME_PATTERN.match(trimmed):
        return False
    return len(_PROSE_WORD_PATTERN.findall(content)) >= _MIN_PROSE_WORDS


def has_humor_aside(text: str) -> bool:
    """The documented humor aside-heuristic predicate (see ``HUMOR_ASIDE_PATTERN`` above): a
    structural aside whose content also reads as prose, not data."""
    for match in HUMOR_ASIDE_PATTERN.finditer(text):
        content = match.group(1) if match.group(1) is not None else match.group(2)
        if content is not None and _is_prose_aside(content):
            return True
    return False


def lexicon_predicate(lexicon: Sequence[str]) -> Callable[[str], bool]:
    """Build a case-insensitive substring-presence predicate over ``lexicon``."""
    lowered_lexicon = tuple(term.lower() for term in lexicon)

    def _predicate(text: str) -> bool:
        lowered_text = text.lower()
        return any(term in lowered_text for term in lowered_lexicon)

    return _predicate


has_greeting = lexicon_predicate(GREETING_LEXICON)
has_hedge = lexicon_predicate(HEDGE_LEXICON)


def majority_verdict(samples: Sequence[str], predicate: Callable[[str], bool]) -> bool:
    """The recorded presence/absence verdict rule: a STRICT majority of ``samples`` satisfy
    ``predicate`` (N=``SAMPLES_PER_LEVEL`` by convention, but this works for any sample count)."""
    if not samples:
        msg = "majority_verdict: samples is empty — cannot render a verdict over zero samples"
        raise ValueError(msg)
    hits = sum(1 for sample in samples if predicate(sample))
    return hits > len(samples) / 2


def median_response_length(samples: Sequence[str]) -> float:
    """The recorded length-comparison verdict rule: the median character length across samples."""
    if not samples:
        msg = "median_response_length: samples is empty — cannot compute a median over zero"
        raise ValueError(msg)
    return statistics.median(len(sample) for sample in samples)


def assert_no_forbidden_terms(text: str, *, context: str) -> None:
    """The forbidden-terms classifier (build step 2, bullet "forbidden terms") — every sample
    collected by every axis test, at every level, every mouth, is run through this. Not a
    statistical verdict (no majority/median): a single occurrence is a hard failure."""
    lowered = text.lower()
    hits = [term for term in FORBIDDEN_TERMS if term in lowered]
    if hits:
        msg = f"forbidden-terms violation in {context}: found {hits!r} in sample {text!r}"
        raise AssertionError(msg)


def assert_axis_separates_by_majority(
    *,
    axis: str,
    present_level: str,
    present_samples: Sequence[str],
    absent_level: str,
    absent_samples: Sequence[str],
    predicate: Callable[[str], bool],
) -> None:
    """The no-placebo trip-wire (TK-210 AC3) for a presence-vs-absence axis: majority-verdict
    ``present_samples`` at ``present_level`` MUST be True and majority-verdict ``absent_samples``
    at ``absent_level`` MUST be False, or this raises ``AssertionError`` NAMING ``axis`` as
    unmeasured — never a skip, never a silent pass."""
    present_majority = majority_verdict(present_samples, predicate)
    absent_majority = majority_verdict(absent_samples, predicate)
    if not (present_majority and not absent_majority):
        msg = (
            f"{axis} axis UNMEASURED: the majority-verdict rule could not separate "
            f"{present_level} (majority={present_majority}) from {absent_level} "
            f"(majority={absent_majority}) — no-placebo trip-wire fired (TK-210 AC3); this axis "
            "must be flagged in governance, not shipped as measured."
        )
        raise AssertionError(msg)


def assert_lengths_monotone_increasing(
    *, axis: str, levels_in_order: Sequence[str], samples_by_level: Sequence[Sequence[str]]
) -> None:
    """The no-placebo trip-wire (TK-210 AC3) for a length-monotonicity axis: the median length at
    each successive level in ``levels_in_order`` MUST be strictly greater than the last, or this
    raises ``AssertionError`` NAMING ``axis`` as unmeasured."""
    medians = [median_response_length(samples) for samples in samples_by_level]
    if not all(medians[i] < medians[i + 1] for i in range(len(medians) - 1)):
        msg = (
            f"{axis} axis UNMEASURED: median lengths across {list(levels_in_order)} were "
            f"{medians} — not strictly monotone increasing; no-placebo trip-wire fired "
            "(TK-210 AC3); this axis must be flagged in governance, not shipped as measured."
        )
        raise AssertionError(msg)


def assert_majority_absent_at_every_level(
    *,
    axis: str,
    mouth: str,
    samples_by_level: Mapping[str, Sequence[str]],
    predicate: Callable[[str], bool],
) -> None:
    """The no-placebo trip-wire (TK-210 AC3, DEC-37) for an always-absent axis: NO level in
    ``samples_by_level`` may show a majority-verdict presence of ``predicate``, or this raises
    ``AssertionError`` NAMING ``axis`` and ``mouth`` as unmeasured."""
    offenders = {
        level: samples
        for level, samples in samples_by_level.items()
        if majority_verdict(samples, predicate)
    }
    if offenders:
        msg = (
            f"{axis} axis UNMEASURED for mouth={mouth!r}: majority marker presence detected at "
            f"level(s) {sorted(offenders)} where DEC-37 requires absence — no-placebo trip-wire "
            "fired (TK-210 AC3); this axis must be flagged in governance, not shipped as "
            "measured."
        )
        raise AssertionError(msg)


# --------------------------------------------------------------------------------------------
# The fixed, versioned payload set (build step 1).
# --------------------------------------------------------------------------------------------

# COMPOSE — mirrors compose.py:131-137's real payload shape: calendar/gmail-shaped dict fields,
# routed through wombat.compose.templates.format_payload_fields in the test module.
COMPOSE_NON_URGENT_FIXTURES: tuple[tuple[ItemKind, dict[str, Any]], ...] = (
    (
        ItemKind.GENERIC,
        {
            "title": "Team sync",
            "start_local": "09:00",
            "end_local": "09:30",
            "location": "Conference Room B",
            "agenda": (
                "Review the sprint burndown, discuss the onboarding doc revision, and decide "
                "who owns the Q3 roadmap draft; leave ten minutes at the end for open questions "
                "from the two new hires joining the team this month."
            ),
        },
    ),
    (
        ItemKind.GENERIC,
        {
            "subject": "Weekly newsletter",
            "sender": "digest@example.com",
            "snippet": (
                "This week: the office kitchen renovation wraps up Friday, three new "
                "teammates start Monday, the quarterly potluck moves to the 18th, and the "
                "parking garage will have one level closed for repaving through next week."
            ),
        },
    ),
    (
        ItemKind.GENERIC,
        {
            "title": "Grocery pickup reminder",
            "start_local": "17:30",
            "end_local": "18:00",
            "location": "Corner Market",
            "notes": (
                "Pick up the usual list plus everything for Saturday's dinner: pasta, two "
                "jars of sauce, parmesan, a loaf of sourdough, and a bag of salad greens; the "
                "store said the order will be waiting at the customer pickup counter."
            ),
        },
    ),
)

# Urgent-toned, deliberately KEPT OUT of every humor sample set (see module docstring).
COMPOSE_URGENT_FIXTURE: tuple[ItemKind, dict[str, Any]] = (
    ItemKind.GENERIC,
    {
        "subject": "URGENT: production outage — immediate action required",
        "sender": "ops-alerts@example.com",
        "snippet": "Production is down and on-call is being paged now.",
    },
)

# BRIEF — render_brief_lines-shaped blocks (wombat.compose.brief_template), quiet/everyday.
BRIEF_NON_URGENT_FIXTURES: tuple[str, ...] = (
    "Prep:\n- Team sync 09:00-09:30\n- Dentist appointment 14:00-14:30",
    'Recap:\n- "Weekly newsletter" from "digest@example.com"',
    "Nothing else on the brief this morning.",
)

# DRAFT — one representative fixed task text, mirroring draft_composer.py's exact user-message
# shape (recipient/subject/reply_kind/quoted_excerpt lines).
DRAFT_TASK_TEXT = (
    "recipient: alex@example.com\n"
    "subject: Re: quick question about the invoice\n"
    "reply_kind: high\n"
    'quoted_excerpt: "Can you confirm the invoice total before I forward it to finance?"'
)

# REFLECTION — one representative fixed task text, mirroring reflection_compose.py's _task_text
# line shape (kind/date only — never pattern_id/window_ref, which stay KB/queue-internal).
REFLECTION_TASK_TEXT = "kind: pattern_reflection; date: 2026-07-10"

__all__ = [
    "BRIEF_NON_URGENT_FIXTURES",
    "COMPOSE_NON_URGENT_FIXTURES",
    "COMPOSE_URGENT_FIXTURE",
    "DRAFT_TASK_TEXT",
    "FORBIDDEN_TERMS",
    "GREETING_LEXICON",
    "HEDGE_LEXICON",
    "HUMOR_ASIDE_PATTERN",
    "REFLECTION_TASK_TEXT",
    "SAMPLES_PER_LEVEL",
    "assert_axis_separates_by_majority",
    "assert_lengths_monotone_increasing",
    "assert_majority_absent_at_every_level",
    "assert_no_forbidden_terms",
    "has_greeting",
    "has_hedge",
    "has_humor_aside",
    "lexicon_predicate",
    "majority_verdict",
    "median_response_length",
]
