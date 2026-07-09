"""wombat.safety.tier_policy — the ONE sanctioned way a Stage gains the cog-worx external
capability tier (TK-151, EP-28).

DEC-22 already GRANTED authorization for wombat to admit stages to the external tier; this module
BINDS that grant, it does not re-litigate it. cog-worx owns the tier/taint machinery end to end
(``cogworx.capability.policy.StageToolPolicy`` / ``ToolGate``) — this module adds no new security
logic of its own, it supplies exactly one frozen policy constant plus one binding helper.

Structural fail-closed (verified against installed cog-worx, 2026-07-09): the engine re-binds
``ctx.bind_tool_policy(getattr(stage, "tool_policy", None))`` before EVERY stage
(``runtime/engine.py``) — ``None`` resets the gate to ``DEFAULT_TOOL_POLICY``
(``allowed_tiers={"read", "write"}``, no external). There is no composition-root registry and no
separate "external-tier flag" to forget: a stage that never calls :func:`bind_external_tier`
simply has no ``tool_policy`` attribute, so the engine binds the default and
``ToolGate.check_dispatch`` raises ``TierViolation`` on any external-tier capability it attempts.
Binding is per-stage and never leaks — the engine rebinds fresh before every stage, so admitting
one stage to the external tier has no effect on any other stage in the drive.

DEC-26 INVARIANT: ``taint_drops_external`` stays ``True`` on ``EXTERNAL_DISPATCH_POLICY`` — no
code path may EVER construct or bind a ``StageToolPolicy(taint_drops_external=False)``. This is
what keeps the lethal-trifecta break (S10, TK-148) intact even for a stage admitted to the
external tier: once a drive taints, ``ToolGate._effective_tiers()`` drops ``"external"``
regardless of ``allowed_tiers`` (``tests/safety/test_tier_policy.py`` asserts the invariant
directly and proves this is the ONLY external-admitting ``StageToolPolicy`` construction site
under ``src/wombat``).

Non-goals (out of scope for this ticket): no dispatch stage implementation (TK-78/TK-135), no
bootstrap wiring (TK-177 — no dispatch stage exists at boot yet), no change to cog-worx's
``DEFAULT_TOOL_POLICY`` (the engine-wide default is untouched by this module), no cog-worx edit
(DEC-12).
"""

from __future__ import annotations

from typing import Any

from cogworx.capability.policy import StageToolPolicy

EXTERNAL_DISPATCH_POLICY: StageToolPolicy = StageToolPolicy(
    allowed_tiers=frozenset({"read", "write", "external"}),
    taint_drops_external=True,
)
"""The ONE frozen policy admitting a Stage to the external capability tier (DEC-22 grant, bound
here — not re-litigated). ``taint_drops_external=True`` is the DEC-26 INVARIANT: it must never be
overridden to ``False`` by any code path."""


def bind_external_tier(stage: Any) -> None:
    """Bind ``EXTERNAL_DISPATCH_POLICY`` onto ``stage.tool_policy`` (Stage-scoped, DEC-22).

    This is the ONLY sanctioned call site that admits a Stage to the external tier. The engine
    reads ``getattr(stage, "tool_policy", None)`` and rebinds the gate fresh before every stage
    (``runtime/engine.py``), so this grant is scoped to exactly the stage it is called on and
    never leaks to any other stage in the drive. ``stage`` is typed ``Any`` deliberately: no
    dispatch stage exists yet (TK-78/TK-135 build them later) and cog-worx declares no formal
    ``Stage`` protocol beyond duck-typed ``name``/``transitions``/``run`` — this helper only ever
    needs to set one attribute.
    """
    stage.tool_policy = EXTERNAL_DISPATCH_POLICY


__all__ = ["EXTERNAL_DISPATCH_POLICY", "bind_external_tier"]
