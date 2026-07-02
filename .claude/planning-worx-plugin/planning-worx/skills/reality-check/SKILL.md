---
name: reality-check
description: Prove or disprove a ticket's claimed status against the actual repo — verify acceptance criteria from the code, preferring executable proof, before trusting (or correcting) the contract. Use when reconciling a plan to reality or before marking a ticket done.
---

# reality-check

The contract says what a ticket *claims*. This skill decides what's *true* by looking
at the working tree, not the status field. A status is a hypothesis; the code is the
evidence. Use it to reconcile a resumed plan, or as the done-check before flipping a
ticket to `done` so the contract never drifts.

## The bar
A ticket is `done` ONLY when its `acceptance_criteria` are demonstrably satisfied by
what's in the repo. "Files exist" or "looks scaffolded" is not done. Bias conservative:
when you can't prove a criterion, the ticket is NOT done.

## Method — for one ticket
1. **Read the claim.** Pull the ticket's `acceptance_criteria` (Given/When/Then),
   `files_in_scope`, `intent`, and any `task` children.
2. **Locate the work.** Check each `files_in_scope` path exists and actually contains
   the relevant implementation (Grep/Read). Missing or empty → not done.
3. **Test each criterion.** For every acceptance_criterion, find evidence it holds.
   **Prefer executable proof over inspection**, in this order:
   - run the project's tests (or the specific test that covers it) and read the result;
   - run the command / hit the path the criterion describes and observe the behavior;
   - failing that, trace the code path that satisfies the Given→When→Then and cite it.
   Reading code is the weakest proof — use it only when nothing runnable exists.
4. **Distinguish partial from done.** Track which criteria pass and which don't. One
   unmet criterion means the ticket is not done.

## Verdict (per ticket)
Return:
- **status**: `done` | `partial` | `not_started`
- **evidence**: how each met criterion was proven (test name, command output, file:line)
- **unmet**: the specific acceptance_criteria still failing (empty iff `done`)
- **defect?**: if the ticket was *claimed* `done` but a criterion fails, flag it — that
  is a real regression/defect, not just stale status, and should become a governance
  `issue` scoped to the ticket.

## Boundaries
- Verify against the plan's own criteria — do not invent new requirements, and do not
  silently expand scope. Gaps in the criteria themselves are a `/plan-analyze` concern.
- Reconcile against the working tree you can see. If the work supposedly lives in a
  different repo/branch, say so rather than guessing — you can only prove what's present.
- You report truth; you don't fix code here. Correcting the contract and building the
  remainder is the caller's job (e.g. `/plan-reconcile`).
