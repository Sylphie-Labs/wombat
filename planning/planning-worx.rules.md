# planning-worx — operating rules

These rules govern any planning or implementation work in this repo. They are
intentionally short. The rich detail lives in `planning/contract.yaml`.

## The contract is the single source of truth

- `planning/contract.yaml` is authoritative. **Re-read it before acting.** It
  wins over chat history.
- On ANY change of direction: update the contract first, then re-sync. NEVER let
  the conversation become the record.
- `decisions` and `changelog` are **append-only**. To reverse a decision, add a
  new one that supersedes it. Do not edit accepted decisions or past changelog
  entries. (A hook enforces this.)

## Scope discipline

- Do the **simplest thing** that satisfies a ticket's `acceptance_criteria`.
- Respect `non_goals`, `constraints`, and `complexity_budget`. NEVER add
  unrequested scope, speculative abstraction, or features no ticket asked for.
- A node's `engineering_level` sets the bar: a `prototype` and a `production`
  ticket do not get the same rigor.

## Doing the work

- Work one ticket at a time. Pull the **lean briefing** for it (the `briefing-builder`
  skill) — don't load the whole contract into the build.
- A ticket is done only when every acceptance criterion passes a **runnable check**.
  "Looks done" is not done.
- If a ticket isn't atomic/ready (`atomicity-gate` skill), split it (`story-splitter`)
  before building.

## When in doubt, find the holes

- Before committing to a plan, run the **plan-reviewer** (red-team) agent to hunt
  for gaps, unstated assumptions, missing acceptance criteria, and silent scope
  creep. Treat unresolved CRITICAL findings as blockers.
- Every ambiguity gets a home: raise it in `governance` as an `open_question`,
  then resolve it into a `decision`, `deferral`, or `non_goal`.
