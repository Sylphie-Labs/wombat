---
description: Reconcile the contract against reality — verify each ticket's claimed status against the actual repo, fix the drift, then drive the genuinely-unfinished tickets to completion.
argument-hint: "[scope id, e.g. FEAT-2 or EP-3 — omit for the whole contract]"
allowed-tools: Read, Edit, Bash, Grep, Glob, Task
---

# /plan-reconcile $ARGUMENTS

Pick a plan up mid-flight and finish it. The contract records what each ticket
*claims* (its `status`); this command checks that claim against what's *actually in
the repo*, corrects the contract to match reality, and then continues building what
truly isn't done. Use it when resuming a plan whose work happened outside this
session (another window, another machine, a hand edit) and you don't trust the
statuses.

The contract is the source of truth for the *plan*; the working tree is the source
of truth for *what's built*. This command makes the two agree, then moves forward.

## 1. Load both sides
- Read `planning/contract.yaml` (and `planning/contract.schema.json` for the rules).
- Run `node "${CLAUDE_PLUGIN_ROOT}/scripts/state_digest.js" --status` for the current
  *claimed* state.
- Determine scope: if `$ARGUMENTS` names a node (`FEAT-`/`EP-`/`TK-`), reconcile that
  subtree; otherwise reconcile every non-`canceled` ticket in the contract.

## 2. Verify each ticket against reality
For every ticket in scope, apply the **reality-check** skill: prove or disprove its
claimed status from the actual codebase — check `files_in_scope` exist and contain the
work, evaluate each `acceptance_criterion` against the code, and **prefer executable
proof** (run the test, run the command) over reading. Bias conservative: only call a
ticket `done` with concrete evidence; when unsure, call it `partial` and name the gap.

Classify each into one of:
- **CONFIRMED** — claim matches reality (no change).
- **AHEAD** — claimed `todo`/`in_progress`/`backlog`, but actually satisfied in code.
- **BEHIND** — claimed `done`, but acceptance criteria are not actually met.
- **PARTIAL** — some criteria met, some not (with the unmet ones listed).

## 3. Report the drift BEFORE changing anything
Print a table: `ticket | claimed | actual | evidence | unmet criteria`. This is the
whole point of the command — surface what's really there. Do not silently rewrite.

## 4. Correct the contract to match reality
Edit node `status` to the proven value (`AHEAD` → `done`; `BEHIND`/`PARTIAL` →
`in_progress` or `todo` with the unmet criteria captured). Node `status` is mutable —
edit it freely. But:
- History is append-only: never edit existing `decisions`/`changelog`; append a new
  `changelog` entry summarizing the reconciliation (counts per class).
- A `done` ticket that was actually broken is a real defect — record it as a
  governance `issue` (with `scope` = the ticket id) so it isn't lost.
Then run `node "${CLAUDE_PLUGIN_ROOT}/scripts/validate.js"` and fix any errors.

## 5. Drive the rest to completion
Recompute the work that's genuinely left (the digest's *next ready ticket* now
reflects reality). Then, in dependency + priority order, for each unfinished ticket
hand off to `/plan-ticket <id>` to plan and build it. Build one ticket at a time,
re-checking it with the **reality-check** skill when it's done before marking it so —
the same proof bar, so the contract never drifts again. Stop and ask if a ticket is
blocked, ambiguous, or exceeds its `complexity_budget`.

## 6. Close out
Update `meta.updated_at`, append the changelog entry, and report: how many tickets
were confirmed / corrected, what was actually built to finish, and what (if anything)
remains blocked or open.

Scope note: this reconciles ONE contract against ONE working tree. To finish a plan
that lives in another repo, bring its `planning/contract.yaml` there (and run
`planning-worx init`) first — planning-worx is single-contract by design.
