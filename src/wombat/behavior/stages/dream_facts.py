"""DreamFactsStage — the nightly off-path getting-to-know pass (TK-297, EP-13, DEC-65g, DEC-23
admission, RatingTuner-pattern custody).

Inserted into the ``wombat.dream`` graph between ``dream_persona`` (TK-214) and
``dream_derive`` (TK-299, which now sits between this stage and ``dream_behavior_log`` — TK-299's
own mechanical splice, mirroring the one TK-214 made between ``dream_tune`` and
``dream_behavior_log`` before this stage existed) (``pathways/dream_pathway.py``).

Keyword-injected collaborators only (``DreamTuneStage``/``DreamPersonaStage`` precedent):
``model`` is the SAME budget-guarded ``Model`` every other dream-consolidation call site uses
(``pathways.dream_substrate.build_dream_substrate``'s ``_NightBudgetedModel`` wrapper — this stage
NEVER constructs a model or a second guard, DEC-23); ``chat_turns`` is ``wombat.chat_turns.
ChatTurnStore`` (TK-295, the dream extractor's ONLY organic reader per that module's own
docstring); ``user_facts`` is ``wombat.user_facts.UserFactsStore`` (TK-294), this stage's ONLY
organic write path.

READ: ``chat_turns.turns_since(now - _LOOKBACK_HOURS)`` — a fixed 36-hour trailing window. ZERO
turns means NO model call at all (an idle night costs nothing) and the stage transitions on
immediately with an empty report.

EXTRACT (DEC-23 admission — the ONE model call this stage ever makes): the system instruction asks
for durable facts the user STATED about themselves — people, preferences, running jokes — one per
line, third person, and embeds ``wombat.persona.expression.guard_suffix(Mouth.REFLECTION)``
VERBATIM (never a re-typed copy) — the SAME CON-6 never-clinical/never-motive bar the reflection
mouth already carries. The model's raw output is a PROPOSAL only, never trusted as-is.

DETERMINISTIC POST-FILTER (the custody, mirrors ``RatingTuner``'s own bounded-adaptation posture):
parse one-fact-per-line; drop any line over ``_MAX_FACT_LINE_CHARS`` characters; drop any line
whose casefolded text contains a ``_FORBIDDEN_FACT_TOKENS`` substring (the reflection clinical-term
list, restated here as a screen); ``fact_key`` is a stable ``sha256`` hexdigest of the
casefolded/whitespace-collapsed fact text; a key already present in ``user_facts`` is skipped
(dedupe — read once via ``count``/``list_facts``, never a second store round-trip per candidate);
accepted candidates are capped at the pinned ``_MAX_NEW_FACTS_PER_NIGHT = 5`` — every drop is
logged loud and by reason. Each surviving fact is written via ``user_facts.upsert_fact(key, fact,
source="dream")`` plus ONE INFO journal line per accepted fact (CON-4) — mirrors
``DreamPersonaStage``'s own logger-not-``ctx.journal`` posture (dream stages never touch
``ctx.journal`` directly).

NEVER BLOCKS: a raising ``chat_turns.turns_since`` is treated exactly like zero turns (no model
call, transition on); a raising ``model.complete`` degrades to an empty proposal (no facts land
that night); a raising dedupe read or a raising ``upsert_fact`` for one candidate is caught, logged
LOUD, and skipped — the facts already upserted before the failure stay written (mirrors
``DreamBehaviorLogStage``'s own per-item catch-and-skip posture). One bad night's extraction pass
never blocks the reachable terminal.

OUT OF SCOPE (DEF-8, CON-1): no persona-matrix/gate/rating write of any kind — facts never touch
``LivePersona`` or any rating parameter; no per-turn (in-path) extraction; no second model call, no
retry loop, no embeddings/RAG; no facts deletion beyond ``UserFactsStore``'s own cap.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import StageResult, Transition
from cogworx.loop.stage import StageContext
from cogworx.model.base import ChatMessage, Model

from wombat.chat_turns import ChatTurnStore
from wombat.persona.builder import Mouth
from wombat.persona.expression import guard_suffix
from wombat.user_facts import UserFactsStore

logger = logging.getLogger(__name__)

# DreamFactsStage's committed output kind (TK-297) — a contentless, system-provenance count
# artifact mirroring dream_pathway.py's own DREAM_*_REPORT_KIND idiom: no fact text rides this
# artifact, only counts — the durable record is the wombat_user_facts rows the stage upserted.
DREAM_FACTS_REPORT_KIND = "wombat.dream_facts_report"

# The trailing read window (TK-297 ruling): a fixed 36-hour lookback over wombat_chat_turns — not
# a tunable, a module constant.
_LOOKBACK_HOURS = 36

# Pinned hard cap (DEC-63 no-knob precedent, restated here) — the deterministic custody over the
# model's proposal, never a setting.
_MAX_NEW_FACTS_PER_NIGHT = 5

# A candidate line longer than this is dropped loudly rather than truncated (an honest drop, never
# a silent mangle).
_MAX_FACT_LINE_CHARS = 200

# The reflection mouth's own clinical/motive-inference term screen (persona/expression.py's
# _GUARD_SUFFIX["reflection"] text), restated here as a deterministic casefold-substring drop —
# CON-6 custody at the ONE organic write path into UserFactsStore. This stage's own extraction
# instruction demands THIRD-PERSON output (unlike the reflection mouth, which speaks directly to
# the user), so both the second-person source phrasing AND its third-person conjugation are
# screened — a model honoring the third-person instruction still gets caught.
_FORBIDDEN_FACT_TOKENS: frozenset[str] = frozenset(
    {
        "clinical",
        "diagnosis",
        "disorder",
        "symptom",
        "therapy",
        "indicates a pattern",
        "you seem to",
        "seems to",
        "you tend to",
        "tends to",
        "because you",
        "because they",
        "due to your",
        "due to their",
    }
)

# The fixed extraction instruction — a fact request plus the reflection mouth's own immutable
# guard suffix, imported verbatim (never re-typed) so the CON-6 bar can never drift out of sync
# with the reflection mouth's own copy (TK-297 ruling r3).
_EXTRACTION_INSTRUCTION = (
    "Read the user's own chat turns below and extract any durable facts they stated about "
    "themselves — people, preferences, running jokes. Write each fact as ONE line, in the third "
    "person (e.g. 'The user prefers early mornings.'), describing only what the user said or did. "
    "Output ONLY the fact lines and nothing else — if nothing durable was stated, output nothing. "
) + guard_suffix(Mouth.REFLECTION)


def _fact_key(text: str) -> str:
    """A stable ``sha256`` hexdigest of the casefolded, whitespace-collapsed fact text — the
    dedupe/idempotency key TK-297 rules (never re-derived from anything but the text itself)."""
    normalized = " ".join(text.split()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _parse_candidates(raw_text: str) -> list[str]:
    """One-fact-per-line parse, dropping blank lines, over-long lines, and forbidden-token lines
    — each drop logged loud and by reason. Order-preserving; the cap is enforced by the caller at
    accept time, not here."""
    candidates: list[str] = []
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if len(line) > _MAX_FACT_LINE_CHARS:
            logger.warning(
                "dream_facts: dropping over-long candidate line (%d chars): %r",
                len(line),
                line,
            )
            continue
        casefolded = line.casefold()
        hit = next((token for token in _FORBIDDEN_FACT_TOKENS if token in casefolded), None)
        if hit is not None:
            logger.warning(
                "dream_facts: dropping forbidden-token candidate line (token=%r): %r", hit, line
            )
            continue
        candidates.append(line)
    return candidates


def _existing_fact_keys(user_facts: UserFactsStore) -> set[str]:
    """Every ``fact_key`` already in ``user_facts`` — read ONCE per run (``count`` then
    ``list_facts(count)``), never a second store round-trip per candidate."""
    total = user_facts.count()
    if total == 0:
        return set()
    return {row["fact_key"] for row in user_facts.list_facts(total)}


class DreamFactsStage:
    """The nightly off-path getting-to-know pass (TK-297, EP-13, DEC-65g). See the module
    docstring for the full read/extract/filter/write contract."""

    name: str = "dream_facts"
    transitions: tuple[str, ...] = ("dream_derive",)

    def __init__(
        self, *, model: Model, chat_turns: ChatTurnStore, user_facts: UserFactsStore
    ) -> None:
        self._model = model
        self._chat_turns = chat_turns
        self._user_facts = user_facts

    async def run(self, ctx: StageContext) -> StageResult:
        now = ctx.clock()
        new_facts = 0

        try:
            turns = self._chat_turns.turns_since(now - timedelta(hours=_LOOKBACK_HOURS))
        except Exception:
            logger.error(
                "dream_facts: ChatTurnStore.turns_since failed; tonight's extraction pass is "
                "skipped",
                exc_info=True,
            )
            turns = []

        if turns:
            try:
                turns_text = "\n".join(f"- {turn['text']}" for turn in turns)
                response = await self._model.complete(
                    messages=[
                        ChatMessage(role="system", content=_EXTRACTION_INSTRUCTION),
                        ChatMessage(role="user", content=turns_text),
                    ]
                )
                raw_text = response.text or ""
            except Exception:
                logger.error(
                    "dream_facts: model extraction call failed; tonight's extraction pass is "
                    "skipped",
                    exc_info=True,
                )
                raw_text = ""

            candidates = _parse_candidates(raw_text)
            if candidates:
                try:
                    existing_keys = _existing_fact_keys(self._user_facts)
                except Exception:
                    logger.error(
                        "dream_facts: reading existing facts for dedupe failed; proceeding as if "
                        "no facts exist yet",
                        exc_info=True,
                    )
                    existing_keys = set()

                for candidate in candidates:
                    if new_facts >= _MAX_NEW_FACTS_PER_NIGHT:
                        break
                    key = _fact_key(candidate)
                    if key in existing_keys:
                        logger.info(
                            "dream_facts: dropping duplicate candidate fact_key=%s", key
                        )
                        continue
                    try:
                        self._user_facts.upsert_fact(key, candidate, source="dream")
                    except Exception:
                        logger.error(
                            "dream_facts: upsert_fact failed for fact_key=%s; skipping", key,
                            exc_info=True,
                        )
                        continue
                    existing_keys.add(key)
                    new_facts += 1
                    logger.info("dream_facts: accepted new fact fact_key=%s", key)

        return Transition(
            to="dream_derive",
            output=Artifact(
                kind=DREAM_FACTS_REPORT_KIND,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
                data={"new_facts": new_facts},
            ),
        )


__all__ = ["DREAM_FACTS_REPORT_KIND", "DreamFactsStage"]
