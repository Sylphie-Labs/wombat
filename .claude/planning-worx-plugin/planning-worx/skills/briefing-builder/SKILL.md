---
name: briefing-builder
description: Assemble the lean per-ticket briefing to hand to implementation — only what this one ticket needs, nothing more. Use before building a ticket.
---

# briefing-builder

Track everything in the contract; brief the builder on almost nothing. Piling the
whole plan into the build context makes the agent follow fewer rules (the compliance
cliff), not more. For a given ticket, assemble ONLY this slice:

## Include
- The ticket itself: `id`, `title`, `intent`, `acceptance_criteria` (the done-check),
  `engineering_level`, `complexity_budget`, `estimate`.
- The ticket's `non_goals` and `files_in_scope`.
- Its `task` children if `/plan-ticket` has expanded them (the ordered steps).
- ONLY the constitution rules, constraints, and decisions that actually bear on this
  ticket — not the whole lists. (e.g. the ADR that fixes the relevant library; the
  constraint about offline mode if this ticket touches the network.)
- Any `depends_on` tickets' outputs it builds on (just their ids + what they produced).

## Exclude
- Other features/epics/tickets, the full vision, unrelated decisions, the changelog,
  resolved questions, and anything this ticket explicitly will not touch.

## Output shape
A short briefing the implementer can hold in working memory:

> Build `TK-12`: <intent>. Done when: <acceptance criteria>. Stay within: <non_goals>,
> <complexity_budget>, files <files_in_scope>. Honor: <only the relevant rules/decisions>.

If assembling the briefing reveals the ticket isn't atomic or is under-specified, stop
and route back to `atomicity-gate` / `/plan-clarify` — don't paper over it in the briefing.
