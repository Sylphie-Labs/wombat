---
description: Adversarial cross-artifact audit — run the red-team agent to find holes in the whole plan before building.
allowed-tools: Read, Edit, Bash, Task
---

# /plan-analyze

The hole-finding gate. Nothing should reach implementation without surviving this.

1. Read `planning/contract.yaml`.
2. **Delegate to the `plan-reviewer` (red-team) subagent** with the WHOLE contract.
   Ask it to adversarially hunt for, and report ranked CRITICAL → HIGH → MEDIUM → LOW:
   - requirements with no ticket (coverage gaps) and tickets with no requirement (invented scope),
   - tickets with missing, vague, or untestable acceptance criteria,
   - unstated assumptions, missing edge cases, and unhandled error paths,
   - contradictions between decisions, constraints, and the constitution,
   - silent scope creep / over-engineering, and under-specified risky areas,
   - dependency cycles, ordering problems, or orphaned work.
3. For each CRITICAL/HIGH finding, write a governance item:
   - a gap/ambiguity → `open_question`,
   - a credible failure mode → `risk` (with probability/impact/mitigation),
   - a present defect in the plan → `issue`.
   Link each to the node it concerns via `scope`.
4. Print the full ranked findings report to the user. Do not silently "fix" things —
   surface them so the user decides.
5. Update `meta.stage: analyze`, `meta.updated_at`, append a `changelog` entry.
6. Run the gate: `node "${CLAUDE_PLUGIN_ROOT}/scripts/gate_check.js" analyze`.
   It fails while coverage is incomplete, questions are open, or issues are unresolved.
   Treat unresolved CRITICAL findings as hard blockers.

This command is also worth re-running any time the plan changes materially.
