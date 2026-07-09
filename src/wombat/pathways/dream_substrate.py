"""wombat.pathways.dream_substrate — the dream-only substrate provider (TK-54, EP-13).

Constructs the FOUR collaborators TK-47's off-path sweepers (``CoherenceReconciler``,
``ClaimExtractor``) require: a ``CoherenceStore``, a ``ConsistencyOracle``, a budget-guarded
``Model``, and a cogworx ``SourceRegistry``. This module is pure composition — it never drives a
reconciler/extractor tick itself (TK-47's job) and it NEVER touches the gate path (NG-4/DEC-23 —
the dream model is off-path-only inference).

STORE IDENTITY (Q-90/TK-176 continuity): ``store`` is the SAME ``entity_kg`` instance the caller
passes in, never a second store — cog-worx's ``InMemoryEntityKG``/``Neo4jEntityKG`` each implement
the FULL ``EntityKG`` contract AND the separate ``CoherenceStore`` Protocol on the one concrete
class, but the two Protocols do not structurally overlap (``EntityKG``'s own declaration carries
none of ``CoherenceStore``'s methods) — hence the explicit ``cast`` below, not a second seam.

BUDGET (DEC-23 bounded off-path inference, the Q-90-normalized AC2): ``build_model`` composes
``StructuredOutputModel(BudgetGuardedModel(adapter, guard))`` — the ``BudgetGuard`` ceiling fires
INSIDE ``BudgetGuardedModel.complete`` BEFORE the inner adapter is ever invoked, so a ceiling
already at/over its limit refuses the call with zero network I/O (S11 pre-call, structural).

BUDGET RESET CADENCE (TK-180, CR2-3): the ceiling is PER-NIGHT, not per-process-lifetime.
``BudgetGuard`` itself is one-shot with no reset, so ``DreamSubstrate.model`` is a thin
wombat-owned ``_NightBudgetedModel`` wrapper keyed to the wombat NIGHT (``wombat_today``,
DEC-21) rather than the raw ``build_model(...)`` result: the first call each wombat-night mints a
FRESH ``BudgetGuard`` and rebuilds the inner budget-guarded model via the SAME
``build_model(spec, guard=..., client=...)`` composition above; within one night the ceiling
accumulates exactly as before (correct — TK-52's once-per-night fence means one dream drive per
night). This keeps the collaborators (``CoherenceReconciler``/``ClaimExtractor`` via
``ModelConsistencyOracle``) constructed ONCE at boot over the wrapper (TK-47's keyword-injection
shape holds) while the ceiling itself renews every night instead of accumulating across the
process lifetime.

RESIDENCY EXEMPTION (ASMP-1/Q-87): the consolidation model's endpoint (``deepseek_base_url``) is
the ONE allowed egress — this module imports NOTHING from ``wombat.safety.local_residency`` and
runs no residency check of its own. Store residency (the entity-KG persistence layer, once it is
non-in-memory) rides the substrate real-adapter path per TK-150; that is a SEPARATE check at a
SEPARATE seam, not duplicated here.

NAME COLLISION (flagged, do not confuse): the ``source_registry`` this module builds is
``cogworx.knowledge.source_registry.SourceRegistry`` (a declaration-time source-IDENTITY
registry consumed by ``ClaimExtractor``) — NOT ``wombat.sources.registry.SourceRegistry`` (the
drain-side fetch-source registry). Two distinct types that happen to share a class name.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from cogworx.coherence.oracle import ConsistencyOracle, ModelConsistencyOracle
from cogworx.cost.budget import BudgetGuard
from cogworx.knowledge.source_registry import SourceKind, SourceRegistry
from cogworx.model.base import (
    ChatMessage,
    Model,
    ModelCapabilities,
    ModelResponse,
    ModelTier,
    ToolSpec,
)
from cogworx.model.registry import ModelSpec, build_model
from cogworx.substrate.coherence import CoherenceStore
from cogworx.substrate.entity_kg import EntityKG

from wombat.domain.daily_ledger import wombat_today
from wombat.params import OperatingParams

__all__ = ["DreamSubstrate", "build_dream_substrate"]

# The default night-key clock/zone (TK-180): production boot never threads its own tz/clock
# through build_dream_substrate (bootstrap.py is out of scope for this ticket), so the night
# boundary is resolved in UTC off the real wall clock unless a test injects its own seam.
_UTC_ZONE = ZoneInfo("UTC")


def _utc_now() -> datetime:
    return datetime.now(UTC)


# wombat's own declared source for dream-time claim extraction (the ClaimExtractor consumer,
# TK-47): a "system" source — this is wombat's own off-path inference, never a human/tool/
# document/agent source. Full authority: the framework, not a model, mints this declaration.
_DREAM_SOURCE_KIND: SourceKind = "system"
_DREAM_SOURCE_REF = "wombat.dream"
_DREAM_SOURCE_AUTHORITY = 1.0


class _NightBudgetedModel:
    """A thin wombat-owned ``Model``-protocol wrapper that mints a FRESH ``BudgetGuard`` (and
    rebuilds the inner ``build_model(...)`` composition) once per wombat NIGHT (TK-180, CR2-3).

    ``BudgetGuard`` is one-shot with no reset, so wiring it directly into the process-lifetime
    model (the pre-TK-180 shape) let its counters accumulate forever — after
    ``dream_budget_max_calls`` cumulative calls (days of normal operation) every subsequent call
    raised ``BudgetExceededError``, silently no-op'ing consolidation until restart. This wrapper
    is constructed ONCE at boot (the TK-47 keyword-injection shape holds —
    ``ModelConsistencyOracle``/``ClaimExtractor``/``CoherenceReconciler`` all close over this ONE
    instance) but re-derives
    which inner model backs it on every call: the wombat-day boundary (``wombat_today``, DEC-21) is
    resolved fresh via the injected ``clock``/``tz`` seam, and a night key change swaps in a brand
    new budget-guarded inner model. Within one night the ceiling accumulates exactly as before
    (TK-52's once-per-night fence means one dream drive per night, so this is a no-op in the
    common case — the rebuild only fires on the FIRST call of a new night).
    """

    def __init__(
        self,
        *,
        spec: ModelSpec,
        params: OperatingParams,
        client: Any,
        clock: Callable[[], datetime],
        tz: ZoneInfo,
    ) -> None:
        self._spec = spec
        self._params = params
        self._client = client
        self._clock = clock
        self._tz = tz
        self._night: date | None = None
        self._inner: Model | None = None

    def _current(self) -> Model:
        """Return the inner model for TONIGHT, rebuilding (with a fresh ``BudgetGuard``) the
        first time this is called on a new wombat-night."""
        night = wombat_today(self._clock(), self._tz)
        if self._inner is None or night != self._night:
            guard = BudgetGuard(
                max_usd=self._params.dream_budget_max_usd,
                max_calls=self._params.dream_budget_max_calls,
            )
            self._inner = build_model(self._spec, guard=guard, client=self._client)
            self._night = night
        return self._inner

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._current().capabilities

    async def complete(
        self,
        *,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        tier: ModelTier = "pro",
        json_schema: Mapping[str, Any] | None = None,
    ) -> ModelResponse:
        return await self._current().complete(
            messages=messages, tools=tools, tier=tier, json_schema=json_schema
        )

    def count_tokens(self, text: str) -> int:
        return self._current().count_tokens(text)


@dataclass(frozen=True, slots=True)
class DreamSubstrate:
    """The four dream-only collaborators TK-47's sweepers consume. Built ONCE per process by
    ``build_dream_substrate``; nothing here drives a reconciler/extractor tick."""

    store: CoherenceStore
    oracle: ConsistencyOracle
    model: Model
    source_registry: SourceRegistry


def build_dream_substrate(
    *,
    entity_kg: EntityKG,
    spec: ModelSpec,
    params: OperatingParams,
    client: Any = None,
    clock: Callable[[], datetime] = _utc_now,
    tz: ZoneInfo = _UTC_ZONE,
) -> DreamSubstrate:
    """Assemble the dream substrate (TK-54).

    ``entity_kg`` is the SAME shared instance the caller already threads elsewhere (e.g. TK-176's
    ``RuntimeBundle.entity_kg``) — it becomes ``DreamSubstrate.store`` verbatim, never copied or
    rebuilt. ``spec`` is the caller-supplied ``ModelSpec`` (production callers reuse the SAME
    descriptor ``bootstrap.py`` builds for the drain-side ``"deepseek"`` profile — this module
    never constructs its own). ``params`` supplies the DEC-23 budget ceiling
    (``dream_budget_max_usd``/``dream_budget_max_calls``), now resolved PER-NIGHT rather than
    once for the process lifetime (TK-180, CR2-3 — see ``_NightBudgetedModel``). ``client`` is the
    injectable adapter client seam ``build_model``/``OpenAICompatModel`` already expose — the
    zero-network test seam (a canned/spy client in tests, ``None`` in production for a real SDK
    client). ``clock``/``tz`` are the injectable wombat-night seam (mirroring ``DailyLedger``'s
    idiom, DEC-21): ``clock`` defaults to the real UTC wall clock and ``tz`` to UTC — production
    boot (``bootstrap.py``) does not thread its own tz through this call, only tests inject a
    fixed clock to drive the night boundary deterministically.
    """
    model: Model = _NightBudgetedModel(
        spec=spec, params=params, client=client, clock=clock, tz=tz
    )
    oracle = ModelConsistencyOracle(model, tier="flash")

    source_registry = SourceRegistry()
    source_registry.declare(
        _DREAM_SOURCE_KIND, _DREAM_SOURCE_REF, authority=_DREAM_SOURCE_AUTHORITY
    )

    # See module docstring "STORE IDENTITY": EntityKG's own Protocol declaration does not carry
    # CoherenceStore's methods, so a value merely typed EntityKG does not structurally satisfy
    # CoherenceStore even though the concrete cog-worx classes implement both on one object.
    store = cast(CoherenceStore, entity_kg)

    return DreamSubstrate(store=store, oracle=oracle, model=model, source_registry=source_registry)
