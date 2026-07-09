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

from dataclasses import dataclass
from typing import Any, cast

from cogworx.coherence.oracle import ConsistencyOracle, ModelConsistencyOracle
from cogworx.cost.budget import BudgetGuard
from cogworx.knowledge.source_registry import SourceKind, SourceRegistry
from cogworx.model.base import Model
from cogworx.model.registry import ModelSpec, build_model
from cogworx.substrate.coherence import CoherenceStore
from cogworx.substrate.entity_kg import EntityKG

from wombat.params import OperatingParams

__all__ = ["DreamSubstrate", "build_dream_substrate"]

# wombat's own declared source for dream-time claim extraction (the ClaimExtractor consumer,
# TK-47): a "system" source — this is wombat's own off-path inference, never a human/tool/
# document/agent source. Full authority: the framework, not a model, mints this declaration.
_DREAM_SOURCE_KIND: SourceKind = "system"
_DREAM_SOURCE_REF = "wombat.dream"
_DREAM_SOURCE_AUTHORITY = 1.0


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
) -> DreamSubstrate:
    """Assemble the dream substrate (TK-54).

    ``entity_kg`` is the SAME shared instance the caller already threads elsewhere (e.g. TK-176's
    ``RuntimeBundle.entity_kg``) — it becomes ``DreamSubstrate.store`` verbatim, never copied or
    rebuilt. ``spec`` is the caller-supplied ``ModelSpec`` (production callers reuse the SAME
    descriptor ``bootstrap.py`` builds for the drain-side ``"deepseek"`` profile — this module
    never constructs its own). ``params`` supplies the DEC-23 budget ceiling
    (``dream_budget_max_usd``/``dream_budget_max_calls``). ``client`` is the injectable adapter
    client seam ``build_model``/``OpenAICompatModel`` already expose — the zero-network test seam
    (a canned/spy client in tests, ``None`` in production for a real SDK client).
    """
    guard = BudgetGuard(
        max_usd=params.dream_budget_max_usd, max_calls=params.dream_budget_max_calls
    )
    model = build_model(spec, guard=guard, client=client)
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
