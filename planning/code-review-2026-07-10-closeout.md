# wombat — FINAL whole-repo closeout review register (2026-07-10)

**Status:** FINAL. This is the board-empty closeout pass that contract **v2.38** records as the
final independent review, subsuming all residual arc-level review obligations (the superseded CR6
line and any pending batch-verify residue). Board state at review: **146/146 tickets done**.

**Scope:** the whole repository at HEAD (`bd32890`), reviewed across **8 dimensions** —
safety, runtime, persistence, voice, persona, app (Electron), tests (suite integrity),
integration/config. Every claimed finding was **adversarially verified** by an independent pass:
runnable repros against real seams (installed Playwright/Chromium, real cog-worx Engine/Sweeper,
a throwaway Postgres on 5438 — live 5436 untouched, real TestClient app surface) before any
claim was admitted. Verdicts below are CONFIRMED / DOWNGRADED-MINOR only; refuted claims are
counted, not detailed.

**Outcome at a glance:** 10 CONFIRMED (1 critical, 4 major, 5 minor), 0 downgraded, 2 refuted.

---

## Per-dimension clean summaries

### 1. Safety

I traced the full safety spine against the installed cog-worx source and found it solid on every
dimension except one. The taint latch is framework-owned and correctly wired: email/web body reads
go through tagged untrusted-source read-tier capabilities that latch TaintState in `dispatch_one`
before invoke; the tier gate drops external on a tainted drive. CON-5 never-send holds
structurally — `gmail.messages.send` is registered nowhere; only `drafts.create` (external, held
for human) exists. The Q-114 precedence holds: `dispatch_approved` delegates to `dispatch_one`
with `approved=True`, which bypasses only the ApprovalRequired check, never `check_dispatch`, so
an approved external dispatch on a tainted drive still raises TierViolation. Egress posture
(DEC-28) is clean: stt/tts providers default to local, cloud classes are constructed only in
`voice/select` on explicit provider opt-in, construction does no network I/O, and voice building
is gated on config flags; the sole default egress is DeepSeek. Residency (CON-7) runs at startup
(`check_config`) and in `build_substrate` for pg+neo4j, with the TK-183
all-resolved-addresses-local fix intact. Prompt-injection surface is bounded — untrusted body/page
content reaches the model only as user-role data behind a fixed system instruction, and
DraftComposer's model call exposes no tools. The single defect is the password-fill guard's
case-sensitive type comparison, which a page can defeat with uppercase type casing (confirmed
end-to-end with a local Playwright repro). → **CRF-1**.

### 2. Runtime

I read the real bootstrap assembly, runtime serve loop, the cogworx Engine/_drive and Sweeper
(sibling checkout ground truth), the queue/gate/drain-queue/review-or-speak stages, DailyLedger
civil-day math, the brief/dream timers, ceiling/decay, and schema pre-flight. The
civil-day/timezone threading is solid: `wombat_today` requires an aware instant, every
DailyLedger/engine clock defaults to aware UTC (no naive-datetime seam), `is_due`/`next_fire_at`
do absolute-instant DST-safe math, and the ceiling/rollover fences reset structurally by
(ledger_name, wombat_date) key with exactly-once boundary observation. Compose/gate degrade paths
catch their own exceptions. The schema pre-flight and ledger math handle empty-db and
midnight/month-roll cleanly. However, the core standing drain loop is broken: the drain graph
terminates on Done, so the single serve()-fired drain run COMPLETES after processing one item and
the Sweeper (which only wakes runs with due timers) can never re-drive it — every later queued
item is stranded (critical, proven with a runnable repro). Independently, all eternal self-park
runs accumulate one journal step per Sweeper poll and hit the cogworx max_steps=1000 ceiling,
FAILING the idle drain in ~83 minutes (major, proven). Both drive the standing process's core
loop to silent death within ~1.5 hours of boot. → **CRF-2**, **CRF-3**.

### 3. Persistence

I read the five pg-backed modules (queue, daily_ledger, pending_journal_pg, trail/writer+schema,
behavior/event_log), their migrations, schema_preflight, and the gate/pending_set replay path,
then exercised them against a throwaway pg on 5438 (fresh db, dropped after; live 5436 untouched).
The adapters themselves are solid: `ensure_all_schemas` is idempotent and covers every table
written anywhere (queue, daily_ledger, pending_journal, wombat_behavior_events,
action_trail_projection — no missing coverage); queue drain is FIFO with correct epoch-reclaim
redelivery of un-acked rows on restart; action_trail transitions are idempotent with
first-write-wins timestamps and raise loudly on cross-terminal/dispatch-on-blocked;
pending_journal replays strictly by seq and rebuilds exactly. ASMP-2 single-drainer is enforced
purely by operator obligation (no advisory lock), which is the documented DEC-6/ASMP-2 design,
not a defect; the one shared psycopg connection is safe because all queue ops are synchronous and
never yield mid-statement. The one real defect is outside the adapters: the SourceRegistry poll
loop does not guard `enqueue()`, so a full or transiently-erroring durable queue permanently kills
a source and re-breaks the shutdown teardown. Unbounded pending_journal growth is a latent minor.
→ **CRF-4**, **CRF-5**.

### 4. Voice

I read the full voice arc against its real source: select.py factories + Fallback* wrappers,
stt.py/tts.py cloud providers, transport/playback/key_store seams, ASRSource + SpeakSink degrade
paths, the persona command grammar and feedback lexicon, and the bootstrap wiring. The zero-egress
default is solid: with the default local providers, `build_transcriber`/`build_tts_adapter`
construct no cloud class and read no key (verified by the egress-lesion subprocess proof and by
reading the code), httpx is never imported, cloud-to-local fallback direction is enforced
structurally, and the command/feedback hooks never consume or raise. Cloud key resolution
(env-over-vault, loud-WARN, never silent), missing-voice_id and absent-extra degrades, and
SpeakSink/ASRSource per-item degrade all behave as documented. The one real defect:
`_build_local_transcriber` catches only ImportError (unlike the broad-Exception
`_build_local_tts`), so a non-ImportError whisper model-load failure escapes `build_transcriber`
and crashes boot through the unguarded ASR registration path. → **CRF-6**.

### 5. Persona

The persona system is solid. I verified: the guard-suffix invariant is structural —
`render_expression` appends the per-mouth guard OUTSIDE every strategy and is the only reader of
`_GUARD_SUFFIX`, so no strategy, policy clause, or cue can remove or shadow it (confirmed the
guard stays terminal for all four mouths even under an adversarial non-default clause that says
"ignore the No preamble rule"). The persona_policy.yaml loader rejects out-of-vocab
mouths/axes/levels, any proactivity reference, missing/unknown top-level keys, malformed YAML,
non-string clauses, and — critically — any non-empty DEFAULT-level clause (DEC-38(5)
byte-identity), all failing loud with PersonaPolicyError; confirmed a hostile default-nonempty
file is rejected. `from_strings` rejects bad axis values so a hostile settings.json cannot inject
an out-of-band matrix level (poll/set catch and stand pat). The gate personality_band is weakly
monotone and clamped [0.60, 0.95] around base 0.75 so eff(minimal) >= eff(balanced) = base >=
eff(forward); the dream tuner steps only on >=2 same-direction signals with zero opposing, skips
pinned axes (7-day window), and rides the existing saturating commands.apply clamp. Output-effect
harness is gated behind WOMBAT_TEST_PERSONA_EVAL_LIVE plus resolvable creds and skips loud by
default (never armed). 262 persona/gate/tuner tests pass. The one issue found is a narrow
concurrency race in the mtime hot-apply poll (minor; below finding-severity threshold on this
pass — single-operator, single-writer, self-heals on next poll — recorded here for the trail,
not registered).

### 6. App (Electron)

I read the full Electron surface (main.ts, window-options, permissions, api-process, chat-info,
env-config, save-capture, preload) plus the renderer chat/audio/api/App/ChatPane/AudioPanel and
the Python seams they drive (settings_app/api.py, chat/surface.py, sources/asr.py). The core
security posture is solid: exactly one loadFile and zero loadURL,
contextIsolation+sandbox+nodeIntegration:false, CSP connect-src pinned to self/loopback only, the
permission handler pinned to media-only, per-launch tokens ride only headers (never URLs) and are
exposed solely via three contextBridge invoke shims. The chat surface's register-before-push
ordering, 401 auth, header/body bounds (64 lines, 1 MiB) and honest 30s held/timeout are all
correct, and renderer degrade states (chat unavailable, drop-dir-not-configured, restart notices)
are honest rather than silent — chat text is React-escaped so no XSS/navigation vector exists.
`npm test` (189 passed) and `npm run lint` (tsc) both green. Findings are all minor and latent: a
non-atomic settings.json write that can silently reset settings on a torn write, a non-atomic
mic-capture drop into the polled ASR dir (low-probability partial-read race at the 300s poll;
below registration threshold), and a macOS-only reopen-after-close lifecycle gap that the Windows
operator won't hit. → **CRF-7**, **CRF-8**.

### 7. Tests (suite integrity)

I audited the suite for test-honesty defects: tautologies/mocked-to-green, fakes diverging from
real seams, dead skip-gates, fixture drift, and the 70 pg-DSN skips. I stood up a throwaway
Postgres on 5438 and spot-ran every pg-gated module (~237 executions across gate, integration
e2e, safety, trail, behavior, unit, persona, pathways) — all passed on real pg, so none of the 70
skips are dead; the default run is exactly 1496 passed / 70 skipped with no hidden xfails. The
non-pg skips are win32-gated voice tests (which run on the operator's Windows box) and documented
manual-arm live harnesses (WOMBAT_TEST_GMAIL_LIVE / GCAL_LIVE / PERSONA_EVAL_LIVE) whose
measurement/trip-wire logic is covered by ungated non-live unit tests. Safety-critical tests
exercise real seams rather than fakes — residency via a real psycopg-connect recorder and a real
Model chain with httpx interception, the taint latch via real in-process cog-worx
Registry/ToolGate/dispatch_one, and the password guard via real playwright/chromium against a
local form; the few mock-based substrate tests patch real cog-worx adapter paths (which patch()
would reject if absent) with non-trivial assertions. The only runtime-tautological assert I found
is backed by mypy-strict coverage of the tests tree, so its protocol-conformance claim has a real
static home. **No suite-integrity defects to report.**

### 8. Integration / config

I audited the cross-cutting integration + config surface: every WombatConfig field has both a
documented write side and a live read site (persona axes → matrix_from_config + gate
effective_urgency_threshold; voice/tts → voice.select builders; timezone →
resolve_wombat_zone; chat/brief/feedback/asr → their _maybe_register/build_* guards); no config
field is read-nowhere or written-nowhere. The drain, brief, brief_schedule, dream, and
dream_schedule StageGraphs each have every declared transition resolving to an included node with
a single reachable terminal, including the conditional draft leg and the router's fallback edge.
schema_preflight.ensure_all_schemas covers all five packaged pg tables; the in-memory
substrate/entity_kg cold-boot means NEO4J is correctly unused. The EP-25/EP-26 browser arc
(BrowseAndRead, IngestWebPage/EmailBody, FormSubmit, LoginHandoff, DispatchApproved) is unwired
into any pathway — confirmed deliberate per the tickets' caller-concern framing, not accidental
dead wiring. I proved the clean-import claim empirically: a base-only
`uv sync --frozen --no-default-groups` install (no extras) imports
wombat.runtime/bootstrap/voice.select/capabilities.playwright_capability/sinks/sources cleanly,
with fastapi/uvicorn/playwright/pyttsx3/faster_whisper absent and all their imports lazy or
TYPE_CHECKING-guarded. Only two minor doc/wiring nits surfaced, both listed. → **CRF-9**,
**CRF-10**.

---

## CONFIRMED findings

### CRF-1 — Password-fill deny-always guard bypassed by non-lowercase `type` attribute casing

- **Severity:** MAJOR
- **Dimension:** safety
- **File:** `src/wombat/capabilities/playwright_capability.py`
- **Claim:** `_checked_fill` (the ONE shared password guard, TK-136/Q-114 ruling h, documented as
  UNCONDITIONAL deny-always that "no code path may EVER bypass") decides purely on
  `field_type == "password"`, a case-sensitive Python compare against
  `locator.get_attribute("type")`, which returns the page's verbatim attribute casing. A password
  input authored `<input type="PASSWORD">` (or any non-lowercase casing) is a genuine
  browser-masked password field (`element.type == 'password'`) yet `get_attribute` returns
  `'PASSWORD'`, so the compare is False, the guard does not fire, and `locator.fill(value)`
  writes into the password field. The page is untrusted content in the browsing threat model and
  fully controls the casing, so the absolute guarantee is page-defeatable. Fix:
  compare `(field_type or '').lower() == 'password'` (or read the normalized IDL `.type`).
  Reachability today is latent — the browser capability/type/submit_form actions are not yet
  wired into any live bootstrap pathway — but the stages ship on the closed board and the guard's
  own tests exercise exactly this `get_by_role('textbox')` fill path.
- **Adversarial evidence:** I could not refute this; I reproduced it end-to-end against installed
  Chromium. `_checked_fill` (line 255) does a case-sensitive `field_type == "password"` against
  `locator.get_attribute("type")`, which returns the page's verbatim attribute casing. My repro
  served a local form and typed into role=textbox name=Password. With `type="password"`:
  getAttribute='password', el.type='password', invoke returned `password_field_blocked` and
  `#pw.value` stayed ''. With `type="PASSWORD"` and `type="Password"`: getAttribute returned the
  verbatim casing while `el.type=='password'` (a genuine browser-masked password field), the
  guard did NOT fire, and the fill landed — `#pw.value=='hunter2'` both times. So the documented
  UNCONDITIONAL deny-always guarantee ("no code path may EVER bypass") is defeated by trivial
  page-controlled attribute casing, which is untrusted-content in the browsing threat model. The
  proposed fix `(field_type or '').lower() == 'password'` is correct. Reachability is latent as
  the reviewer honestly disclosed: PlaywrightCapability is not registered in bootstrap.py (only
  the browser stages reference it), so the operator will not hit it in today's daily runtime —
  but the guard and its tests ship on the closed board and the bypass is real. Severity major
  stands: it silently defeats an absolute safety guard the moment the built browser stages are
  wired.
- **Repro:** local, no external site —
  `goto data:text/html,<label>Password <input id=pw type=PASSWORD></label>;`
  `PlaywrightCapability.invoke({action:'type',role:'textbox',name:'Password',value:'hunter2'})`
  returns `{ok:True}` (NOT `password_field_blocked`) and `#pw.value=='hunter2'`, while
  `el.type=='password'`. Identical call with lowercase `type='password'` correctly returns
  `password_field_blocked`. Verified end-to-end against installed Playwright/Chromium.

### CRF-2 — Standing drain run COMPLETES after one item — queue goes permanently deaf

- **Severity:** CRITICAL
- **Dimension:** runtime
- **File:** `src/wombat/runtime.py`
- **Claim:** `_drive_and_serve` fires exactly ONE `engine.run` for the drain pathway (run_id
  `wombat-drain-<uuid>`) then relies solely on the Sweeper. But the drain graph terminates on
  Done: review_or_speak returns Done on a HOLD (review_or_speak.py:209) and SpeakSink returns
  Done/Degraded(to=None) on a surface (speak.py:66/90/105). So the moment the drain processes its
  FIRST batch the run reaches COMPLETED (or DEGRADED) — a terminal state whose timers are
  cancelled. The Sweeper only re-drives runs with a due timer, so it can never wake a COMPLETED
  run. Every item enqueued after the first is stranded in the pg queue forever; after boot wombat
  drains exactly one item then is deaf until a full process restart (which drains one more, then
  dies again). While the queue is empty the drain survives by self-parking (drain_queue Wait), so
  it dies on the first item that ever arrives. The AC5 acceptance test
  (tests/integration/test_serve_boot.py:161) asserts status is COMPLETED after ONE item and never
  enqueues a second, which is exactly why this slipped verification.
- **Adversarial evidence:** I built a runnable repro (scratchpad/repro_drain.py) wiring the REAL
  wombat DrainQueueStage into the REAL cogworx Engine/Sweeper over InMemoryJournal, with a
  terminal 'gate' stage returning Done (faithful to review_or_speak's HOLD-Done and speak's
  Done/Degraded(to=None), both terminal). Output: boot on empty queue parks WAITING
  (drain_calls=1); enqueue 2 items; tick 1 fires the drain_queue Wait timer, resumes the run,
  drains item-1, hits Done → run COMPLETED (drain_calls=2); ticks 2–5 report timers_fired=0 and
  item-2 is NEVER drained. I verified the mechanism in cog-worx engine.py: the Done case sets
  COMPLETED and calls cancel_timers_for_run (lines 855–857), and the drain graph
  (bootstrap.py:918) is linear drain_queue→gate→…→speak(terminal) with no edge back to
  drain_queue except its own empty-queue Wait; the SourceRegistry is enqueue-only, and
  `_drive_and_serve` fires exactly one drain `engine.run`. So after the first queued item flows
  through, the drain run is terminal with no armed timer and the Sweeper can never re-drive it.
  AC5 (test_serve_boot.py:161) asserts COMPLETED after one item and never enqueues a second,
  matching the bug. No contract decision defers or sanctions single-item draining. Critical: the
  queue-drain path goes permanently deaf after exactly one item per process lifetime.
- **Repro:** `scratchpad/repro_drain.py` (real DrainQueueStage + in-memory cogworx Engine/Sweeper,
  2-item fake queue): after initial engine.run status=completed, drain_calls=1; every subsequent
  Sweeper.tick reports timers_fired=0 and item-2 is NEVER drained.

### CRF-3 — Eternal self-park runs accumulate journal steps and FAIL at max_steps=1000

- **Severity:** MAJOR
- **Dimension:** runtime
- **File:** `src/wombat/bootstrap.py`
- **Claim:** `build_engine` constructs cogworx Engine without max_steps, so the default 1000
  stands (engine.py:112). Every wombat standing pathway self-parks forever on a Wait onto itself
  (DrainQueueStage idle heartbeat, BriefTimerStage, DreamTimerStage). Each Sweeper re-drive
  replays the whole committed prefix from the entry and commits ONE new step, so the run's
  committed-step count grows by 1 per poll; at seq>=max_steps the engine flips the run to FAILED
  and cancels its timers (engine.py:747–751), silently (no event_sink is wired, so no log). The
  drain self-parks every _DRAIN_POLL_INTERVAL_SECONDS=5s, so a purely-idle drain that never
  receives an item hits 1000 in ~83 minutes and dies — the same net outcome as CRF-2 via a
  different trigger, and the ACTIVE killer whenever the queue stays empty. The
  brief_timer/dream_timer runs self-park once per day, so they FAIL after ~1000 days. Replay cost
  is also O(n) per poll (O(n^2) overall).
- **Adversarial evidence:** I could not refute it — I reproduced it for real. Using the REAL
  DrainQueueStage (returns Wait(to=self) on empty queue, drain_queue.py:74) driven by the REAL
  cogworx Engine + fire_timer over an in-memory journal, an always-empty queue's idle heartbeat
  accumulates exactly one committed step per Sweeper poll: my run printed 201/401/601/801
  committed steps and the run flipped to FAILED at 1000 steps on poll 1000 — matching the claimed
  trajectory precisely. Ground truth confirms the wiring: engine.py:112 defaults max_steps=1000,
  bootstrap.py build_engine (322–339) passes neither max_steps nor event_sink, and runtime.py:157
  starts ONE long-lived drain run that only Sweeper.fire_timer re-drives (engine.py:594 replays
  the whole committed prefix from graph.entry each poll), with nothing restarting it after
  FAILED. At the 5s drain heartbeat a purely-idle wombat dies in ~83+ minutes, silently (no sink
  wired, so the RUN_FAILED emit at engine.py:750 goes nowhere). No contract decision rules this
  acceptable (the runtime v1 note only covers restart/cold-boot, not step accumulation). Severity
  is at least major as stated — arguably understated, since a normal idle window permanently
  halts all queue processing until manual restart. Verdict: CONFIRMED.
- **Repro:** `scratchpad/repro_maxsteps.py` (self-parking Wait stage + in-memory Engine/Sweeper):
  committed steps climb 201, 401, 601, 801 and at poll 1000 the run flips to FAILED with 1000
  steps.

### CRF-4 — SourceRegistry poll loop leaves enqueue() unguarded — a full/erroring queue permanently kills a source and breaks shutdown

- **Severity:** MAJOR
- **Dimension:** persistence
- **File:** `src/wombat/sources/registry.py`
- **Claim:** In `_poll_loop` the try/except only wraps `await source.poll()`; the `else` branch
  that calls `self._enqueue.enqueue(...)` is not guarded. A QueueFullError (wombat_queue at
  max_pending=100) or any psycopg error (e.g. a reaped/dropped connection to the live pg)
  therefore propagates out of the loop and terminates that source's asyncio task for good — the
  source silently stops polling until a full process restart, with only a late "Task exception
  was never retrieved" log. This directly contradicts the team's own guarding precedent for the
  exact same failure modes at another enqueue call site (PatternDetectorStage, TK-204 CR3-2,
  which catches both QueueFullError and a reaped-connection psycopg.OperationalError).
  Compounding: the dead task's stored exception is not CancelledError, so `registry.stop()`
  re-raises it from `await task`, aborting the remaining stop() iterations and the whole runtime
  `finally` teardown (queue/ledger/pending_journal/behavior_event_log connections never closed).
- **Adversarial evidence:** Reading registry.py:96–111, the try/except wraps only
  `await source.poll()`; the else branch's `self._enqueue.enqueue(...)` is unguarded.
  queue.py:133 confirms `enqueue` raises QueueFullError at capacity (and does a DB call, so a
  reaped pg conn raises psycopg.OperationalError the same way). I ran a runnable repro
  (PYTHONPATH=src) with an event-emitting source and an enqueuer that raises QueueFullError:
  after ~1 poll `task.done()` is True and `task.result()` re-raises QueueFullError — the source
  is dead, no retry. `await reg.stop()` then RE-RAISED QueueFullError (stop() at registry.py:88
  only suppresses CancelledError). runtime.py:196–210 shows
  `await bundle.source_registry.stop()` is the FIRST line of the shutdown `finally`, so the
  re-raise skips queue/daily_ledger/pending_journal/behavior_event_log/action_trail closes —
  those connections leak. This contradicts the team's own precedent: PatternDetectorStage
  (pattern_detector.py:184–201) catches both QueueFullError and generic Exception around the
  identical enqueue seam. TK-3 AC4 only mandates guarding poll() raises, not the enqueue site, so
  this is not contract-ruled. Major: an operationally-real event (queue at 100 pending or a
  dropped pg connection) silently kills a source until full restart and breaks shutdown teardown.
- **Repro:** PYTHONPATH=src, drive SourceRegistry with a source that emits an event each poll and
  an enqueuer that raises QueueFullError: after ~1 poll the task is done() and task.result()
  re-raises QueueFullError; `await registry.stop()` also re-raises it. poll()-errors by contrast
  degrade+retry.

### CRF-5 — pending_journal is append-only with no compaction and is fully replayed on every boot

- **Severity:** MINOR
- **Dimension:** persistence
- **File:** `src/wombat/gate/pending_journal_pg.py`
- **Claim:** PgPendingJournal.append only ever INSERTs; nothing trims the table, and
  rebuild_from_journal replays every row (SELECT ... ORDER BY seq ASC) at each boot. A "clear" or
  a remove leaves all prior add/remove rows durably present forever, so the journal grows without
  bound over long daily use and every restart re-reads the entire history. Final replayed state
  is correct (verified: add i1, add i2, remove i1 → rebuilt {i2}), so this is latent, not a
  correctness bug — but boot-time replay cost and table size grow monotonically with the
  operator's lifetime usage, with no checkpoint/snapshot to bound them.
- **Adversarial evidence:** Code reading confirms the mechanism exactly: PgPendingJournal.append
  only ever INSERTs (add/remove/clear), replay() is one SELECT ... ORDER BY seq ASC with no
  LIMIT, rebuild_from_journal replays every row, and a repo-wide grep for DELETE/TRUNCATE/DROP
  finds none against pending_journal (the only DELETE is queue.py on wombat_queue, a different
  table). Runnable repro against a throwaway pg (docker postgres:16 on 5438, removed after):
  appended 2000 adds + 1997 removes; on-disk rows=3997, replay returned all 3997 records, live
  items after rebuild=3, rebuild_correct=True. So the journal is genuinely append-only with no
  checkpoint/compaction and boot replays the full history regardless of live count — exactly as
  claimed, and final state stays correct (latent, not a correctness bug). Severity minor is
  accurate: it is a deliberate WAL+replay design under NG-3 austerity, and replay of ~4000 rows
  took 5ms, so for a single-operator personal assistant with a small max_pending set the
  monotonic growth is operationally negligible over the app's lifetime.
- **Repro:** append N add/remove pairs then rebuild_from_journal — result is correct but replay()
  returns all N rows regardless of how few items remain live; no code path deletes or checkpoints
  pending_journal rows.

### CRF-6 — Local ASR build catches only ImportError, so a whisper model-load failure crashes boot

- **Severity:** MAJOR
- **Dimension:** voice
- **File:** `src/wombat/voice/select.py`
- **Claim:** `_build_local_transcriber` wraps `FasterWhisperTranscriber(model_name=...)` in
  `except ImportError` ONLY, while its sibling `_build_local_tts` catches broad Exception.
  FasterWhisperTranscriber.__init__ loads the CTranslate2/whisper model at construction, which
  raises NON-ImportError errors on realistic conditions: model not yet cached + offline
  (huggingface_hub LocalEntryNotFoundError, an OSError/FileNotFoundError), a bad wombat_asr_model
  name (HFValidationError/RepositoryNotFoundError), or a corrupted cache. Any of these propagates
  out of build_transcriber → _maybe_register_asr (no guard) → build_source_registry (no guard) →
  assemble_runtime, crashing boot. This hits BOTH the default local-STT path and the case where a
  cloud STT primary is healthy but its best-effort local fallback slot fails to construct
  (build_transcriber then raises despite a working cloud primary). It violates CON-3 (voice is
  additive, never blocks boot), the _build_local_transcriber docstring's own "never blocks boot"
  promise, and _maybe_register_asr's "Neither missing piece ever raises" claim. faster_whisper
  1.2.1 IS installed on the operator box, so this is reachable the first time voice input is
  enabled before the "base" model is fetched, or on any transient HF-hub/offline condition at
  boot.
- **Adversarial evidence:** Verified src/wombat/voice/select.py: _build_local_transcriber
  (line 126–141) catches ImportError ONLY, while its sibling _build_local_tts (155–170) catches
  broad Exception. FasterWhisperTranscriber.__init__ (asr.py:110–113) builds
  WhisperModel(model_name) at construction. Repro in a scratch dir with
  WombatConfig(deepseek_api_key='sk', deepseek_base_url='https://x') (wombat_stt_provider
  defaults 'local'): monkeypatching FasterWhisperTranscriber to raise RuntimeError,
  build_transcriber(cfg, key_store=None) RE-RAISED RuntimeError while the parallel
  build_tts_adapter returned None gracefully. WhisperModel is real (faster_whisper 1.2.1
  installed): offline, WhisperModel('bad-name') raises ValueError (non-ImportError) synchronously
  at construction, no network; an uncached model offline raises an OSError-family HF error. Boot
  chain is unguarded: sources/bootstrap.py:454 _maybe_register_asr calls build_transcriber(config)
  with no try/except, itself called unguarded by build_source_registry, so the raise propagates
  and crashes boot. This contradicts the function's own "never blocks boot" docstring,
  _maybe_register_asr's "Neither missing piece ever raises" claim (bootstrap.py:433), and CON-3
  (voice additive). No contract decision rules the narrow except intentional. Also hits the
  cloud-primary-healthy path (line 232 role='fallback'). Major: real boot crash on a realistic,
  reachable condition (voice enabled + uncached/offline or bad WOMBAT_ASR_MODEL). Fix: broaden
  the except to Exception.
- **Repro:** cd to a scratch dir; WombatConfig(deepseek_api_key='sk',
  deepseek_base_url='https://x') has wombat_stt_provider=='local'; monkeypatch
  wombat.voice.select.FasterWhisperTranscriber to a class whose __init__ raises
  RuntimeError('model not cached and offline'); build_transcriber(cfg, key_store=None) RAISES
  RuntimeError (crash), whereas the parallel build_tts_adapter with a raising Pyttsx3Adapter
  returns None (graceful).

### CRF-7 — settings.json write is non-atomic; a torn write silently resets ALL settings

- **Severity:** MINOR
- **Dimension:** app
- **File:** `src/wombat/settings_app/api.py`
- **Claim:** PUT /settings does existing.update(...) then settings_path.write_text(...) directly
  over the live file (no temp+os.replace). If that write is interrupted (crash/kill/full disk
  mid-write), the file is left truncated. _read_settings (same module) swallows
  JSONDecodeError/OSError to {}, so the very next GET shows every app-editable field as null and
  the next PUT writes only the touched fields onto an empty dict — the operator's
  previously-saved persona/provider/voice settings are silently discarded with no error banner
  anywhere.
- **Adversarial evidence:** Reproduced with a runnable repro against create_app + TestClient
  (fake key store). Seeded wombat.settings.json with deepgram/elevenlabs/voice-xyz/
  persona-humor=dry; GET returned them. I then left truncated JSON on disk (simulating a torn
  write); GET returned every app-editable field as null (no error). A subsequent PUT of one
  unrelated field (wombat_voice_enabled=true) wrote a file containing ONLY that key — all
  previously-saved persona/provider/voice settings discarded, API surfaced no error. Root cause
  confirmed by reading api.py: put_settings (line 148) does existing.update then a bare
  write_text with no temp+os.replace, and _read_settings (100–106) swallows
  JSONDecodeError/OSError to {}. The chain is real (non-atomic write → possible truncated file →
  read degrades to empty → next PUT loses untouched fields). Could not refute: it reproduces
  exactly, and no contract decision exempts settings writes from atomicity (the codebase even
  uses atomic temp+os.replace in trail/renderer.py with a dedicated test). Severity minor is
  accurate: latent, requires a crash/full-disk at the instant of a sub-1KB write; the runtime
  config loader logs a WARNING on the corrupt read though the Electron UI stays silent.
- **Repro:** truncate wombat.settings.json to invalid JSON (simulate a torn write), then call
  GET /settings with the token: every field returns null instead of the saved values; a
  subsequent PUT persists only the touched field.

### CRF-8 — macOS: closing all windows kills the settings-API child but reopening never respawns it

- **Severity:** MINOR
- **Dimension:** app
- **File:** `app/electron/main.ts`
- **Claim:** window-all-closed calls teardownApiProcess() (child.kill) BEFORE the
  `process.platform !== 'darwin'` quit guard, so on macOS the settings-API child is killed while
  the app stays resident in the dock. The `activate` handler recreates the BrowserWindow but
  never re-runs startApiProcess, and ipcMain.handle('wombat:settings-api-info') is closed over
  the dead child's stale {port,token}. Reopened window's getSettings() hits a refused port →
  permanent "Failed to load settings" until full relaunch. Operator runs Windows
  (window-all-closed → app.quit()), so is not affected; cross-platform correctness bug only.
- **Adversarial evidence:** Read app/electron/main.ts and api-process.ts. main.ts:99–104 shows
  window-all-closed calls teardownApiProcess() (which runs makeTeardown's child.kill(),
  api-process.ts:84–93) BEFORE the process.platform !== 'darwin' quit guard, so on macOS the
  settings-API child is killed but the app stays resident. Grep confirms startApiProcess is
  called exactly once (main.ts:49, inside app.whenReady), never in the activate handler
  (main.ts:92–96, which only calls createWindow). ipcMain.handle('wombat:settings-api-info')
  closes over the stale `info` (main.ts:74), and App.tsx:271 renders "Failed to load settings" on
  the refused port. Bug is real and correctly labeled minor: operator runs Windows where the
  guard fires app.quit(), so it is a macOS-only cross-platform correctness bug.
- **Repro:** on macOS — close all windows, then click the dock icon to reactivate; the settings
  pane renders the load-error banner and never recovers without quitting and relaunching.

### CRF-9 — .env.example omits the morning-brief and other operator env vars, and carries a stale persona comment

- **Severity:** MINOR
- **Dimension:** integration
- **File:** `.env.example`
- **Claim:** the operator template .env.example (copy-to-.env, the file an operator provisions
  from) never lists WOMBAT_BRIEF_PATH / WOMBAT_VOICE_ENABLED (the morning brief — the headline
  daily feature the operator relies on tomorrow), nor
  GOOGLE_OAUTH_CLIENT_ID/GOOGLE_OAUTH_CLIENT_SECRET (the gcal/gmail sources),
  WOMBAT_CHAT_HANDSHAKE_FILE (chat pane), WOMBAT_FEEDBACK_FILE, or
  WOMBAT_ASR_DROP_DIR/WOMBAT_ASR_MODEL. All are documented in README.md and read by config.py,
  so this is a template-completeness gap, not a boot break. Line 43's comment "nothing reads
  these yet (TK-209 owns hot-apply)" is also stale: persona axes are now read at boot
  (persona/matrix.matrix_from_config, live-polled, and gate-actuated via
  effective_urgency_threshold).
- **Adversarial evidence:** Read .env.example fully (64 lines): it omits WOMBAT_BRIEF_PATH,
  WOMBAT_VOICE_ENABLED, GOOGLE_OAUTH_CLIENT_ID/SECRET, WOMBAT_CHAT_HANDSHAKE_FILE,
  WOMBAT_FEEDBACK_FILE, WOMBAT_ASR_DROP_DIR, WOMBAT_ASR_MODEL. All are real WombatConfig fields
  (config.py 171–201, 245) and all are documented in README lines 36–43. No contract decision
  scopes the template to a subset; TK-187/208 budgets show operator vars are meant to land in
  .env.example. The stale-comment claim also holds: line 43 says persona axes are unread
  ("nothing reads these yet"), but bootstrap.py:654 loads matrix_from_config(config) into
  LivePersona at boot and :707 wires effective_urgency_threshold into the gate (both defs
  exist). Finding reproduces exactly; severity minor is correct since every field is optional
  with a default (no boot break) — a template-completeness/doc-drift gap, not refutable.
- **Repro:** read .env.example end-to-end and grep for WOMBAT_BRIEF_PATH /
  GOOGLE_OAUTH_CLIENT_ID (absent); compare to README.md "Optional environment" and
  config.WombatConfig fields.

### CRF-10 — `wombat` console-script entry point resolves to a Hello-World stub, not the runtime

- **Severity:** MINOR
- **Dimension:** integration
- **File:** `pyproject.toml`
- **Claim:** [project.scripts] declares `wombat = "wombat:main"`, and src/wombat/__init__.py's
  main() is a placeholder that only prints "Hello from wombat!" and returns. So an installed
  `wombat` command (e.g. `uv run wombat`) does nothing useful — the real boot is only
  `python -m wombat` (wombat.__main__ → runtime.serve). Dead/misleading wiring; low impact
  because README consistently uses `python -m wombat`.
- **Adversarial evidence:** Verified in source, no repro needed. pyproject.toml line 50 declares
  [project.scripts] wombat = "wombat:main"; that target is src/wombat/__init__.py main(), a
  two-line stub — print("Hello from wombat!") then return. The real boot is
  src/wombat/__main__.py which does asyncio.run(serve()) from wombat.runtime. So an installed
  `wombat`/`uv run wombat` command only prints the greeting and never boots the runtime.
  README.md documents `python -m wombat` throughout (lines 16, 38, 86) and never the bare console
  command, so no operator flow depends on the entry point. Real dead/misleading wiring; severity
  minor is correct because the daily-use path (python -m wombat) is unaffected.
- **Repro:** in any synced venv run `uv run wombat` → prints "Hello from wombat!" instead of
  booting; contrast `python -m wombat` which calls runtime.serve().

---

## DOWNGRADED-MINOR findings

None. No finding was downgraded on adversarial re-verification; all admitted findings stand at
their claimed severity.

---

## Refuted claims

2 claims were refuted on adversarial verification and are not registered.
