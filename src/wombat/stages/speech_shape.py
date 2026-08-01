"""SpeechShapeStage — the speech-shaped DeepSeek summary hop between ``chat_reply`` and ``speak``
(TK-267, DEC-55).

Jim's directive (DEC-55): the spoken channel gets a plain-English summary produced by ONE
DeepSeek call; TTS never receives formatted/composed text; the text channel (journal, chat pane,
brief) is byte-untouched. The live drain graph becomes ``compose -> chat_reply -> speech_shape ->
speak`` — this stage is the NEW hop, inserted AFTER ``chat_reply`` (which still resolves the FULL
composed text to the chat broker, upstream of any shaping).

``run()`` reads the SAME ``wombat.composed_output`` artifact ``compose`` produced (via
``ctx.last_output("compose")``, mirroring ``chat_reply``/``SpeakSink``'s own read) and, iff voice
is enabled AND a TTS adapter is available, calls ``ctx.model.complete`` ONCE (the same TK-8 mouth
pattern as ``ComposeStage``: a bounded ``asyncio.wait_for``, never-raise, degrade-on-any-failure)
to produce a plain spoken-English summary. Voice-off or no adapter is a ZERO-model-call
pass-through — the voice-off outcome downstream at ``speak`` stays byte-identical to today.

TK-9 layer 2 (Q-68) rides along exactly as it does for ``ComposeStage``: an optional
``spend_ledger``/``daily_token_ceiling`` gate the call PRE-call (fail-closed to no speech at/over
the ceiling, or on a ledger read failure) and account POST-call (a ledger write failure only logs
loud — the already-produced speech output stands).

NO-PLACEBO VALIDATION (DEC-55f): ``_shape_speech_text`` is a pure function over the model's raw
text — ANY of the enumerated closed token classes (bold/italic markers, heading ``#``, backticks/
code fences, ``[text](url)`` links, raw ``http(s)://`` URLs, list markers) or an overlong response
REJECTS the whole text outright (never a partially-stripped rewrite — a rewrite risks producing
mangled, ungrammatical speech that never existed in anyone's actual output). Rejection, a blank
response, or the model call itself raising/timing out ALL produce the SAME outcome: no speech
text, ``degraded=True`` — NEVER a fallback to the composed text (DEC-55c never-verbatim; there is
no template/verbatim fallback for this mouth, unlike ``ComposeStage``'s degrade-to-template).

NO fifth 'speech' persona mouth (DEC-55e, deferred): the prompt is a FIXED module constant with
``persona.expression.guard_suffix(Mouth.COMPOSE)`` appended verbatim (a read-only import) — zero
persona-package diff.

LEADING SPEAKER-LABEL STRIP (TK-317, DEC-69a): before the trim/length-check/forbidden loop above,
``_shape_speech_text`` applies one ANCHORED, ONE-SHOT strip for a leading "Label: " prefix at
string START only — a SINGLE word-like token (a leading letter, then up to ~32 chars of letters/
digits/apostrophe/hyphen/underscore — NO spaces, batch-review repair: a spaced token class ate
legitimate leading clauses like "It costs 5: dollars") followed by a colon and whitespace.
STRIP, not reject —
removing a leading label cannot mangle the remainder, unlike the DEC-55f markdown classes below,
which still reject the whole text outright and are unmodified by this change. The strip runs
BEFORE the length check so the remaining body keeps the full ``max_chars`` budget. It is generic
by design (no configured-assistant-name dependency) and belt-only: ``_SPEECH_SHAPE_INSTRUCTION``
also asks the model not to open with a name/label, but nothing relies on the model obeying it
(DEC-27 deterministic-boundary posture).

DEC-57/TK-272: when the composed-output artifact carries ``held_chat=True`` (a chat item the gate
held for voice purposes only), ``run()`` takes the EXACT SAME voice-off pass-through shape as
today — ZERO model calls, ``speech_text=None``, ``degraded=False`` — regardless of
``voice_enabled``/``adapter_present``. A held chat reply is quiet by design, never degraded.

TK-279 (DEC-60b, supersedes DEC-57 IN PART — voice origin only): the pass-through gate becomes
``held_chat and not voice_turn`` — a held reply to a SPOKEN turn (``voice_turn=True``, read off
the SAME composed-output artifact) falls through to the real shaping call exactly as a surfaced
item would; a held TYPED chat (``voice_turn=False``) stays byte-identical to the pre-TK-279
quiet pass-through above.

TK-327 (DEC-71b/c/d/e as revised by DEC-72b/c/h/i, further revised by DEC-74): an opt-in,
default-OFF ``expressive_tags`` flag. False (the default) leaves ``_system_instruction``
BYTE-IDENTICAL to today's join — a byte pin. True extends the SAME join with
``voice.expressive.render_expressive_instruction()`` — a definitions block (one line per
``voice.expressive.TAG_DEFINITIONS`` entry) plus the fixed placement rules, offering ONLY the
8-tag square-bracket steward subset. ``_shape_speech_text`` gains an ``allowed_tags`` parameter
(default the empty ``frozenset``): after the existing forbidden-pattern/length checks, ANY
bracketed ``[...]`` token not EXACTLY in ``allowed_tags`` rejects the WHOLE text to ``None`` (the
DEC-55f no-placebo posture extended — never a partial strip). The stage passes
``voice.expressive.ALLOWED_TAGS`` iff ``expressive_tags`` else the empty set, so validate-then-send
is structural: no code path in this stage ever emits unvalidated text (DEC-72i). Marker chars
count against the injected ``max_chars`` (DEC-71e, no budget change). The opt-in
``speak_full_replies``/full-replies path below is untouched by this flag (DEF-18 — tags never
reach the pane).

DEC-74 (adjacency reject, orchestrator ruling correcting a disproven DEC-72c premise): the
zero-space markdown-link pattern in ``_FORBIDDEN_PATTERNS`` below never matched the SPACED
adjacency hazard '[tag] (paren)' — only '[tag](paren)'. Ruling v2.190 r1 homes the fix as a
widened, whitespace-tolerant variant of that SAME pattern, HERE at the ``_shape_speech_text``
seam: ``_FORBIDDEN_PATTERNS`` is single-consumer (only ``_shape_speech_text`` reads it, the
full-reply path strips via its own separate constants), so this is DEC-74's explicit-logic
custody, not a coincidental reuse of a pattern that never covered the class.

TK-318 (DEC-69b), Jim verbatim: "I am ok with listening to the full response" — an opt-in,
default-OFF ``speak_full_replies`` flag. When True AND the stage would otherwise shape (voice on,
adapter present, the existing held-chat/voice-turn pass-through gate above unchanged), ``run()``
SKIPS the shaping model call entirely — ZERO model calls, same as the pass-through above — and
instead derives the spoken text from the SAME composed text via ``_sanitize_full_reply_text``: the
TK-317 leading-label strip, then a deterministic markdown/URL token STRIP, then the label strip
RE-APPLIED (batch-review repair: ``**Wombat**: ...`` only exposes its label once the bold markers
are gone — the pre-markdown pass alone would have spoken it) (never a reject — this
mode's text IS the user-visible pane reply, so DEC-55f's reject-to-silence posture would recreate
the exact chat/voice misalignment DEC-69b exists to close), then whitespace collapse, then
word-boundary truncation at the injected ``max_chars``. A blank-after-sanitize result takes the
SAME degrade branch as every other speech-production failure in this stage (``speech_text=None``,
``degraded=True`` — never raise, never verbatim markdown). OFF (the default) is byte-identical to
today — every line below this paragraph is unreachable when ``speak_full_replies=False``.
"""

from __future__ import annotations

import asyncio
import logging
import re

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import StageResult, Transition
from cogworx.loop.stage import StageContext
from cogworx.model.base import ChatMessage

from wombat.config import ConfigurationError, WombatConfig
from wombat.cost.daily_spend_ledger import DailySpendLedger
from wombat.persona.builder import Mouth
from wombat.persona.expression import guard_suffix
from wombat.stages.artifacts import (
    SPEECH_OUTPUT,
    composed_output_from_artifact_data,
    composed_output_held_chat_from_artifact_data,
    composed_output_voice_turn_from_artifact_data,
    speech_output_to_artifact_data,
)
from wombat.voice.expressive import (
    ALLOWED_TAGS,
    find_disallowed_token,
    render_expressive_instruction,
)

logger = logging.getLogger(__name__)

# AC-FIXED (mirrors ComposeStage's Q-50 posture) — not a TK-13 tunable.
_DEFAULT_TIMEOUT_SECONDS = 2.0

# A fixed, plain-spoken-English summarization instruction (DEC-55) — no prompt iteration. The
# guard suffix for Mouth.COMPOSE is appended verbatim at construction (below); no fifth 'speech'
# mouth is introduced (DEC-55e deferral).
_SPEECH_SHAPE_INSTRUCTION = (
    "You summarize one item for the user to be read aloud by text-to-speech. Rewrite it as plain "
    "spoken English, in one or two short, natural sentences. Never use markdown or any other "
    "formatting. Never read a URL or link aloud — describe it in words instead. Never begin your "
    "reply with a name or speaker label followed by a colon."
)

# DEC-55f: the hard brevity bound a validated speech text must fit within. TK-303 (DEC-67e)
# unpins this: it stays the DEFAULT (this constant is still its home), but SpeechShapeStage now
# takes an injected max_chars, threaded from config.wombat_spoken_reply_max_chars at bootstrap.
_MAX_SPEECH_CHARS = 400

# TK-317 (DEC-69a): one anchored, one-shot leading "Label: " strip — a leading letter, then up to
# ~32 chars of letters/digits/apostrophe/hyphen/underscore, then a colon and whitespace, at string
# START only. NO space in the token class (batch-review repair): single-word labels only —
# 'Wombat:'/'Assistant:' still strip, but a legitimate leading clause ('It costs 5: dollars',
# 'By the time we arrive: it will be late') is never eaten; the instruction sentence already
# discourages self-labeling, so multi-word labels are not worth the false positives. Generic by
# design (no configured-assistant-name dependency); a colon anywhere else in the text is never
# touched, and only the FIRST leading match is ever removed.
_LEADING_LABEL_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9'_-]{0,31}:\s+")

# DEC-55f no-placebo validator: one compiled pattern per enumerated closed token class. ANY match
# rejects the whole text outright (see the module docstring) — never a partial strip/rewrite.
_FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\*\*.+?\*\*", re.DOTALL),  # bold (**text**)
    re.compile(r"(?<!\w)_[^_\n]+_(?!\w)"),  # italic (_text_)
    re.compile(r"(?<!\*)\*[^*\n]+\*(?!\*)"),  # italic (*text*)
    re.compile(r"^\s*#{1,6}\s", re.MULTILINE),  # heading (# text)
    re.compile(r"`"),  # backtick / code fence
    # markdown link ([text](url)) — TK-327 (DEC-74, explicit-logic custody homed here, single-
    # consumer per ruling v2.190 r1): the gap between "]" and "(" is now whitespace-tolerant so
    # the '[tag] (paren)' adjacency hazard (e.g. '[break] (see below)') trips this SAME pattern
    # too, the pinned safe direction (reject, never mangle); a bare allowed tag followed by
    # ordinary prose parentheses elsewhere is unaffected. DEC-74's rule is a bracketed group plus
    # optional whitespace plus an OPEN paren — it does NOT require a closing paren or any content
    # inside (batch-review repair: the prior ``\([^)]+\)`` tail let an unterminated/empty paren
    # ('[break] (see below', '[break] ()', '[break](') slip past the reject; ordinary model output
    # is not guaranteed to balance parens). DEC-72c's original adjacency mechanism (zero-space
    # only) is superseded in part by this widening — DEC-72c's INTENT (safe-direction rejection of
    # the whole adjacency class) is what this pattern now actually delivers.
    re.compile(r"\[[^\]]+\]\s*\("),
    re.compile(r"https?://\S+", re.IGNORECASE),  # raw URL
    re.compile(r"^\s*[-*+]\s", re.MULTILINE),  # bullet list marker
    re.compile(r"^\s*\d+\.\s", re.MULTILINE),  # numbered list marker
)


def _shape_speech_text(
    raw_text: str | None,
    max_chars: int = _MAX_SPEECH_CHARS,
    allowed_tags: frozenset[str] = frozenset(),
) -> str | None:
    """DEC-55f no-placebo validator: ``raw_text`` unchanged (trimmed) if it is free of every
    enumerated forbidden token class and within ``max_chars`` (TK-303/DEC-67e: defaults to the
    pinned ``_MAX_SPEECH_CHARS``, injectable so ``SpeechShapeStage`` can carry a configured
    bound); otherwise ``None`` (unsanitizable/overlong/blank -> no speech text, never a rewritten
    guess).

    TK-317 (DEC-69a): FIRST, an anchored one-shot strip removes a leading "Label: " speaker-label
    prefix (if any) — BEFORE the length check, so the remaining body keeps the full ``max_chars``
    budget. This is a STRIP, never a reject: a leading label cannot mangle what follows it.

    TK-327 (DEC-71d as revised by DEC-72c/i): LAST, ``allowed_tags`` (empty unless
    ``expressive_tags`` is on) is the emission-policy guarantee — ANY bracketed ``[...]`` token
    not EXACTLY in ``allowed_tags`` rejects the WHOLE text to ``None``, same no-placebo posture as
    every check above it. Prose parentheses are never bracketed tokens and are never touched."""
    if raw_text is None:
        return None
    unlabeled = _LEADING_LABEL_PATTERN.sub("", raw_text, count=1)
    stripped = unlabeled.strip()
    if not stripped or len(stripped) > max_chars:
        return None
    for pattern in _FORBIDDEN_PATTERNS:
        if pattern.search(stripped):
            return None
    if find_disallowed_token(stripped, allowed_tags) is not None:
        return None
    return stripped


# TK-318 (DEC-69b): the deterministic STRIP (never reject) token classes for the
# wombat_speak_full_replies=True path — same enumerated markdown/URL surface as
# ``_FORBIDDEN_PATTERNS`` above, but each is REMOVED rather than triggering a whole-text reject
# (see the module docstring: the text here IS the user-visible pane reply, so reject-to-silence
# would recreate the exact chat/voice misalignment DEC-69b exists to close). Markdown links are
# reduced to their link text FIRST, before the bare-URL drop, so a link's own URL never survives
# as a dangling bare URL.
_FULL_REPLY_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_FULL_REPLY_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_FULL_REPLY_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
# Opus-verify repair: underscore BOLD (__text__) — the single-underscore italic pattern below can
# never match it (its char class excludes '_'), so '__Wombat__: ...' survived the strip and was
# spoken verbatim, label and all. Handled BEFORE the single-underscore italic so nesting resolves.
_FULL_REPLY_BOLD_UNDERSCORE_RE = re.compile(r"__(.+?)__", re.DOTALL)
_FULL_REPLY_ITALIC_UNDERSCORE_RE = re.compile(r"(?<!\w)_([^_\n]+)_(?!\w)")
_FULL_REPLY_ITALIC_ASTERISK_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_FULL_REPLY_HEADING_RE = re.compile(r"^\s*#{1,6}\s+", re.MULTILINE)
_FULL_REPLY_BACKTICK_RE = re.compile(r"`")
_FULL_REPLY_BULLET_RE = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_FULL_REPLY_NUMBERED_RE = re.compile(r"^\s*\d+\.\s+", re.MULTILINE)
_FULL_REPLY_WHITESPACE_RUN_RE = re.compile(r"\s+")


def _strip_markdown_tokens(text: str) -> str:
    """TK-318 (DEC-69b): one deterministic pass removing every enumerated markdown/URL token
    class — a link becomes its own text, a bare URL is dropped, bold/italic/heading/backtick
    markers are removed, bullet/numbered list markers are removed. Content-independent (no intent
    inspection, mirrors ``compose.brief_template._sanitize_display_text``'s deterministic
    posture) and STRIP-not-reject, unlike ``_FORBIDDEN_PATTERNS`` above."""
    stripped = _FULL_REPLY_LINK_RE.sub(r"\1", text)
    stripped = _FULL_REPLY_URL_RE.sub("", stripped)
    stripped = _FULL_REPLY_BOLD_RE.sub(r"\1", stripped)
    stripped = _FULL_REPLY_BOLD_UNDERSCORE_RE.sub(r"\1", stripped)
    stripped = _FULL_REPLY_ITALIC_UNDERSCORE_RE.sub(r"\1", stripped)
    stripped = _FULL_REPLY_ITALIC_ASTERISK_RE.sub(r"\1", stripped)
    stripped = _FULL_REPLY_HEADING_RE.sub("", stripped)
    stripped = _FULL_REPLY_BACKTICK_RE.sub("", stripped)
    stripped = _FULL_REPLY_BULLET_RE.sub("", stripped)
    stripped = _FULL_REPLY_NUMBERED_RE.sub("", stripped)
    return stripped


def _truncate_at_word_boundary(text: str, max_chars: int) -> str:
    """``text`` (already within-budget if <= ``max_chars``) else cut at ``max_chars`` and back off
    to the last preceding space, so the result never lands mid-word — a single word longer than
    ``max_chars`` is returned cut exactly at the cap (no boundary exists to back off to)."""
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    return truncated.rstrip()


def _sanitize_full_reply_text(raw_text: str, max_chars: int) -> str | None:
    """TK-318 (DEC-69b): the wombat_speak_full_replies=True path's deterministic sanitize —
    ``raw_text`` (the SAME composed text ``compose`` produced) run through the TK-317 leading-
    label strip, then ``_strip_markdown_tokens``, then the label strip RE-APPLIED (batch-review
    repair: ``**Wombat**: ...`` only exposes its leading label once the bold markers are gone),
    then whitespace collapse, then word-boundary truncation at ``max_chars``. ``None`` iff the
    result is blank after sanitizing — the stage's existing degrade branch handles that (never
    raise, never verbatim markdown)."""
    unlabeled = _LEADING_LABEL_PATTERN.sub("", raw_text, count=1)
    token_stripped = _strip_markdown_tokens(unlabeled)
    token_stripped = _LEADING_LABEL_PATTERN.sub("", token_stripped, count=1)
    collapsed = _FULL_REPLY_WHITESPACE_RUN_RE.sub(" ", token_stripped).strip()
    if not collapsed:
        return None
    return _truncate_at_word_boundary(collapsed, max_chars)


class SpeechShapeStage:
    """Produces the spoken-channel summary via a SECOND DeepSeek mouth call; degrades to no
    speech (never composed text) on any failure (TK-267, DEC-55)."""

    name: str = "speech_shape"
    transitions: tuple[str, ...] = ("speak",)

    def __init__(
        self,
        *,
        config: WombatConfig,
        voice_enabled: bool,
        adapter_present: bool,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        spend_ledger: DailySpendLedger | None = None,
        daily_token_ceiling: int | None = None,
        max_chars: int = _MAX_SPEECH_CHARS,
        speak_full_replies: bool = False,
        expressive_tags: bool = False,
    ) -> None:
        # Mirrors ComposeStage's AC3: fail at CONSTRUCTION when this mouth WILL be called (voice
        # on + adapter present) but the shared deepseek profile has no key to build against.
        deepseek_key = config.deepseek_api_key.get_secret_value().strip()
        if voice_enabled and adapter_present and not deepseek_key:
            msg = "SpeechShapeStage: DEEPSEEK_API_KEY is missing/blank; the mouth cannot be wired"
            raise ConfigurationError(msg)
        self._voice_enabled = voice_enabled
        self._adapter_present = adapter_present
        self._timeout_seconds = timeout_seconds
        self._spend_ledger = spend_ledger
        self._daily_token_ceiling = daily_token_ceiling
        # TK-303 (DEC-67e): injected max_chars, defaulting to the pinned _MAX_SPEECH_CHARS —
        # bootstrap.build_speech_shape_stage threads config.wombat_spoken_reply_max_chars here.
        self._max_chars = max_chars
        # TK-318 (DEC-69b): default-OFF — bootstrap.build_speech_shape_stage threads
        # config.wombat_speak_full_replies here.
        self._speak_full_replies = speak_full_replies
        # TK-327 (DEC-71c as revised by DEC-72d): default-OFF — bootstrap threads the
        # key-gated, constructed-adapter decision here (TK-328). False = ALLOWED_TAGS stays
        # empty and the instruction below is byte-identical to today (the pin).
        self._expressive_tags = expressive_tags
        self._allowed_tags: frozenset[str] = ALLOWED_TAGS if expressive_tags else frozenset()
        # Built ONCE — the FIXED prompt (DEC-55) plus Mouth.COMPOSE's guard suffix, appended
        # verbatim via the read-only persona.expression seam (no fifth 'speech' mouth, DEC-55e).
        # TK-327: expressive_tags extends the SAME join with one more element — the definitions
        # block + placement rules — rather than altering the first two (byte pin when off).
        instruction_parts = [_SPEECH_SHAPE_INSTRUCTION, guard_suffix(Mouth.COMPOSE)]
        if expressive_tags:
            instruction_parts.append(render_expressive_instruction())
        self._system_instruction = " ".join(instruction_parts)

    async def run(self, ctx: StageContext) -> StageResult:
        art = await ctx.last_output("compose")
        if art is None:
            msg = "speech_shape: no compose output available yet"
            raise RuntimeError(msg)
        composed_text, item_id, item_kind, _degraded = composed_output_from_artifact_data(art.data)
        held_chat = composed_output_held_chat_from_artifact_data(art.data)
        voice_turn = composed_output_voice_turn_from_artifact_data(art.data)

        if not self._voice_enabled or not self._adapter_present or (held_chat and not voice_turn):
            # ZERO model calls (AC4): a silent pass-through — the voice-off/no-adapter outcome at
            # speak stays byte-identical to today. DEC-57/TK-272: a held chat reply takes this
            # EXACT same voice-off shape (speech_text=None, degraded=False) — quiet-by-design,
            # never a model call, never routed through the degraded-warning branch downstream.
            return Transition(
                to="speak",
                output=Artifact(
                    kind=SPEECH_OUTPUT,
                    produced_by=self.name,
                    provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                    data=speech_output_to_artifact_data(item_id, item_kind, None, False),
                ),
            )

        if self._speak_full_replies:
            # TK-318 (DEC-69b): ZERO model calls — the pane's actual composed text, deterministic-
            # sanitized under the spoken cap, IS the spoken text. A blank-after-sanitize result
            # takes the same degrade branch as every other speech-production failure below.
            full_reply_text = _sanitize_full_reply_text(composed_text, self._max_chars)
            full_reply_degraded = full_reply_text is None
            if full_reply_degraded:
                logger.warning(
                    "speech_shape: wombat_speak_full_replies sanitize produced no speech text "
                    "(blank after stripping); degrading to no speech"
                )
            return Transition(
                to="speak",
                output=Artifact(
                    kind=SPEECH_OUTPUT,
                    produced_by=self.name,
                    provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                    data=speech_output_to_artifact_data(
                        item_id, item_kind, full_reply_text, full_reply_degraded
                    ),
                ),
            )

        messages = [
            ChatMessage(role="system", content=self._system_instruction),
            ChatMessage(role="user", content=composed_text),
        ]

        degraded = False
        speech_text: str | None = None

        # TK-9 layer 2 PRE-call gate (Q-68), mirroring ComposeStage: only armed if both are wired.
        if self._spend_ledger is not None and self._daily_token_ceiling is not None:
            try:
                spent_today = self._spend_ledger.tokens_spent_today()
            except Exception:
                logger.warning(
                    "speech_shape: daily spend ledger read failed; failing closed to no speech "
                    "without calling the model",
                    exc_info=True,
                )
                degraded = True
            else:
                if spent_today >= self._daily_token_ceiling:
                    logger.warning(
                        "speech_shape: daily token ceiling reached (%d spent >= %d ceiling); "
                        "degrading to no speech without calling the model",
                        spent_today,
                        self._daily_token_ceiling,
                    )
                    degraded = True

        response = None
        if not degraded:
            try:
                response = await asyncio.wait_for(
                    ctx.model.complete(messages=messages), timeout=self._timeout_seconds
                )
            except asyncio.CancelledError:
                # Never swallow cancellation — only the mouth's own failures degrade.
                raise
            except Exception:
                logger.warning(
                    "speech_shape: model call failed; degrading to no speech", exc_info=True
                )
                degraded = True

        if not degraded and response is not None:
            # TK-9 layer 2 POST-call accounting: a genuinely successful call records what it
            # spent, regardless of whether the returned text later fails validation below — the
            # call already spent the tokens either way. A ledger WRITE failure only logs loud.
            tokens_spent = response.usage.prompt_tokens + response.usage.completion_tokens
            if self._spend_ledger is not None:
                try:
                    self._spend_ledger.add_tokens(tokens_spent)
                except Exception:
                    logger.warning(
                        "speech_shape: daily spend ledger write failed; speech output stands",
                        exc_info=True,
                    )
            speech_text = _shape_speech_text(response.text, self._max_chars, self._allowed_tags)
            if speech_text is None:
                logger.warning(
                    "speech_shape: model response failed speech validation (blank, overlong, or "
                    "carrying a forbidden token class); degrading to no speech"
                )
                degraded = True

        if degraded:
            speech_text = None

        return Transition(
            to="speak",
            output=Artifact(
                kind=SPEECH_OUTPUT,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data=speech_output_to_artifact_data(item_id, item_kind, speech_text, degraded),
            ),
        )


__all__ = ["SpeechShapeStage"]
