"""wombat composition root — assemble ONE cog-worx Engine with wombat's config (TK-1).

Pure wiring (DEC-12, never forks the Engine): it passes the four required substrate seams from
an injected ``SubstrateBundle`` (TK-14) plus wombat's six optional seams to ``Engine(...)``. It
does NOT call the model — the DeepSeek profile is registered as a ``ModelSpec`` DESCRIPTOR; the
client is built per-drive at run-start, so composition stays model-silent.
"""

from __future__ import annotations

import threading

from cogworx.capability.registry import Registry
from cogworx.context.personality import PersonalityProfile
from cogworx.context.rules import RuleSet
from cogworx.cost.budget import BudgetPolicy
from cogworx.model.base import ModelCapabilities
from cogworx.model.providers.config import ProviderConfig
from cogworx.model.registry import ModelRegistry, ModelSpec
from cogworx.recall.stack import RecallStack
from cogworx.runtime.engine import Engine

from .config import WombatConfig, load_config
from .substrate import SubstrateBundle, build_substrate

MODEL_PROFILE = "deepseek"

_lock = threading.Lock()
_engine: Engine | None = None


def _deepseek_registry(config: WombatConfig) -> ModelRegistry:
    """Register the DeepSeek profile as a ModelSpec descriptor (no client built here)."""
    registry = ModelRegistry()
    spec = ModelSpec(
        provider="openai_compat",  # DeepSeek speaks the OpenAI-compatible protocol
        config=ProviderConfig(
            api_key=config.deepseek_api_key,
            base_url=config.deepseek_base_url,
            model_pro="deepseek-chat",
            model_flash="deepseek-chat",
        ),
        # capabilities is REQUIRED for a non-None base_url (DeepSeek endpoint).
        capabilities=ModelCapabilities(structured_output=True, streaming=True, tools=True),
    )
    registry.register_spec(MODEL_PROFILE, spec)
    return registry


def _personality() -> PersonalityProfile:
    return PersonalityProfile(
        name="wombat", role="a quiet personal assistant", tone="concise and calm"
    )


def build_engine(
    bundle: SubstrateBundle | None = None, *, config: WombatConfig | None = None
) -> Engine:
    """Assemble (once) and return the wombat Engine. Idempotent: a second call returns the same
    instance — never a silent duplicate (AC3). ``bundle``/``config`` default to the cold-boot
    substrate and env config respectively."""
    global _engine
    with _lock:
        if _engine is None:
            cfg = config if config is not None else load_config()
            sub = bundle if bundle is not None else build_substrate()
            _engine = Engine(
                models=_deepseek_registry(cfg),
                journal=sub.journal,
                graph_store=sub.graph_store,
                latent=sub.latent,
                pathways=sub.pathways,
                model_profile=MODEL_PROFILE,
                budget_policy=BudgetPolicy(),
                registry=Registry(),
                recall_stack=RecallStack(channels=[]),
                personality=_personality(),
                rules=RuleSet(),
            )
        return _engine


def reset_engine() -> None:
    """Drop the process singleton so the next build_engine() reassembles. For tests / re-init."""
    global _engine
    with _lock:
        _engine = None
