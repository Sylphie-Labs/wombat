# StageGraph route-guard is runtime-only — an undeclared Wait-to-self edge passes registration and the whole test suite, crashing only when that branch first executes under a real Engine

- **Status:** OPEN
- **Reported:** 2026-07-02 by wombat
- **Severity:** medium
- **cog-worx commit:** 73bbf2e
- **Area (guess):** loop / graph

## What wombat was doing
Building a `DrainQueueStage` (a cog-worx `Stage`) whose empty-queue branch returns `Wait(to="drain_queue")` — i.e. a re-park to ITSELF (the documented idle idiom: the Sweeper re-drives from entry). Its `transitions` tuple was declared `("gate",)` — the items-present destination — but accidentally OMITTED the `"drain_queue"` self-edge. The stage was registered into a `StageGraph` via a builder and unit-tested extensively (its own tests drove it through a hand-rolled `StageContext` fake, not a real Engine). Everything was green — 191 tests, ruff, mypy --strict — and the stage registered without complaint. The defect only surfaced the FIRST time a real `Engine.run()` drove the graph into the empty-queue branch.

## Repro
```python
# A stage whose run() can return a destination NOT in its declared `transitions`.
from cogworx.loop.result import Wait  # + your StageResult imports
# stage.transitions = ("gate",)          # declares ONE edge
# stage.run(ctx) returns Wait(to="drain_queue")  # ... but re-parks to ITSELF (undeclared)

# 1. Build a StageGraph containing this stage and register it in a PathwayRegistry.
#    -> succeeds. No error at construction or registration.
# 2. Drive it with a real Engine into the branch that returns the undeclared Wait:
#    await engine.run(run_id=..., pathway_id=..., initial=<artifact>)
#    -> raises StageGraphError only now (first execution of that branch).
```
Needs no external service to reproduce the guard behavior (a minimal 1-stage graph + the in-memory `cold_boot` doubles suffice).

## Expected
The mismatch — a stage can return a `Wait`/`Transition` to a destination outside its declared `transitions`/`edges_from` — is caught as EARLY as possible: ideally at `StageGraph` construction or `PathwayRegistry.register` time, or via an offered graph-lint / validation helper. Because a self-park is a *static structural intent* of the stage, a consumer shouldn't have to reach it at runtime (which stage-level fakes never do) to learn the edge is undeclared.

## Actual
The declared-route guard is RUNTIME-ONLY: it checks `graph.edges_from(stage)` when the branch actually executes and commits a step. So an undeclared `Wait`-to-self (or any conditionally-returned destination) is invisible until that specific branch runs under a real Engine. Verbatim:
```
cogworx.loop.graph.StageGraphError: stage 'drain_queue' returned a result routing to 'drain_queue', which is not in its declared edge set ['gate']
```

## Hypothesis (optional)
`Transition.to` / `Wait.to` are constructed inside `run()`, so FULL static verification is impossible in general. But cog-worx could still surface this class of bug far earlier, e.g.: (a) a `StageGraph.validate()` / lint that flags stages whose result-destinations can't be shown ⊆ declared edges, or warns when `transitions` lacks a self-edge for a stage that imports/returns `Wait`; (b) a `cogworx.testing` helper that drives each stage's result branches through the guard; or (c) at minimum, docs on `Stage.transitions` stating explicitly that it MUST include Wait-to-self re-park edges. The core observation: the guard being runtime-only is exactly why a real shipped defect survived a fully-green suite — the guard's value is undercut by how late it fires.

## Workaround in place? (optional)
Yes. wombat now (1) declares every stage's `transitions` to include its Wait-to-self edge, and (2) adopted a convention that any stage with a Wait/self-park branch MUST have at least one real-Engine/StageGraph route-check test (stage-level fakes are insufficient). This caught and fixed the bug, but the convention is a wombat-side guard against a framework sharp edge.

---
--- cog-worx fills below ---

## Diagnosis

## Fix
- **Commit(s):**
- **Files:**

## Verification

## ⚠️ Action required in wombat (if any)
