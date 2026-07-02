"""StageContextFake — the reusable StageContext test double (TK-5, Q-47).

cog-worx ships NO ``StageContext`` double (verified 2026-07-02). This fake implements the FULL
``cogworx.loop.stage.StageContext`` Protocol, raising ``NotImplementedError`` on every member
EXCEPT ``clock`` (an injected, controllable datetime source) and ``last_output`` (a configurable
mapping, defaulting to empty — for TK-6/7/8/10 reuse once stages start routing on upstream
output). A stage under test that reaches for any other ``ctx`` member fails loudly instead of
silently degrading, catching accidental ctx-surface creep (Q-47).

Reusable: later spine tickets (TK-6/7/8/10) import this same fake for their own Stage tests.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from cogworx.claims.provenance import Artifact
from cogworx.coordination.events import Event
from cogworx.cost.budget import BudgetGuard
from cogworx.model.base import Model

if TYPE_CHECKING:
    from cogworx.context.types import AssembledCallContext, ContextPolicy, ContextRequest
    from cogworx.injection.policy import InjectedMemory, MemoryPolicy
    from cogworx.recall.query import RecallQuery
    from cogworx.substrate.graph_store import GraphStore
    from cogworx.substrate.journal import Journal
    from cogworx.substrate.latent import LatentStore

_UNWIRED = (
    "StageContextFake: ctx.{member} is not wired — this stage touched ctx surface "
    "beyond clock()/last_output()"
)


@dataclass
class StageContextFake:
    """A ``StageContext`` double. Only ``clock`` and ``last_output`` are implemented.

    ``now_fn`` is required (no default) — the caller injects a fixed or steppable datetime
    source. ``last_output_map`` is optional and defaults to empty (``last_output`` then returns
    ``None`` for any stage name, matching the real Protocol's documented "no output yet" case).
    """

    now_fn: Callable[[], datetime]
    last_output_map: Mapping[str, Artifact | None] = field(default_factory=dict)
    run_id: str = "fake-run"
    session_id: str = "fake-session"

    @property
    def budget(self) -> BudgetGuard:
        raise NotImplementedError(_UNWIRED.format(member="budget"))

    @property
    def model(self) -> Model:
        raise NotImplementedError(_UNWIRED.format(member="model"))

    @property
    def journal(self) -> Journal:
        raise NotImplementedError(_UNWIRED.format(member="journal"))

    @property
    def graph(self) -> GraphStore:
        raise NotImplementedError(_UNWIRED.format(member="graph"))

    @property
    def latent(self) -> LatentStore:
        raise NotImplementedError(_UNWIRED.format(member="latent"))

    @property
    def clock(self) -> Callable[[], datetime]:
        return self.now_fn

    def emit(self, event: Event) -> None:
        raise NotImplementedError(_UNWIRED.format(member="emit"))

    async def dispatch(self, capability: str, args: Mapping[str, Any]) -> Any:
        raise NotImplementedError(_UNWIRED.format(member="dispatch"))

    async def read_human_input(self, step_index: int) -> Artifact | None:
        raise NotImplementedError(_UNWIRED.format(member="read_human_input"))

    async def last_output(self, stage_name: str) -> Artifact | None:
        return self.last_output_map.get(stage_name)

    async def recall(
        self,
        query: RecallQuery,
        *,
        policy: MemoryPolicy | None = None,
    ) -> InjectedMemory:
        raise NotImplementedError(_UNWIRED.format(member="recall"))

    async def assemble_context(
        self,
        request: ContextRequest,
        *,
        policy: ContextPolicy | None = None,
    ) -> AssembledCallContext:
        raise NotImplementedError(_UNWIRED.format(member="assemble_context"))

    def bind_context_policy(self, policy: ContextPolicy | None) -> None:
        raise NotImplementedError(_UNWIRED.format(member="bind_context_policy"))


__all__ = ["StageContextFake"]
