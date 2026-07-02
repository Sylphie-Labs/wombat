---
name: governance-lifecycle
description: How to track and resolve open questions, assumptions, risks, deferrals, and decisions so no ambiguity stays unowned. Use when recording or resolving anything non-task in the plan.
---

# governance-lifecycle

Every ambiguity gets a home and a state. The point is that nothing important is left
for the agent to silently decide. Items convert into each other — model the edges.

## The conversion edges
- **open_question → decision | deferral | non_goal.** A question must resolve into
  one of these. Set the question's `status: resolved` and `resolution: <new id>`.
- **assumption → risk → issue.** An assumption that fails validation becomes a `risk`
  (set `becomes`/`converted_from`); a risk that materializes becomes an `issue`.
- **decision → superseded decision.** Never edit an accepted decision; add a new one
  with `supersedes: <old id>` and set the old one's `status: superseded`.

## Required fields by type
- decision: `context`, `decision` ("We will…"), `consequences`. Append-only.
- deferral: `revisit_trigger` — REQUIRED. The condition/date that revisits it. This is
  what makes a deferral "later" and not a silent "never".
- risk: `probability`, `impact` (low|medium|high), `mitigation`.
- assumption: `validation_method` (how/when it'll be checked).
- Every item: an `owner` and `date_raised` (un-owned items rot).

## Ambiguity to actively hunt for (in /plan-clarify and /plan-analyze)
- Undefined terms, unspecified limits/quantities, "etc." and "and so on".
- Unstated error paths and edge cases ("what happens when this fails / is empty / conflicts?").
- Implicit assumptions about scale, environment, data shape, or third parties.
- Decisions made implicitly in passing that were never recorded.
- Anything where two reasonable people would build different things.

## Deferral vs non-goal vs constraint
later (deferral, has a trigger) · never-this-scope (non_goal, has a rationale) ·
hard limit we must obey (constraint).
