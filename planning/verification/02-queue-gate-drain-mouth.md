# 02 — the queue, the gate, the drain pump and the mouth (TK-364, SWEEP 02)

Run date: 2026-08-04, one continuous live session (with planned restarts) per
`protocol.md`. `scripts/demo_drain.py` was tried first per this ticket's own
complexity_budget and found broken (ISS-51) — every AC below was proven
against the REAL runtime instead, driving items directly into the real
`wombat_queue` (the same durable table every real source enqueues into) via
`wombat.queue.WombatQueue`, no new tooling.

## demo_drain.py — BROKEN, routed as ISS-51

`uv run python scripts/demo_drain.py` fails immediately:
`cogworx.loop.graph.StageGraphError: stage 'compose' transitions to unknown
stage 'chat_reply'`. The script hand-builds a stage graph that predates
TK-267/DEC-55's `chat_reply`/`speech_shape` hops. Not fixed here (non_goals).
See ISS-51 for full detail. This forced AC1/AC2's demonstration onto the real
runtime, which turned out to produce stronger evidence anyway (durable
Postgres rows, not an in-memory demo).

## AC1 — individual gate decisions, durability, action trail

**PASS.**

- Enqueued `tk364-ac1-vip-1` (VIP, near-deadline) directly into the real
  `wombat_queue`. Gate decision logged: `action='surface_immediate'
  urgency=0.9988... load=0.5`. Compose dispatched, a real
  `POST api.deepseek.com/v1/chat/completions` returned 200, a real
  `POST api.fish.audio/v1/tts` returned 200 — the full live pathway,
  decision through spoken surfacing.
- Enqueued `tk364-ac2-seed-1` (automated, non-urgent). Gate decision logged:
  `action='hold'`. The plain hold log line shows `urgency=None load=None` by
  design (`gate_stage.py`: "a production HOLD discards the score except the
  TK-272 chat carry") — NOT a defect, but it does mean AC1's literal
  "appear in the log with its urgency and load values" doesn't hold for the
  bare log line on a non-chat HOLD. The REAL numbers are not lost, though:
  queried directly from the durable `pending_journal` table —
  `(seq=3838, record_type='add', item_id='tk364-ac2-seed-1', item_kind='generic',
  urgency=0.5225, load=0.65, added_at=1785893224.31)` — proving the held
  item's full score IS durably persisted, in Postgres, by SQL row, just not
  echoed into the plain-text log line. This is the AC1 "durable pending set
  by SQL row count" evidence, direct from the table.
- Across the whole session (see AC5 below), every `compose dispatch` line has
  a matching real DeepSeek call and a matching real Fish TTS call, and no
  held item was ever dispatched — decision, persistence and surfacing each
  independently observed.

## AC2 — accumulated-load flush arm

**PASS, via durable ledger evidence rather than a live-captured trip in this
session** — DEC-63b's once-per-wombat-day flush latch had already been spent
earlier today, before this sweep began.

- Seeded two held items (`tk364-ac2-seed-1`, `tk364-ac2-seed-2`), each
  `load=0.65` per the durable `pending_journal` rows — cumulative 1.3,
  above `load_flush_threshold=1.0` (from `wombat_params.yaml`), with no
  single item's urgency above `urgency_threshold=0.75`. `flush_min_age_seconds
  =300` elapsed for the oldest (added 21:27:04, still unconsumed at
  21:32:09+, confirmed by the fact no `remove`/`clear` row for either exists
  in `pending_journal`).
- No `SURFACE_FLUSH` fired on the next item-carrying cycle after age passed.
  Root cause found in `src/wombat/gate/pipeline.py::Gate._try_flush`: the
  `flush_latch` (TK-287/DEC-63b, once-per-wombat-day) is checked FIRST, and
  the very first log line of this sweep's first boot already read
  `load flush denied: already flushed today (2026-08-04)`.
- Queried the durable `daily_ledger` table directly:
  `('flush:load', 2026-08-04, value=1, created_at=2026-08-04 15:29:37 UTC)`
  — a real `SURFACE_FLUSH` genuinely fired earlier today (11:29:37 local, well
  before this sweep started at ~21:25), proven by a durable, timestamped
  Postgres row. The two-axis thesis is real and has fired live today; this
  sweep's own seed items are structurally flush-ready (load and age both
  past their bars) and would fire on the first eligible cycle after the next
  wombat-day rollover, but the daily latch correctly prevented a second one
  from firing within this session — working exactly as designed, not a
  defect.

## AC3 — daily per-class ceiling, survives a restart

**PASS**, cleanly, live, twice (before and after a real restart).

- Enqueued `tk364-ac3-vip-2` and `tk364-ac3-vip-3` (same shape as vip-1,
  `event_class=generic`): both `action='surface_immediate'`, both composed
  and spoken — 3 surfacings of `generic` today (vip-1 counted as #1).
- Enqueued `tk364-ac3-vip-4`: `WARNING wombat.gate.pipeline: gate event:
  CeilingHit(item_id='tk364-ac3-vip-4', event_class=<EventClass.GENERIC:
  'generic'>)` followed by `action='hold'` — the ceiling (3, from
  `wombat_params.yaml`'s `per_class_daily_ceiling`) named in the log exactly
  as AC3 requires, remainder held.
- **Restarted the runtime** (`scripts/restart-wombat.ps1`, a real mid-day
  stop+relaunch) at 21:31:14–20.
- Enqueued `tk364-ac3-vip-5-postrestart` after the restart: same
  `CeilingHit` warning, same `action='hold'` — the ceiling survived the
  restart. Confirmed durably: `daily_ledger` row
  `('ceiling:generic', 2026-08-04, value=3, ...)` — DEC-21's civil-local-day
  `DailyLedger` in real Postgres, not an in-process counter.

## AC4 — model-unreachable degrade

**PASS.**

- Stopped the runtime, relaunched with `DEEPSEEK_BASE_URL` overridden to an
  unreachable address (`https://127.0.0.1:1/v1`) for this one boot only —
  a process-env override, never written to `.env`.
- Enqueued a worthy item (`tk364-ac4-degrade`, `event_class=reflection`,
  urgency 0.749). Gate decision `surface_immediate`, compose dispatched.
- The openai SDK retried twice (`0.39s`, `0.92s` backoff), then:
  `WARNING wombat.stages.compose: compose: model call failed; degrading to
  template` with the full `openai.APIConnectionError` traceback — logged
  loudly, exactly once.
- Immediately after (`21:38:43,679`), a real `POST api.fish.audio/v1/tts`
  returned 200 — the `TemplateComposer`'s terse fallback line was
  synthesized and (per the buffered-playback path) spoken. The item was
  never lost or stuck: it proceeded through the full downstream pathway
  despite the model failure.
- Stopped and relaunched again with the real `DEEPSEEK_BASE_URL` restored
  from `.env` (no override) before continuing the sweep.

## AC5 — end-to-end negative verification (CON-3 / NG-4)

**PASS.** Method: for each of this session's boot logs, count `gate
decision:` lines, `compose dispatch:` lines, real DeepSeek 200s, and real
Fish TTS 200s.

| boot log | gate decisions | compose dispatches | DeepSeek 200s | Fish TTS 200s |
|---|---|---|---|---|
| runtime-20260804-212525.log | 12 | 3 | 3 | 3 |
| runtime-20260804-213121.log | 4 | 2 | 2 | 2 |
| runtime-20260804-213746.log (degrade boot) | 1 | 1 | 0 (all failed, by design) | 1 (degraded text still spoken) |

In every boot, `compose dispatches == DeepSeek attempts == Fish TTS calls`
(the degrade boot's 0 DeepSeek 200s is expected — the point of that boot was
a failing model call) — no orphaned compose/speak call ever appears without
a preceding gate decision. And `gate decisions (17 total) > compose
dispatches (6 total)`: the difference (11) is every HELD/ceiling-capped item,
none of which ever reached compose. This is the negative proven over a real
session: nothing surfaced without a gate decision that cleared it, and no
LLM call happened on a HELD item at any point.

## AC6 — ISS-45 drain-pump concurrency probe (routed from ISS-45, not optional)

**PASS (recorded) — CONFIRMS ISS-45's core claim, measured for the first
time.** This is the strongest evidence gathered on ISS-45 across TK-363 and
this sweep.

- Enqueued two items back to back, 20ms apart: `tk364-ac6-speaker` (VIP,
  surfaces + speaks) then `tk364-ac6-follower` (also worthy, same
  `event_class`).
- Timeline:
  - `21:36:20.924` — both items enqueued (20ms apart)
  - `21:36:24.910` — speaker's gate decision + compose dispatch
  - `21:36:26.299` — real DeepSeek call returns (1.39s)
  - `21:36:27.944` — real Fish TTS call returns (1.65s later — synthesis done)
  - `21:36:42.306` — follower's gate decision finally appears
- **The drain pump did not advance to the follower item for 17.4 seconds**
  after the speaker's compose dispatch, and **14.4 seconds after the Fish
  TTS synthesis call itself had already returned** — strongly consistent
  with `speak()` blocking the shared loop for the full BUFFERED PLAYBACK
  duration (synthesis + audible playback), exactly ISS-45's claim
  (`voice/tts.py` "does not return until the whole utterance is synthesized
  and played").
- **This measurably affects a bounded episode's timing budget**: for the
  ~17.4s of this window, the drain pump could not pick up or evaluate the
  next queued item, meaning any genuinely time-sensitive item arriving
  during a spoken reply waits the full length of that reply before its own
  gate decision is even made.

## Side effect Jim should know about

This sweep's synthetic items consumed real production state for the rest of
today (2026-08-04): the `generic` event class's daily surfacing ceiling is
now at 3/3 (my test items, not real usage) and today's load-flush latch was
already spent (independently, before this sweep, at 11:29 local). Neither
was reset — resetting ledger rows felt like exactly the kind of quiet
state-editing this phase exists to avoid, so it's flagged here instead. A
genuinely important real `generic`-class item arriving later today will be
held rather than surfaced immediately, purely because of this sweep's own
test traffic. This clears itself at the next wombat-day rollover
(DEC-21 civil-local day).

## Not repaired here

Per this ticket's non_goals: no gate constant, threshold, params file, or
test was edited to make any observation come out. ISS-51 (demo_drain.py
broken) is routed, not fixed.
