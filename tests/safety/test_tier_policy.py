"""TK-151 — external-tier admission policy acceptance criteria (EP-28, DEC-22, DEC-26).

ALL tests below drive the REAL cog-worx classes IN-PROCESS — ``Registry`` + ``ToolGate`` +
``dispatch_one`` — no mocks of the security machinery, no ``Engine``, mirroring the idiom
``tests/safety/test_taint_latch_adversarial.py`` established for TK-148. Stub stages are plain
duck-typed objects; the gate is bound the same way the engine binds it
(``gate.bind_policy(getattr(stage, "tool_policy", None))``, ``runtime/engine.py``).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from cogworx.capability.policy import DEFAULT_TOOL_POLICY, TierViolation, ToolGate
from cogworx.capability.registry import Registry, function_capability
from cogworx.capability.router import dispatch_one

from wombat.safety.taint import TRUSTED_OUTPUT_TAG, UNTRUSTED_SOURCE_TAG
from wombat.safety.tier_policy import EXTERNAL_DISPATCH_POLICY, bind_external_tier

_CAPABILITY_NAME = "gmail.drafts.create"
_DRAFT_ARGS = {"to": "jane@example.com", "body": "hi"}


class _StubStage:
    """A minimal duck-typed stage — just enough shape for ``bind_external_tier`` /
    ``getattr(stage, "tool_policy", None)`` (the engine's own binding pattern,
    ``runtime/engine.py``). No dispatch stage exists yet (TK-78/TK-135)."""

    name = "stub_stage"


async def _create_gmail_draft(to: str, body: str) -> str:
    return f"draft to {to}: {body}"


async def _read_untrusted_source(source: str) -> str:
    return f"untrusted content from {source}"


def _register_external_capability(registry: Registry, *, trusted_output: bool = True) -> None:
    tags = (TRUSTED_OUTPUT_TAG,) if trusted_output else ()
    registry.register(
        function_capability(_create_gmail_draft, name=_CAPABILITY_NAME, tier="external"),
        tags=tags,
    )


# --------------------------------------------------------------------------------------- AC1


async def test_ac1_admitted_where_bound() -> None:
    """bind_external_tier(stub_stage); bind the gate to stage.tool_policy in a fresh, untainted
    drive; dispatching the external capability succeeds — no TierViolation."""
    registry = Registry()
    _register_external_capability(registry)

    stage = _StubStage()
    bind_external_tier(stage)

    gate = ToolGate(registry)
    gate.bind_policy(getattr(stage, "tool_policy", None))

    result = await dispatch_one(gate, registry, _CAPABILITY_NAME, _DRAFT_ARGS)
    assert result == "draft to jane@example.com: hi"


# --------------------------------------------------------------------------------------- AC2


async def test_ac2_stage_scoped_never_global() -> None:
    """An UNBOUND second stage in the same setup attempting the same external capability is
    refused with TierViolation, and cog-worx's engine-wide default is untouched."""
    registry = Registry()
    _register_external_capability(registry)

    bound_stage = _StubStage()
    bind_external_tier(bound_stage)
    unbound_stage = _StubStage()  # bind_external_tier never called

    gate = ToolGate(registry)

    gate.bind_policy(getattr(bound_stage, "tool_policy", None))
    await dispatch_one(gate, registry, _CAPABILITY_NAME, _DRAFT_ARGS)

    gate.bind_policy(getattr(unbound_stage, "tool_policy", None))
    with pytest.raises(TierViolation):
        await dispatch_one(gate, registry, _CAPABILITY_NAME, _DRAFT_ARGS)

    assert DEFAULT_TOOL_POLICY.allowed_tiers == frozenset({"read", "write"})


# --------------------------------------------------------------------------------------- AC3


async def test_ac3_structural_fail_closed_without_the_helper() -> None:
    """A dispatch stage WITHOUT bind_external_tier applied (no tool_policy attribute at all)
    is refused with TierViolation — fail-closed is structural, not opt-out."""
    registry = Registry()
    _register_external_capability(registry)

    stage = _StubStage()
    assert not hasattr(stage, "tool_policy")

    gate = ToolGate(registry)
    gate.bind_policy(getattr(stage, "tool_policy", None))

    with pytest.raises(TierViolation):
        await dispatch_one(gate, registry, _CAPABILITY_NAME, _DRAFT_ARGS)


def _is_stage_tool_policy_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "StageToolPolicy"
    if isinstance(func, ast.Attribute):
        return func.attr == "StageToolPolicy"
    return False


def _admits_external_tier(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg != "allowed_tiers":
            continue
        for sub in ast.walk(kw.value):
            if isinstance(sub, ast.Constant) and sub.value == "external":
                return True
    return False


def test_ac3_the_helper_is_the_only_external_admitting_construction_site() -> None:
    """Structural proof of no self-grant: scan every ``StageToolPolicy(...)`` construction under
    ``src/wombat`` and assert the ONLY one whose ``allowed_tiers`` admits ``"external"`` lives in
    ``tier_policy.py`` (this ticket's own module)."""
    src_root = Path(__file__).resolve().parents[2] / "src" / "wombat"
    offending: list[str] = []
    for path in src_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "StageToolPolicy(" not in text:
            continue
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if not _is_stage_tool_policy_call(node):
                continue
            assert isinstance(node, ast.Call)
            if _admits_external_tier(node) and path.name != "tier_policy.py":
                offending.append(str(path))
    assert offending == []


def test_dec26_invariant_taint_drops_external_stays_true() -> None:
    """The DEC-26 INVARIANT, asserted directly: EXTERNAL_DISPATCH_POLICY never disables
    taint_drops_external, and it is exactly the {read, write, external} tier set."""
    assert EXTERNAL_DISPATCH_POLICY.taint_drops_external is True
    assert EXTERNAL_DISPATCH_POLICY.allowed_tiers == frozenset({"read", "write", "external"})


# --------------------------------------------------------------------------------------- AC4


async def test_ac4_taint_still_dominates_the_admitted_tier() -> None:
    """bind_external_tier admits the external tier; latching TaintState on the SAME gate (via a
    capability tagged untrusted-source, mirroring the TK-148 adversarial proof) then makes a
    subsequent external dispatch fail with TierViolation — taint outranks the grant."""
    registry = Registry()
    _register_external_capability(registry)
    registry.register(
        function_capability(_read_untrusted_source, name="read_untrusted", tier="read"),
        tags=(UNTRUSTED_SOURCE_TAG,),
    )

    stage = _StubStage()
    bind_external_tier(stage)

    gate = ToolGate(registry)
    gate.bind_policy(getattr(stage, "tool_policy", None))

    # Sanity: external is admitted and dispatchable before any taint.
    await dispatch_one(gate, registry, _CAPABILITY_NAME, _DRAFT_ARGS)
    assert gate.taint.tainted is False

    await dispatch_one(gate, registry, "read_untrusted", {"source": "inbox"})
    assert gate.taint.tainted is True

    with pytest.raises(TierViolation):
        await dispatch_one(gate, registry, _CAPABILITY_NAME, _DRAFT_ARGS)
