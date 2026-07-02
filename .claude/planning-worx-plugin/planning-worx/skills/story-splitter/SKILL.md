---
name: story-splitter
description: Patterns for splitting a too-big ticket into small, vertical, independently valuable slices. Use when a ticket fails the atomicity-gate.
---

# story-splitter

When a ticket is too big or fails the atomicity gate, split it into smaller tickets
that each still deliver observable value (vertical slices), then keep only the ones
worth doing now.

## The meta-pattern (use this first)
Find the **core complexity** (the one source of variation that makes it big), then
**reduce all variations to one** — implement a single complete slice through it, and
make each remaining variation its own ticket.

## SPIDR (Mike Cohn)
- **Spike** — if you can't split because you don't understand it, make a time-boxed
  research ticket (mark it as a `poc`). Last resort.
- **Path** — split by alternate paths through the flow (card vs. wallet payment).
- **Interface** — split by interface/device, or simple UI first then richer.
- **Data** — support a subset of data first (one format/locale), add the rest later.
- **Rules** — relax a business rule for v1, add the full rule as a later ticket.

## Lawrence patterns (when SPIDR doesn't fit)
Workflow steps · Operations (CRUD) · Business-rule variations · Data variations ·
Data-entry methods · Major effort · Simple/complex · Defer performance · Break out a spike.

## After splitting
- Each new ticket must pass the `atomicity-gate`.
- Prefer the split that lets you **throw away** low-value slices and yields roughly
  **equal-sized** pieces.
- Keep traceability: new tickets keep the same `parent` epic. Move acceptance criteria
  to the slice that actually delivers them.
- If you split a ticket already referenced elsewhere, update `depends_on`/`blocks`.
