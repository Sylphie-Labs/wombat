---
name: atomicity-gate
description: The readiness predicate for tickets — is a ticket atomic, well-formed, and ready for one build pass? Use when writing, reviewing, or picking up a ticket.
---

# atomicity-gate

A ticket is **ready** only if ALL of these hold. If any fail, it is not atomic —
split it with the `story-splitter` skill before building.

1. **Who/what/why present** — `intent` names a user/role, the change, and the value.
2. **Testable acceptance criteria** — ≥1 `acceptance_criteria` as Given/When/Then,
   each binary and observable. Reject vague terms ("fast", "better", "robust").
3. **Vertical slice** — delivers an observable change in behavior, not just one layer
   ("build the DB table" alone is horizontal — fold it into the slice that uses it).
4. **Single responsibility / one PR** — it does exactly one thing and could merge on
   its own without breaking the build.
5. **Fits one pass** — implementable and testable in a single agent invocation within
   context. Right-size, don't estimate: if it needs more than ~1–2 days, split it.
6. **No live dependencies** — `depends_on` is empty or every dependency is `done`.
7. **Bounded scope** — `files_in_scope` is set (or knowable), and `non_goals` names
   what this ticket will NOT touch. `engineering_level` and `complexity_budget` are set.

Quick verdict: if you can't write the runnable check that proves it's done, it isn't
ready. If it has more than one "and" in its outcome, it's probably two tickets.

INVEST is the vocabulary behind this: Independent, Negotiable, Valuable, Estimable,
Small, Testable — weight **Small, Testable, Independent** most for near-term work.
