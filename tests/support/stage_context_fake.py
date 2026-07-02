"""StageContextFake — the reusable StageContext test double (TK-5, Q-47).

cog-worx ships NO ``StageContext`` double (verified 2026-07-02). This fake implements the FULL
``cogworx.loop.stage.StageContext`` Protocol, raising ``NotImplementedError`` on every member
EXCEPT ``clock`` (an injected, controllable datetime source), ``last_output`` (a configurable
mapping, defaulting to empty — for TK-6/7/8/10 reuse once stages start routing on upstream
output), and ``model`` (a configurable ``FakeModel``, defaulting to unwired/raising — TK-8). A
stage under test that reaches for any other ``ctx`` member fails loudly instead of silently
degrading, catching accidental ctx-surface creep (Q-47).

``FakeModel`` (TK-8, Q-50) is a configurable fake ``cogworx.model.base.Model`` for
``ComposeStage`` tests: configure ``response`` for a canned success, ``raises`` for a
provider/connection/timeout/BudgetExceeded-shaped failure, and/or ``sleep_seconds`` to outlast a
caller's ``asyncio.wait_for`` timeout. It satisfies the ``Model`` Protocol under strict mypy
(``capabilities``, ``complete``, ``count_tokens``) and records every ``complete()`` call's
messages in ``calls`` so tests can assert on the captured prompt.

Reusable: later spine tickets (TK-6/7/8/10) import this same fake for their own Stage tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from cogworx.claims.provenance import Artifact
from cogworx.coordination.events import Event
from cogworx.cost.budget import BudgetGuard
from cogworx.model.base import (
    ChatMessage,
    Model,
    ModelCapabilities,
    ModelResponse,
    ModelTier,
    ToolSpec,
)

if TYPE_CHECKING:
    from cogworx.context.types import AssembledCallContext, ContextPolicy, ContextRequest
    from cogworx.injection.policy import InjectedMemory, MemoryPolicy
    from cogworx.recall.query import RecallQuery
    from cogworx.substrate.graph_store import GraphStore
    from cogworx.substrate.journal import Journal
    from cogworx.substrate.latent import LatentStore

_UNWIRED = (
    "StageContextFake: ctx.{member} is not wired — this stage touched ctx surface "
    "beyond clock()/last_output()/model()"
)


@dataclass
class FakeModel:
    """A configurable fake ``cogworx.model.base.Model`` (TK-8, Q-50).

    Exactly one of ``response`` / ``raises`` is the intended configuration per test:

    - ``response`` set -> ``complete()`` returns it (the success path).
    - ``raises`` set -> ``complete()`` raises it (a provider/connection/HTTP-5xx/
      BudgetExceeded-shaped failure — ``ComposeStage``'s degrade path, AC2).
    - ``sleep_seconds`` > 0 -> ``complete()`` awaits ``asyncio.sleep`` first, to outlast a
      caller's ``asyncio.wait_for`` timeout (the OTHER AC2 degrade trigger).

    Every call's ``messages`` is recorded in ``calls`` (in call order) so a test can assert on
    the exact captured prompt (e.g. that it excludes gate/queue-internal keys).
    """

    response: ModelResponse | None = None
    raises: BaseException | None = None
    sleep_seconds: float = 0.0
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)
    calls: list[Sequence[ChatMessage]] = field(default_factory=list)

    async def complete(
        self,
        *,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        tier: ModelTier = "pro",
        json_schema: Mapping[str, Any] | None = None,
    ) -> ModelResponse:
        self.calls.append(messages)
        if self.sleep_seconds:
            await asyncio.sleep(self.sleep_seconds)
        if self.raises is not None:
            raise self.raises
        if self.response is not None:
            return self.response
        raise NotImplementedError("FakeModel: neither response= nor raises= was configured")

    def count_tokens(self, text: str) -> int:
        return len(text.split())


@dataclass
class StageContextFake:
    """A ``StageContext`` double. Only ``clock``, ``last_output``, and ``model`` are implemented.

    ``now_fn`` is required (no default) — the caller injects a fixed or steppable datetime
    source. ``last_output_map`` is optional and defaults to empty (``last_output`` then returns
    ``None`` for any stage name, matching the real Protocol's documented "no output yet" case).
    ``model_fake`` is optional and defaults to ``None`` — unconfigured, ``ctx.model`` still raises
    ``NotImplementedError`` so stages that never touch the mouth (TK-6/7) are unaffected; TK-8's
    ``ComposeStage`` tests pass a configured ``FakeModel``.
    """

    now_fn: Callable[[], datetime]
    last_output_map: Mapping[str, Artifact | None] = field(default_factory=dict)
    run_id: str = "fake-run"
    session_id: str = "fake-session"
    model_fake: FakeModel | None = None

    @property
    def budget(self) -> BudgetGuard:
        raise NotImplementedError(_UNWIRED.format(member="budget"))

    @property
    def model(self) -> Model:
        if self.model_fake is None:
            raise NotImplementedError(_UNWIRED.format(member="model"))
        return self.model_fake

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


__all__ = ["FakeModel", "StageContextFake"]
