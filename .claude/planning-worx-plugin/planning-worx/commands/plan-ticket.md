---
description: Plan one atomic ticket — verify it's ready, expand it into task steps, and produce the lean briefing to build it.
argument-hint: <ticket-id, e.g. TK-12>
allowed-tools: Read, Edit, Bash, Task
---

# /plan-ticket $1

Zoom into a single ticket and produce an executable, atomic plan — same schema,
one level down. This is the "ticket plan" that gets handed to implementation.

1. Read `planning/contract.yaml` and find node `$1`. If it isn't a `ticket`, stop
   and say so.
2. Check it against the **atomicity-gate** skill (single responsibility, vertical
   slice, testable acceptance criteria, no unmet dependencies, bounded scope). If it
   FAILS, use the **story-splitter** skill to split it into sibling tickets, write
   those, and stop — re-run `/plan-ticket` on the smaller pieces.
3. If it passes, decompose it into `task` child nodes (ids `TASK-1`…, `parent: $1`,
   `kind: task`) — the concrete, ordered steps to implement it. Decompose only as
   far as needed for one build pass per task (as-needed, not exhaustively).
4. Use the **briefing-builder** skill to assemble the LEAN briefing for `$1`: just
   this ticket + its acceptance criteria + its non_goals + relevant constraints +
   relevant decisions. Present that briefing — it's what you actually build from,
   and it deliberately excludes the rest of the contract (compliance cliff).
5. Optionally **delegate to `plan-reviewer`** to red-team this ticket's plan for
   missing edge cases before building.
6. Update `meta.updated_at` and append a `changelog` entry. (Leave `meta.stage`.)

Keep the work inside the ticket's stated scope and `complexity_budget`. If you
discover the ticket was wrong, update the contract and re-sync — don't drift.
