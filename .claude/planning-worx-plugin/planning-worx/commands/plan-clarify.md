---
description: Resolve open questions and ambiguity before design — turn each into a decision, deferral, or non-goal.
allowed-tools: Read, Edit, Bash
---

# /plan-clarify

Drive ambiguity out of the plan. Every open question must get a home.

1. Read `planning/contract.yaml`. List every governance item with
   `type: open_question` and `status: open`.
2. Also re-read the `vision`, `features`, `non_goals`, and `constraints` and
   actively hunt for *unstated* ambiguity (the `governance-lifecycle` skill lists
   what to look for). Add any new `open_question`s you find.
3. For each open question, work with the user to resolve it into exactly one of:
   - a **decision** (`type: decision`, `status: accepted`, with `context` / `decision` / `consequences`),
   - a **deferral** (`type: deferral`, with a REQUIRED `revisit_trigger` — the condition that brings it back),
   - or a **non_goal** (`type: non_goal`, with `rationale`).
   Set the original question's `status: resolved` and `resolution` to the new item's id.
   Ask the user rather than guessing on anything material.
4. Update `meta.updated_at`, append a `changelog` entry. Set `meta.stage: clarify`.
5. Run the gate: `node "${CLAUDE_PLUGIN_ROOT}/scripts/gate_check.js" clarify`.
   It fails while any `open_question` is still `open`. Don't report done until it passes.

Decisions are append-only — if you change your mind later, add a new decision
that supersedes the old one; never edit an accepted decision.
