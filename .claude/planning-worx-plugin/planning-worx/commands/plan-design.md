---
description: Decide the tech stack and architecture (as ADRs) and break features into epics. Delegates to the plan-architect agent.
allowed-tools: Read, Edit, Bash, Task
---

# /plan-design

Turn clarified requirements into a technical shape — without leaking implementation
bias into the planner's context.

1. Read `planning/contract.yaml`. Confirm `/plan-clarify` is done (no open questions).
2. **Delegate to the `plan-architect` subagent** (fresh context). Give it the
   vision, constitution, features, constraints, and non-goals. Ask it to return:
   - a proposed `tech_stack[]` (each with a rationale),
   - the key architectural **decisions** as ADRs (`type: decision`, `status: proposed`),
   - a breakdown of each `feature` into `epic` nodes (ids `EP-1`…, `parent: <FEAT>`).
3. Review its proposal WITH THE USER. Accept decisions by setting `status: accepted`.
   Respect the constitution and constraints — reject anything that violates them.
4. Write `tech_stack[]`, the `decisions`, and the `epic` nodes into the contract.
   Link tech entries to their `decision_ref`.
5. Update `meta.stage: design`, `meta.updated_at`, append a `changelog` entry.
6. Run the gate: `node "${CLAUDE_PLUGIN_ROOT}/scripts/gate_check.js" design`.
   Fix gaps (empty stack, no ADRs, a feature with no epics) before reporting done.

Keep the design at the level of decisions and structure — not code. Implementation
details belong to the tickets, not here (over-specifying here is its own form of
over-engineering).
