"""TK-297/TK-299 — the ``build_dream_pathway`` graph-shape proof (EP-13/EP-37, DEC-65g/DEC-66).

Not pg-gated (mirrors ``test_dream_persona_stage.py``'s own AC4 engine-drive idiom): a REAL
``Engine`` drives ``wombat.dream`` end-to-end over ``_PassthroughStage`` doubles for every stage
EXCEPT ``DreamFactsStage`` and ``DreamDeriveStage`` (real ones, over in-memory/monkeypatched
collaborators, the latter given a ``None`` ``ExternalItemStore`` so it derives zero facts and
still transitions cleanly) — proving the landed graph order is exactly ``dream_consolidate ->
dream_outcome -> dream_tune -> dream_persona -> dream_facts -> dream_derive -> dream_behavior_log
-> dream_window -> dream_pattern -> dream_run`` (AC4/AC5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import StageResult, Transition
from cogworx.loop.stage import StageContext
from cogworx.loop.state import RunStatus
from cogworx.model.registry import ModelRegistry
from cogworx.runtime.engine import Engine

from tests.support.stage_context_fake import FakeModel
from wombat.behavior.stages.dream_derive import DreamDeriveStage
from wombat.behavior.stages.dream_facts import DreamFactsStage
from wombat.chat_turns import ChatTurnStore
from wombat.pathways.dream_pathway import (
    DREAM_PATHWAY_ID,
    build_dream_pathway,
    dream_trigger_artifact,
)
from wombat.substrate import cold_boot_bundle
from wombat.user_facts import UserFactsStore

_NOW = datetime(2026, 7, 30, 3, 0, 0, tzinfo=UTC)
_UNREACHABLE_DSN = "postgresql://nonexistent-host-should-never-be-dialed:1/db"


@dataclass
class _PassthroughStage:
    """A trivial always-transitions-onward double (mirrors ``test_dream_persona_stage.py``'s own
    passthrough-stage convention) — this module's ONE AC is the landed graph's shape, not any one
    stage's own behavior."""

    name: str
    to: str
    transitions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.transitions = (self.to,)

    async def run(self, ctx: StageContext) -> StageResult:
        return Transition(
            to=self.to,
            output=Artifact(
                kind="test.passthrough",
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={},
            ),
        )


async def test_ac4_the_landed_graph_walks_all_ten_stages_in_order_to_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A real DreamFactsStage over a zero-turns ChatTurnStore (lazy — never actually connects) so
    # it transitions on with NO model call, exactly like every other off-path stage here.
    chat_turns = ChatTurnStore(_UNREACHABLE_DSN)

    def _turns_since(self: ChatTurnStore, cutoff: datetime) -> list[dict[str, object]]:
        return []

    monkeypatch.setattr(ChatTurnStore, "turns_since", _turns_since)

    facts_stage = DreamFactsStage(
        model=FakeModel(raises=AssertionError("zero turns must never call the mouth")),
        chat_turns=chat_turns,
        user_facts=UserFactsStore(_UNREACHABLE_DSN),
    )

    # A real DreamDeriveStage over a None ExternalItemStore (AC3's degrade shape) — zero rows in,
    # zero facts derived, zero UserFactsStore calls made, still transitions on with NO model call
    # (this stage never calls one).
    derive_stage = DreamDeriveStage(
        external_items=None,
        user_facts=UserFactsStore(_UNREACHABLE_DSN),
    )

    dream_graph = build_dream_pathway(
        _PassthroughStage(name="dream_consolidate", to="dream_outcome"),
        _PassthroughStage(name="dream_outcome", to="dream_tune"),
        _PassthroughStage(name="dream_tune", to="dream_persona"),
        _PassthroughStage(name="dream_persona", to="dream_facts"),
        facts_stage,
        derive_stage,
        _PassthroughStage(name="dream_behavior_log", to="dream_window"),
        _PassthroughStage(name="dream_window", to="dream_pattern"),
        _PassthroughStage(name="dream_pattern", to="dream_run"),
    )

    bundle = cold_boot_bundle()
    bundle.pathways.register(DREAM_PATHWAY_ID, dream_graph)

    models = ModelRegistry()
    models.register_factory(
        "deepseek",
        lambda guard: FakeModel(raises=AssertionError("passthrough stages never call the mouth")),
    )
    engine = Engine(
        models=models,
        journal=bundle.journal,
        graph_store=bundle.graph_store,
        latent=bundle.latent,
        pathways=bundle.pathways,
        model_profile="deepseek",
        clock=lambda: _NOW,
    )

    final = await engine.run(
        run_id="run-tk297-graph-shape",
        session_id="run-tk297-graph-shape",
        pathway_id=DREAM_PATHWAY_ID,
        initial=dream_trigger_artifact(_NOW),
    )

    assert final.status is RunStatus.COMPLETED
    assert [step.stage_name for step in final.steps] == [
        "dream_consolidate",
        "dream_outcome",
        "dream_tune",
        "dream_persona",
        "dream_facts",
        "dream_derive",
        "dream_behavior_log",
        "dream_window",
        "dream_pattern",
        "dream_run",
    ]
