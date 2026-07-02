---
description: Ingest planning/vision.md into the master contract — set vision, non-goals, constraints, and the top-level feature list.
allowed-tools: Read, Edit, Bash
---

# /plan-vision

Turn the human vision document into the structured top of the contract.

1. Read `planning/vision.md` in full, plus the current `planning/contract.yaml`.
2. Populate `meta`: set `project_id` (lowercase-kebab) and `title`.
3. Populate `vision`: `problem`, `outcome`, `measurable_goals[]`, `target_users[]`.
   Be concrete — copy the user's specifics; do not smooth them into vagueness.
4. Capture guardrails:
   - `non_goals[]` (ids `NG-1`…) — the explicit "not building this" list. If the
     vision doc names none, ASK the user for 2–3; this is the main over-engineering guard.
   - `constraints[]` (ids `CST-1`…) — hard limits.
5. Create the **feature list** as top-level nodes: one `feature` node per feature
   (ids `FEAT-1`…, `kind: feature`, `parent: null`, a one-line `intent`, `status: todo`,
   a `priority` P1/P2/P3). Do NOT create epics or tickets yet — that is `/plan-design`
   and `/plan-tickets`.
6. For anything ambiguous or unstated that matters, add a governance
   `open_question` (ids `Q-1`…, `type: open_question`, `status: open`, `scope`).
   Don't guess — record the question.
7. Set `meta.stage: vision`, `meta.updated_at` (today), append a `changelog` entry.
8. Run the gate: `node "${CLAUDE_PLUGIN_ROOT}/scripts/gate_check.js" vision`.
   If it fails, fix the gaps before reporting done. Then tell the user the open
   questions to resolve next with `/plan-clarify`.
