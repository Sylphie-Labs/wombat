# Finding stub — TK-73 / RISK-6: conflict-with-alternatives slot quality

**Spike:** Can a deterministic, model-free earliest-gap algorithm over a free/busy
projection propose alternative slots a human rates as reasonable?
**Status:** built + running; **HUMAN-GATED** on the ">=80% rated reasonable" bar.
**This page is the deliverable** (TK-73 AC2), not the green test run.

## What was built (throwaway spike)

- `src/wombat/calendar/models.py` — minimal frozen types (`CalendarEventItem`,
  `WorkingHours`, `Conflict`, `AlternativeSlot`, ...). Times are minutes since
  local midnight for a single day; no timezone/I/O (NG-4).
- `src/wombat/calendar/slots.py` — pure functions: `detect_conflicts`,
  `project_busy`, `free_intervals`, `propose_alternatives` (naive earliest-gap).
- `src/wombat/calendar/report.py` — renders the rating sheet below.
- `tests/calendar/fixtures/conflicts.yaml` — 6 hand-authored conflict scenarios.
- `tests/calendar/test_slots.py` — self-eval (see "Automated result").

## Automated result (the half a machine can judge)

For all 6 scenarios the algorithm proposes **>= 3 distinct, valid candidate slots**
per conflict, where *valid* = inside working hours (09:00-18:00), correct duration,
and **non-overlapping with any busy block** and with each other. `pytest` green,
`ruff` clean, `mypy --strict` clean.

What this does NOT prove: whether a human finds the *times* sensible. That is the
gate below.

## Rate these slots (Jim) — mark [x] reasonable, leave [ ] unhelpful

Regenerate anytime:
`.venv/Scripts/python.exe -m wombat.calendar.report tests/calendar/fixtures/conflicts.yaml`

### 1. simple double-booking — move '1:1 with Sam' (10:00-11:00) vs 'Design review'
- [ ] rank 0: 09:00-10:00
- [ ] rank 1: 11:00-12:00
- [ ] rank 2: 12:00-13:00
- [ ] rank 3: 13:00-14:00
- [ ] rank 4: 14:00-15:00

### 2. packed morning, open afternoon — move 'Vendor call' (10:00-10:30) vs 'Roadmap sync'
- [ ] rank 0: 10:30-11:00
- [ ] rank 1: 11:00-11:30
- [ ] rank 2: 11:30-12:00
- [ ] rank 3: 12:00-12:30
- [ ] rank 4: 12:30-13:00

### 3. lunch-spanning clash — move 'Recruiting debrief' (12:30-13:15) vs 'Lunch (held)'
- [ ] rank 0: 10:00-10:45
- [ ] rank 1: 10:45-11:30
- [ ] rank 2: 13:00-13:45
- [ ] rank 3: 13:45-14:30
- [ ] rank 4: 14:30-15:15

### 4. back-to-back wall with one gap — move 'Overlapping ask' (13:30-14:30) vs 'Block A'
- [ ] rank 0: 09:00-10:00
- [ ] rank 1: 10:00-11:00
- [ ] rank 2: 11:00-12:00
- [ ] rank 3: 12:00-13:00
- [ ] rank 4: 15:00-16:00

### 5. three-way overlap — move 'Quick sync B' (11:15-11:45) vs 'Quick sync A'
- [ ] rank 0: 09:00-09:30
- [ ] rank 1: 09:30-10:00
- [ ] rank 2: 10:00-10:30
- [ ] rank 3: 10:30-11:00
- [ ] rank 4: 12:00-12:30

### 6. long meeting bumped by short — move 'Urgent escalation' (15:00-15:30) vs 'Strategy block'
- [ ] rank 0: 09:00-09:30
- [ ] rank 1: 09:30-10:00
- [ ] rank 2: 10:00-10:30
- [ ] rank 3: 10:30-11:00
- [ ] rank 4: 11:00-11:30

## Pre-rating observations (for the author of TK-74, not a substitute for ratings)

1. **Pure earliest-first can feel "too early."** Scenarios 4-6 lead with 09:00,
   pushing a meeting hours before its original time even when a slot adjacent to
   the clash exists. If you down-rate these, the cheap fix for TK-74 is to rank by
   *proximity to the original start* rather than absolute earliest. Decide this
   before hardening.
2. **Distinctness was a deliberate choice.** An earlier version slid by 15-min
   granularity and emitted near-duplicates (10:00, 10:15, 10:30...). The test
   `test_slots_never_overlap_each_other` now forbids that; candidates step by the
   slot's own duration so each option is genuinely different.
3. **Same-day only.** The spike never spills into the next day; every fixture day
   has enough room. TK-74's AC mentions "next working day if none" — out of scope
   here and untested.

## Exit decision (Jim to complete)

- Reasonable slots: ____ / (total rated)   →   ____%
- [ ] >= 80%: earliest-gap clears TK-74 to proceed as designed.
- [ ] < 80%: TK-74 needs the revision noted above (rank by proximity to original
      start) — re-run this spike before hardening.
