# cog-worx ships no StageContext test double — every stage-building consumer hand-rolls a full-Protocol fake

- **Status:** OPEN
- **Reported:** 2026-07-02 by wombat
- **Severity:** low
- **cog-worx commit:** 73bbf2e
- **Area (guess):** loop / testing / packaging

## What wombat was doing
Unit-testing custom `Stage` implementations (DrainQueueStage, GateStage, ReviewOrSpeakStage, ComposeStage, ComposeDispatchRouter). Each `Stage.run(self, ctx: StageContext)` takes a `StageContext`, which is a ~15-member Protocol (`run_id, session_id, budget, model, journal, graph, latent, clock, emit, dispatch, read_human_input, last_output, recall, assemble_context, bind_context_policy`). To unit-test a stage in isolation (no real Engine, no substrate) we need a `StageContext` to pass in.

`cogworx.testing` ships doubles for the substrate seams — `InMemoryJournal`/`InMemoryGraphStore`/`InMemoryLatentStore` (doubles.py), a `FakeModel` (fake_model.py), fake oracle/mcp/recall — but **no `StageContext` double**. So every consumer that writes a custom stage has to hand-roll one.

## Repro
```python
from cogworx.loop.stage import StageContext  # a runtime_checkable Protocol, ~15 members
# There is no cogworx.testing double implementing it:
from cogworx.testing import doubles          # has journal/graph/latent doubles + FakeModel...
# ...but nothing that satisfies StageContext, so you cannot do:
#   ctx = doubles.InMemoryStageContext(clock=..., last_output={...})
# You must implement all ~15 Protocol members yourself.
```

## Expected
A configurable `StageContext` test double (or a builder) in `cogworx.testing` — most members raising `NotImplementedError` by default (so a stage touching an unexpected ctx member fails loud), with the commonly-needed ones injectable: `clock`, `last_output` (a name→Artifact map), `model` (accepting the existing `FakeModel`), and `emit`. This is the single most reused test fixture for anyone building stages on the Spine.

## Actual
None exists. We built `tests/support/stage_context_fake.py` — a full-Protocol `StageContextFake` that raises `NotImplementedError` on every member except an injectable `clock`, `last_output`, and (later) `model`. It works well and doubles as a ctx-surface guard (a stage touching anything unexpected blows up loudly), but every cog-worx consumer will re-invent this same class.

## Hypothesis (optional)
Not a bug — a missing testing-ergonomics capability. `FakeModel` already exists in `cogworx.testing.fake_model`, which suggests the testing surface is meant to grow; a `StageContext` double is the natural next member. (Minor related note: because it wasn't obvious `FakeModel` shipped, we also hand-rolled a small fake model in one test before finding it — a `cogworx.testing` index/README of available doubles would help discovery.)

## Workaround in place? (optional)
Yes — `tests/support/stage_context_fake.py` in the wombat repo, reused across all stage tests. No urgency; purely ergonomic. If cog-worx ships an official one with a compatible shape, wombat would adopt it and delete the local fake.

---
--- cog-worx fills below ---

## Diagnosis

## Fix
- **Commit(s):**
- **Files:**

## Verification

## ⚠️ Action required in wombat (if any)
