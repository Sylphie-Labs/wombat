# 07 — Behavior analysis and the psychology KB's gated reflection (TK-369, SWEEP 07)

Run date: 2026-08-05, per `protocol.md`, one sweep at a time per DEC-86.

**Headline: the constitution's bars hold, and the reflection has never spoken.**
Ten reflection candidates have been generated on ten consecutive nights and the
gate held every single one. No reflection text has ever been composed on this
host, which means the one output whose failure mode is a *constitutional
violation* has never been exercised in production. Routed as **ISS-64**.

| AC | Check | Class | Result |
|---|---|---|---|
| AC1 | windows derived deterministically, traceable, no model on the derivation | A | **PASS** on determinism; durability half blocked by ISS-63 |
| AC2 | at most ONE reflection, gate decision visible, silence as the default | A | **PASS** — ten nights, ten holds |
| AC3 | the reflection text read word by word against the constitution | A | **BLOCKED** — no reflection text has ever existed |
| AC4 | no dashboard, chart, streak, score or nagging anywhere (NG-3) | A | **PASS** |

---

## AC1 — deterministic windows, no model on the derivation — **PASS (durability blocked)**

**Determinism and the model-free bar: PASS.**
`behavior/window_detector.py:59` — `detect_productivity_windows(events:
Sequence[BehaviorEventRow]) -> list[WindowSummary]` is a pure function over
logged event rows. It takes no model, no clock, no I/O; the derivation from row
to window is the function body. No model call exists anywhere on that path,
which is the same DEC-23/NG-4 line TK-368 AC4 proved for the gate.

**The event corpus is real:** `wombat_behavior_events` holds **243 rows**
spanning **2026-07-10 → 2026-08-04**.

**One thing worth recording about that corpus:** every single row is
`outcome_ignored` — 188 `generic`, 54 `screen_activity`, 1 `reflection`, and
**zero** rows carrying any other outcome label. The outcome signal feeding the
rating tuner is entirely one-sided. That is not a defect on its own (nothing has
been surfaced *to* act on, so "ignored" is the honest label), but it compounds
ISS-63: even if ratings persisted, there is currently no outcome variety for a
tuner to learn direction from.

**Durability half: BLOCKED by ISS-63.**
`behavior/stages/write_window_summaries.py:17` writes windows via
`writer.record` under `subject=f"productivity_window:{...}"` — that is the
`ObservationWriter` over `entity_kg`, which TK-368 established is
`InMemoryEntityKG`. So productivity windows are volatile exactly as
`RatingParams` are: written nightly, read by the pattern detector in the *same*
process, gone at exit. That is why pattern detection works nightly yet nothing
accumulates. **Second independent consequence of ISS-63**, recorded there.

## AC2 — at most one reflection, and silence as the default — **PASS**

The strongest evidence in this sweep, and it required no construction.

**At most one per night is structural, not incidental.**
`pattern_detector.py:38-45` — the enqueue is keyed
`idempotency_key("wombat.reflection", <wombat-date-iso>)`, so a same-night
re-fire collapses to `EnqueueResult.ALREADY_QUEUED` and cannot create a second
row. The KB match itself is one-candidate-wins: *"The FIRST entry whose
condition matches wins — ONE candidate `pattern_id`, never more."*

**Enqueue is genuinely conditional.** `pattern_detector.py:35-36` — *"No match
(or an empty `kb`) is silent: zero enqueues, no log."* So a reflection item
existing at all means the KB said a pattern warranted a nudge that night.

**Every decision is visible in the log, and every one is a hold.** Ten
consecutive nights, one item each, gate decision recorded each time:

```
logs/runtime-20260719-193314.log:3094  17:wombat.reflection:2026-07-20  action='hold'
logs/runtime-20260720-192648.log:4161  17:wombat.reflection:2026-07-21  action='hold'
logs/runtime-20260728-213405.log:107   17:wombat.reflection:2026-07-28  action='hold'
logs/runtime-20260730-123649.log:79    17:wombat.reflection:2026-07-30  action='hold'
logs/runtime-20260730-184232.log:324   17:wombat.reflection:2026-07-31  action='hold'
logs/runtime-20260801-212610.log:87    17:wombat.reflection:2026-08-01  action='hold'
logs/runtime-20260801-213850.log:677   17:wombat.reflection:2026-08-02  action='hold'
logs/runtime-20260803-120804.log:79    17:wombat.reflection:2026-08-03  action='hold'
logs/runtime-20260803-120804.log:737   17:wombat.reflection:2026-08-04  action='hold'
logs/runtime-20260805-091501.log:37    17:wombat.reflection:2026-08-05  action='hold'
```

**Silence as the default is observed, emphatically: 10 candidates, 10 holds, 0
nudges.** The item is judged by the standard gate with no special path
(`pattern_detector.py:48-51` — the stage has no gate, pending-set or journal
collaborator; its only write is the injected `enqueue`). One item also decayed
naturally rather than surfacing: `runtime-20260803-120804.log:33` —
`DecayEvent(item_id='17:wombat.reflection:2026-08-02', age_seconds=122896.9)`.

The nagging-repeat failure mode NG-3 worries about is not present. If anything
the product errs the other way — see ISS-64.

## AC3 — the reflection text against the constitution — **BLOCKED**

**There is no reflection text to read. wombat has never produced one.**

Two independent searches, both empty:

- `grep -riE "reflection_compose|Mouth.REFLECTION" logs/*.log` → **zero hits in
  any log, ever.**
- every `item_kind='reflection'` gate decision across all logs filtered for
  anything other than `action='hold'` → **zero.**

The reflection is composed only if the item clears the gate and drains. Since no
item ever cleared, `ReflectionComposeStage` has never run in production. The
fourth persona mouth has never spoken.

**This is why it is BLOCKED and not a PASS.** The AC exists precisely because
the immutable guard suffix being *present in source* is not evidence that the
output *obeys* it. Reading the suffix instead of the output would be exactly the
substitution DEC-84 was written to stop. Nothing can be concluded about whether
wombat's reflections state observations rather than causes, avoid clinical
framing, or preserve autonomy — because there are none.

**What would unblock it:** one reflection that actually clears the gate. That is
not forceable inside a sweep (TK-369's non_goals forbid forcing a reflection the
gate did not clear, and rightly so). Routed as **ISS-64** with the shape of the
question for the architect.

Same precedent as TK-366, where `action_trail_projection` had zero rows in the
whole table and operator attestation could not cover a path that had never once
produced an artifact.

## AC4 — no dashboard, chart, streak or score anywhere — **PASS**

**Method (stated because it is not a screenshot).** The Electron app was not
running at sweep time, so this was verified by exhaustively inventorying the
UI surface rather than by photographing it. That is defensible here *only*
because the surface is small enough to enumerate completely — the renderer is
`App.tsx` plus a handful of panel components, all listed below. A screenshot
walk of every view belongs to TK-373 (SWEEP 11) regardless.

**Search:** `dashboard|streak|score|chart|graph|analytics|leaderboard` across
`app/src/**/*.{ts,tsx}` → **one hit**, `audio.ts:155`, the word "graph" in
*"Tears down the capture graph"* — the Web Audio API sense, not a chart.

**Positive inventory — what the app actually is:** `AudioPanel`, `DevicesPanel`,
`DangerZone`, `ChatPane`, `ChatDock`, `Button`, `Field`, and a settings body of
persona axes (Humor, Directness, Brevity, Warmth, Proactivity), assistant name,
brief time, model selection, API keys, and limits (daily token ceiling).

That is a **configuration surface, not a dashboard** — exactly what FEAT-13
promises and what NG-3 requires. No streak, no score, no progress meter, no
productivity chart. The behavior analysis feeds the gate; it is never displayed
back at the user.

---

## Findings routed

- **ISS-64 (new, MAJOR)** — the reflection path has never produced output in
  production: 10 candidates generated on 10 consecutive nights, 10 holds, zero
  compositions ever. The riskiest output in the product is entirely unexercised
  and AC3 cannot be run against anything.
- **ISS-63 (existing)** — gains a second documented consequence here:
  productivity windows are volatile for the same reason `RatingParams` are.

## State left behind

Read-only sweep. No runtime started or stopped, no database written, no KB entry
edited, no threshold changed, no reflection forced, no source or test file
touched. The runtime left up by TK-367 was untouched; the Electron app was not
running before or after.

## What this sweep does NOT claim

Per DEC-85(c) the sweep ran and its findings were routed. It does **not** assert
FEAT-8/FEAT-9 have PASSED — the constitutional check at the heart of this area
is blocked on an output that has never existed. TK-377 alone adjudicates.
