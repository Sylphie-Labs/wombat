# Code review — 2026-07-09 (commits 531f392..946505c)

> **What this is:** a severity-ranked findings register from a targeted review of the
> commit range `531f392..946505c` (outbound/draft-approval track, dream/outcome loop,
> ingestion hardening, spine integration in `bootstrap.py`, contract-drift/test-honesty
> sampling), written so each finding can be routed into `planning/contract.yaml` by the
> architect-of-record. **This document does not modify the contract** — per operating
> rules, each S1/S2 finding below should become a governance entry (`open_question` →
> `decision`) and/or a ticket; S3–S5 items can be batched into hygiene tickets or
> explicitly deferred. Findings are numbered `CR2-n` to avoid colliding with the
> `2026-07-06` register's `CR-n` ids.
>
> **Method:** five parallel subsystem sweeps (outbound/send-paths, dream/outcome loop,
> ingestion hardening, spine integration, contract-drift/test-honesty) followed by an
> independent adversarial verification pass on every finding that survived — each
> finding below carries its own `verify_evidence`, and several severities were revised
> (one upgraded S3→S2, one downgraded S3→S4) during that pass. Every finding in this
> register **survived independent adversarial verification**; nothing below is
> first-pass-only. Baseline health at review time: 810 tests passed / 50 gated skips
> (pg- and live-gated), ruff clean, mypy strict clean on `src/` **and** `tests/`.

---

## S1 — Critical (breaks a constitution-level promise)

None found in this pass. (The two S1s from the 2026-07-06 register — pending-set replay
at boot and raw subject/sender reaching the mouth — are out of scope here; this pass
covers only `531f392..946505c`.)

---

## S2 — High (real defect, bounded blast radius)

### CR2-1 · Same-host residency guard is bypassed by a libpq `hostaddr=` DSN — data silently persists off-host

- **Where:** `src/wombat/safety/local_residency.py:81-104` (`_extract_host`); `:127-152`
  (`make_residency_check._check`).
- **Failure scenario:** operator (or a tampered config) sets
  `WOMBAT_PG_DSN=postgresql://localhost/wombat?hostaddr=8.8.8.8` (or the keyword form
  `host=localhost hostaddr=8.8.8.8 dbname=wombat`, or even bare `hostaddr=8.8.8.8
  dbname=wombat` with no `host=` at all). `runtime.serve()` calls `check_config(config)`
  (`runtime.py:135`), which extracts only the `host`/netloc component —
  `_extract_host` inspects `parsed.hostname`, the query key `host`, and keyword tokens
  starting with `host=`, but **never** `hostaddr` — so it sees `localhost` (or `None`,
  treated as a local unix socket) and **passes**. libpq/psycopg use `hostaddr` for the
  actual TCP connection (`host` is used only for auth/TLS SNI when both are present), so
  `WombatQueue` / `DailyLedger` / `PgPendingJournal` / `ActionTrailWriter` all connect to
  the remote address and every persisted user-model claim, journal row, action-trail row,
  and queue item lands off-host. The same parsing gap applies to
  `substrate.build_substrate`'s residency check on `pg_dsn`/`neo4j_uri`.
- **Why S2 (upgraded from an initial S3 read):** TK-150's AC3 is explicitly the
  "adversarial startup" (tampered/misconfigured DSN) threat model, and this is the *one*
  structural guard that is supposed to make CON-7/NG-7 non-bypassable. A documented libpq
  keyword defeats it outright and the guard fails open — the constitution-level residency
  promise is not actually structural for `hostaddr`-bearing configs. Held at S2 rather
  than S1 because the trigger requires a DSN that includes the `hostaddr` keyword, not a
  routine everyday misconfiguration.
- **Verification status — CONFIRMED, independently re-verified:** direct read of
  `_extract_host` confirms no `hostaddr` branch in any of the three DSN forms; when no
  `host=` token/hostname is present it returns `None`, which `_check` treats as an
  inherently-local unix socket. Executable repro (`local_addrs=['10.0.0.5']`):
  `postgresql://localhost/wombat?hostaddr=8.8.8.8` → extracted host `localhost` → PASS;
  `host=localhost hostaddr=8.8.8.8 dbname=wombat` → `localhost` → PASS; bare
  `hostaddr=8.8.8.8 dbname=wombat` → `None` → PASS; control
  `postgresql://8.8.8.8/wombat` → `8.8.8.8` → REFUSED (guard works when `host` itself is
  the remote address). Reachability confirmed: `runtime.py:135-136` residency-checks and
  then hands the *same* `config.wombat_pg_dsn` string to the real adapters. Not a recorded
  accepted risk — grep for `hostaddr` across `planning/`, `src/`, `tests/` returns zero
  hits; Q-25/TK-150 discuss same-host resolution of the `host` component only;
  `tests/safety/test_local_residency.py:92-141` has no `hostaddr` case, so AC3's "off-host
  Postgres URL is refused" claim is unpinned against this vector.
- **Proposed disposition:** extend `_extract_host` to also surface `hostaddr` (query key
  and `hostaddr=` keyword token) and residency-check it; when both `host` and `hostaddr`
  are present, check `hostaddr` (the actual connection target). Add a `hostaddr`-bearing
  case to the AC1/AC3 test tables.

### CR2-2 · Draft approval is structurally broken in the standing runtime — the human's "approve" is read as `None` and the sole drain run wedges RUNNING

- **Where:** `src/wombat/bootstrap.py:690` (`draft_ask_step_index =
  pre_dispatch_stages.index(draft_composer_stage)`), consumed at
  `src/wombat/stages/draft_dispatch.py:85` (`ctx.read_human_input(self._ask_step_index)`).
- **Failure scenario:** the drain is one long-lived run — `runtime.py` does a single
  `engine.run` on `wombat.drain`, then `Sweeper.run_forever` re-drives it via
  `fire_timer`. Whenever the queue is empty, `DrainQueueStage` returns
  `Wait(to='drain_queue')` and the run parks; each Sweeper poll replays the committed
  Waits and runs `drain_queue` fresh at an ever-higher `step_index` (the engine's `_drive`
  `seq` accumulates across the self-Wait loop). Once a HIGH-triage reply enqueues a DRAFT
  item, it drains at `seq=N` (`N>=1` even for the very first real item, since the boot
  drive already parked once), so `gate(N+1)→review_or_speak(N+2)→compose_dispatch(N+3)→
  draft_composer` parks its `AwaitHuman` at `seq=N+4`, **not** the hardcoded `4`.
  `DraftComposer` has already created the Gmail draft and journaled a PENDING trail row
  before this park. The operator approves via `provide_human_input`, which records the
  answer at `last_step.step_index` (`=N+4`) and re-drives; `DraftDispatchStage` then calls
  `read_human_input(4)` → `None` (nothing was committed at index 4) → `decision=None` → it
  writes a BLOCKED `record_refusal` trail row and raises `MissingApprovalAnswer`. Every
  real draft approval fails: the draft is orphaned in Gmail Drafts, the trail says
  blocked/refused instead of dispatched, and the raising resume leaves the single drain
  run stranded `RUNNING` with no re-park/timer, **halting all further draining** —not
  just the outbound path. never-send is still upheld (draft_dispatch dispatches zero
  capabilities on every path).
- **Why S2:** safety is structurally intact (no send ever fires), so this stays below
  S1 — but it is a functional/liveness break of the entire outbound draft-approval
  completion path *and* it wedges the one standing drain run, halting all further
  draining until restart. Real defect, bounded to the outbound feature + drain liveness,
  no constitution-safety breach.
- **Verification status — CONFIRMED, independently reproduced end-to-end:** an
  independent script using the real `DrainQueueStage` + real `DraftDispatchStage` over a
  real cog-worx `Engine`/`InMemoryJournal`, mirroring bootstrap's exact 5-stage prefix
  (composer at graph index 4, `ask_step_index=4`): after 1 committed boot Wait + 3 idle
  `fire_timer` polls, an enqueued DRAFT item drained on the next poll parked
  `draft_composer`'s `AwaitHuman` at `step_index=8`, not 4. `provide_human_input`
  recorded the answer at step 8; the resume drive's `draft_dispatch` called
  `read_human_input(4)` → `None` → `record_refusal('drain-run:draft_composer')` +
  raised `MissingApprovalAnswer`. The run was left `RUNNING` (stranded), `refusals=
  ['drain-run:draft_composer']`, `dispatched=[]`. Not masked by any existing test: the
  only coverage (`tests/integration/test_outbound_wiring_e2e.py:362-385`) drives a
  single **fresh** `engine.run` over a pre-loaded queue, so the item drains at `seq=0`
  and the park lands at `step_index==4` by construction — a genuine blind spot that
  never exercises idle-Wait accumulation. Not recorded: `bootstrap.py:680-682`'s own
  comment asserts the false premise that "the positional index draft_composer lands at
  in a fresh single-item drive is the step_index the engine records the human's answer
  under" — true only for a fresh run, never for steady-state operation.
- **Proposed disposition:** do not compute `ask_step_index` from static graph position.
  Either make `DraftDispatchStage` locate the park by stage identity rather than a fixed
  index (read the human answer keyed to the `AwaitHuman`'s own committed step, or have
  the engine expose "the awaiting step's index" to the resumed stage), or key the
  human-input read off the run's last `AWAITING_HUMAN` step. Add an e2e test that idles
  the drain (≥1 Sweeper Wait cycle) *before* enqueuing the draft, then approves, and
  asserts the trail row is DISPATCHED.

### CR2-3 · Dream consolidation `BudgetGuard` is process-cumulative, not per-night — model inference silently dies for the process lifetime after ~20 calls / $0.10

- **Where:** `src/wombat/pathways/dream_substrate.py:86`.
- **Failure scenario:** `build_dream_substrate` constructs **one**
  `BudgetGuard(max_usd=0.10, max_calls=20)` and bakes it into the assembled model via
  `build_model(spec, guard=guard)`. `bootstrap.assemble_runtime` calls
  `build_dream_substrate` exactly once at boot; the resulting model is handed to both
  `ClaimExtractor` and, via `ModelConsistencyOracle`, the `CoherenceReconciler`. The
  dream `StageGraph` and these stage instances are registered once and re-driven every
  night. `BudgetGuard` is a stateful one-shot object with no reset method, and this
  bypasses the cog-worx engine seam (`BudgetPolicy.new_guard()`) that mints a fresh
  guard per drive. `_calls`/`_spent_usd` accumulate across **all** nightly runs. Once
  cumulative calls reach 20 (or spend reaches $0.10) — reachable within one busy night
  or over a few nights — `check()` raises `BudgetExceededError` on every subsequent call
  forever: the extractor's raise is swallowed as a "STALL" and the reconciler oracle's
  raise is swallowed as `subjects_failed`. Nightly consolidation permanently degrades to
  a silent no-op (no claim extraction, no reconciliation adjudication) until the process
  restarts. This contradicts both the as-built docstring's claim of a "per-drive-segment"
  ceiling and `wombat_params.yaml`'s explicit "per-drive-segment" labeling of these
  ceilings.
- **Why S2:** the dream consolidation sweep is off-path (never touches the
  gate/brief/send/residency path), so no constitution-level promise breaks — correctly
  below S1. But it is a real defect with bounded blast radius (the dream
  consolidation/user-model-learning subsystem only), reachable in normal long-running
  operation (20 cumulative calls across a standing single-user nightly process is
  reached within days, or one busy night), producing permanent silent-to-user
  degradation until restart.
- **Verification status — CONFIRMED, independently reproduced:** structural read
  confirms a single guard baked once (`dream_substrate.py:86-89`), injected into both
  collaborators, held across nightly re-drives (`bootstrap.py:709-745`). `BudgetGuard`
  confirmed one-shot with no reset (`cost/budget.py:28-79`); cog-worx's own
  `registry.py` docstring states a baked guard is *not* the production path.
  `wombat_params.yaml:107,110` explicitly labels these "the per-drive-segment USD
  ceiling" / "per-drive-segment call-count ceiling" — the as-built wiring violates its
  own documented semantics. Failure-swallowing confirmed at
  `dream_pathway.py:169-180` (extractor) and `reconciler.py:104-124` (oracle never
  raises; failure counted in `stats.subjects_failed`) — no user-facing signal beyond a
  server log. Executable repro against the real `build_dream_substrate` +
  `load_operating_params` + a canned client: exactly 20 completes succeed, call #21 is
  refused with `"call ceiling reached: 20 of 20 calls used"`, and a subsequent
  simulated "next night" call is **still** refused (no per-night reset). Not a recorded
  risk: the cog-worx CF-3.0-B deferral concerns cumulative-across-*resume* ceilings
  within one run (intended reset is per-segment) — the opposite of this bug (no
  per-drive reset at all); `test_dream_substrate.py` only pins an already-exhausted
  guard raising pre-network, never reuse across drives.
- **Proposed disposition:** provision the dream model with a per-drive guard the way the
  engine does (rebuild the budget-guarded model, or re-arm/replace the guard, at the
  start of each dream run) so the DEC-23 ceiling is per-night as documented; or, if a
  lifetime cap is genuinely intended, correct the docstring/params comments and the
  contract to say so and size the ceiling for a standing multi-week process. Add a test
  that drives two successive dream runs and asserts the second still gets a fresh
  budget.

---

## S3 — Medium (correctness edges, currently fenced or low-frequency)

### CR2-4 · Re-adding an already-held item at capacity silently evicts a DIFFERENT held item

- **Where:** `src/wombat/gate/pending_set.py:152` (`add`), with the redelivery driver at
  `src/wombat/gate/pipeline.py:174` and the ack at
  `src/wombat/stages/review_or_speak.py:161`.
- **Failure scenario:** pending set is full (`max_pending=100`). The last item added, `c`,
  has its `PendingSetAdd` committed to the pg journal but the process crashes before it
  is acked. On restart, `rebuild_from_journal` restores all 100 items including `c`
  (correct). The at-least-once queue then redelivers the unacked `c`; `Gate.pipeline`
  scores it and calls `pending_set.add(c)` again — `pipeline.py:174` has **no membership
  check**. Because `len(_items)==max_pending`, `add()` enters the eviction branch and
  treats already-held `c` as a brand-new contender: `evicted=_lowest_urgency(_items)` is
  some **other** item `b` (`c` is not the minimum), `c.urgency>b.urgency`, so it journals
  `Remove(b)+Add(c)`, deletes `b`, and overwrites the already-present `c`. Net: `b` — a
  durably-committed held notification the user should still receive — is silently lost,
  and the set shrinks to 99. This violates the module's own promise that no committed
  mutation is ever lost, and `pipeline.py:16`'s claim that this path "re-adds
  idempotently" (true only in the non-full branch).
- **Verification status — CONFIRMED, independently reproduced with real code:**
  deterministic repro (`uv run`, `InMemoryPendingJournal`): committed `{a(u=5), b(u=1),
  c(u=3)}` into a cap-3 `PendingSet` (c last, set at capacity). Rebuild from journal →
  `{a,b,c}` (correct). Re-ran `add(c)` (simulating the at-least-once redelivery of the
  unacked source item): hit the at-capacity branch, computed `evicted` over a set that
  already contains `c`, found `b` (`b != c`), `c.urgency(3) > b.urgency(1)`, journaled
  `Remove(b)+Add(c)`, deleted `b`, overwrote already-present `c`. Result: `size 2`, `b`
  permanently lost. Redelivery path confirmed real end-to-end: `WombatQueue.drain`
  reclaims rows leased by a dead epoch on restart (at-least-once); the write-ahead add
  runs in `GateStage.run` while the ack runs in the later `ReviewOrSpeakStage.run`, so a
  crash in that inter-stage window leaves add committed + row unacked → restart
  redelivers → `Gate.pipeline` calls `pending_set.add(scored)` unconditionally. No
  membership guard in `add()`'s at-capacity branch and no dedup in `drain_queue.py`,
  `gate_stage.py`, or `pipeline.py` (grep + read confirmed). Not a recorded accepted
  risk — the contract asserts the *opposite*: TK-27's complexity_budget point (8)
  (Q-51/52 custody switchover) states this exact crash-between-add-and-ack redelivery
  "collapses to a harmless idempotent re-add," which is true only in `add()`'s
  non-full branch; at capacity it is false. Q-45's recorded crash reasoning covers only
  the mid-eviction abort (a different case), not this add-committed-then-redeliver case.
- **Proposed disposition:** add an idempotent-membership guard at the top of
  `PendingSet.add`: if `item.item_id in self._items`, refresh in place (optionally
  re-journal an `Add` to update urgency/added_at) and return `None`, **before** the
  capacity/eviction logic — so a redelivered already-held item can never displace another
  held item.

### CR2-5 · A single non-UTF-8 byte in the feedback file permanently degrades the wired feedback source

- **Where:** `src/wombat/user_model/feedback_source.py:169` (`_poll_file` /
  `read_text`).
- **Failure scenario:** `FeedbackInputSource` is wired live whenever
  `WOMBAT_FEEDBACK_FILE` is set. `_poll_file` does
  `self._feedback_file.read_text(encoding="utf-8")` on the whole file with strict
  decoding. One stray non-UTF-8 byte anywhere in the file raises
  `UnicodeDecodeError`, which propagates out of `poll()` (the registry catches it, logs,
  and marks the source degraded — but retries forever). Because the raise happens
  before `self._lines_read = len(lines)`, the offset never advances, so every
  subsequent poll re-reads the same file and re-raises: the feedback channel is
  permanently dead — every valid `"<item_ref> y"` line already in the file, and all
  future ones, are silently never absorbed. This directly violates the module's stated
  contract ("a malformed line is logged as a warning and skipped — it never raises")
  and CON-3 (poll() must never kill the source's loop). Additionally,
  `super().poll()` drains the `PushSource` buffer *before* `_poll_file()` runs, so any
  already-pushed event is lost when `_poll_file` raises.
- **Verification status — CONFIRMED, directly reproduced:** `feedback_source.py:169-171`
  confirmed: `read_text(encoding="utf-8").splitlines()` then `self._lines_read =
  len(lines)` assigned only *after* the read, so a strict-decode raise leaves the offset
  unadvanced. `registry.py:98-103` confirmed to retry the poll loop forever after
  catching and logging. Scratchpad repro (file = one valid line + a raw non-UTF-8 byte
  sequence + one more valid line, plus one pushed event): polls 0/1/2 all raised
  `UnicodeDecodeError` with `lines_read` stuck at 0 (permanent) and the buffer empty
  after the raise (the pushed event was drained by `super().poll()` then lost when
  `_poll_file` raised before returning). Both valid lines never emit. Wiring confirmed
  in scope for this review range (TK-51 + TK-176 both land in `531f392..HEAD`);
  `FeedbackInputSource` is live-registered whenever `WOMBAT_FEEDBACK_FILE` is set. No
  contract deferral for feedback-file encoding (grepped `utf-8`/`encoding`/
  `UnicodeDecode`/`byte` in contract.yaml — none). No test exercises a non-UTF-8 byte
  (`test_feedback_source.py` covers missing file and malformed lines only, both after a
  successful decode).
- **Proposed disposition:** decode tolerantly
  (`read_bytes().decode("utf-8", errors="replace")` or `read_text(errors="replace")`)
  so a bad byte becomes a malformed line that is warned+skipped per the stated
  contract; and advance `_lines_read` / capture drained push events before the file
  read (or wrap `_poll_file` so a failure cannot discard already-drained pushed
  events).

### CR2-6 · Residency guard admits a host that resolves to a local address among remote ones (multi-A-record / DNS-rebinding false accept)

- **Where:** `src/wombat/safety/local_residency.py:147`
  (`make_residency_check._check`, the `any(...)` over resolved addrs).
- **Failure scenario:** `check_config` feeds `wombat_pg_dsn` through `residency_check`
  as the structural CON-7/NG-7 same-host boundary. For a non-literal host, `_check`
  resolves it and passes if **any** resolved address is local:
  `any(_addr_is_local(addr, locals_now) for addr in resolved)`. A host that resolves to
  multiple A/AAAA records — e.g. `[127.0.0.1, 8.8.8.8]` (a multi-homed name, or an
  attacker/DNS-rebinding response that appends a loopback record) — therefore *passes*
  the guard even though the Postgres driver may actually connect to the remote
  `8.8.8.8`, sending local-residency-protected data off-host. The guard is meant to make
  off-host persistence structurally impossible; `any` lets a single co-resolved local
  record defeat it.
- **Verification status — CONFIRMED, independently reproduced:** direct read of
  `local_residency.py:147` confirms the hostname path passes on
  `any(_addr_is_local(...))`. Executable repro (`uv run`): resolver returning
  `["8.8.8.8","127.0.0.1"]` with `local_addrs=["127.0.0.1","::1"]` over
  `'postgresql://attacker-controlled.example.com/wombat'` → **ACCEPTED**; and
  remote-first `["203.0.113.9","127.0.0.1"]` → also **ACCEPTED**. Because
  psycopg/libpq try resolved addresses in order until one connects, a reachable remote
  among the co-resolved set can be dialed while the guard passed. Literal-IP,
  unix-socket, no-host, and localhost paths verified sound — only the multi-address
  resolver path is over-permissive. Not recorded: Q-25 and Q-87 only specify "resolves
  to same-host"; `test_local_residency.py:104-131` exercises single-address resolver
  tables only, never the mixed multi-A case; no deferral covers it.
- **Why S3 (not higher):** the DSN is operator config and the trigger needs an atypical
  multi-A/DNS-rebinding response, making this a correctness edge on a defense-in-depth
  guard rather than a routinely-reachable breach — held below CR2-1's S2 because that
  finding is a documented, single-keyword bypass (`hostaddr=`) reachable via ordinary
  config, whereas this one needs an adversarial or unusual DNS response.
- **Proposed disposition:** require **all** resolved addresses to be local
  (`resolved and all(_addr_is_local(...) for addr in resolved)`), refusing when the
  resolver returns any non-local address (or an empty list), so a co-resolved loopback
  record can't smuggle a remote endpoint past the boundary.

---

## S4 — Latent, currently fenced by composition (record so the fence is owned)

### CR2-7 · Feedback-file offset is a line COUNT, so a truncation/rotation silently drops a batch of lines

- **Where:** `src/wombat/user_model/feedback_source.py:170-171`
  (`new_lines = lines[self._lines_read:]; self._lines_read = len(lines)`).
- **Failure scenario:** offset tracking is by line count only, with no inode/size/
  identity check. If the feedback file is truncated or rotated (replaced with fewer
  lines than `_lines_read`) and new content is written before the next poll,
  `lines[self._lines_read:]` slices past the end and returns `[]` while `_lines_read`
  resets down to the new, smaller `len(lines)`. Lines written into the truncated region
  before that poll are never read — no y/n signal is ever emitted for them. No
  duplicates result, but a batch of genuine feedback is silently missed on any rotation.
- **Verification status:** taken on reviewer evidence (read-confirmed mechanism —
  `feedback_source.py:164-182` has no file-identity check, so it cannot distinguish
  "file grew" from "file was replaced"); verify with a rotation-specific test at fix
  time.
- **Proposed disposition:** track a byte offset plus a file-identity check
  (size/inode), and on detected shrinkage re-read from 0 (accepting bounded duplicates,
  which `FeedbackSignal`'s `event_key` idempotency already dedups) rather than silently
  resuming past the new tail.

### CR2-8 · Trail renderer sidecar is written non-atomically — a crash mid-write leaves a corrupt sidecar that then wedges every future render()

- **Where:** `src/wombat/trail/renderer.py:105` (`_save_sidecar`) and `:98-103`
  (`_load_sidecar`).
- **Failure scenario:** `_save_sidecar` opens the sidecar in `"w"` (truncate) mode and
  `json.dump()`s into it. A crash partway through the write leaves a truncated/corrupt
  JSON file (present, not absent). On the next `render()`, `_load_sidecar` does
  `json.load()` with no error handling, which raises `JSONDecodeError` and propagates
  out of `render()`, permanently breaking the human-audit log renderer on every
  subsequent pass until someone manually deletes the sidecar. This is harsher and
  undocumented compared to the documented, honest "lost sidecar → re-render duplicates"
  mode (Q-89 ruling 2), which assumes an *absent* sidecar, not a corrupt one.
- **Why S4:** the renderer is currently off-path (no daemon drives `render()` yet),
  which bounds impact.
- **Verification status:** taken on reviewer evidence (`renderer.py:98-107,139-143`
  read-confirmed: no try/except around `json.load`, truncate-write with no
  temp-file+rename); verify at fix time.
- **Proposed disposition:** write the sidecar atomically (dump to a temp file in the
  same dir, then `os.replace` onto the target), and/or guard `_load_sidecar` to treat an
  unparseable sidecar as empty (falling back to the documented duplicate-on-loss
  behavior) rather than raising.

### CR2-9 · Crash between `drafts.create` and the `AwaitHuman` step-commit re-creates the draft on cross-instance resume (documented cog-worx at-least-once side effect)

- **Where:** `src/wombat/integrations/gmail/draft_composer.py:245-275` (dispatch
  `DRAFT_CREATE_CAPABILITY`, then return `AwaitHuman` which the engine commits
  afterward).
- **Failure scenario:** `DraftComposer.run()` calls `ctx.dispatch(DRAFT_CREATE_CAPABILITY,
  ...)` (draft #1 created in Gmail) and then returns `AwaitHuman`; the engine commits
  that step **after** `run()` returns. If the process dies between the successful
  `drafts.create` and `commit_step`, the step is uncommitted; a subsequent crash-resume
  from a **different** Engine instance re-drives from entry, re-reaches
  `draft_composer` at the same `step_index` (existing step is `None`), re-runs
  `record_proposal` (ON CONFLICT no-op) and re-dispatches `drafts.create` → draft #2.
  `gmail.drafts.create` is not idempotent.
- **Verification status — taken as CONFIRMED-but-accepted on reviewer evidence:**
  `record_proposal` (line 237-243) precedes `dispatch` (line 245-249), so the
  journal-before-side-effect discipline holds. The cog-worx `engine.py` module
  docstring (lines 45-48) explicitly documents this as an accepted at-least-once
  property: "a bare resume of a still-RUNNING run from a DIFFERENT Engine instance …
  can double-execute an uncommitted stage's SIDE EFFECT … Single-flight cross-instance
  crash-resume is the operator's responsibility." Within one Engine instance the
  `_driving` mutex excludes double-execution, and wombat is single-process (ASMP-2), so
  this is fenced.
- **Proposed disposition:** accept as a fenced, documented cog-worx property for
  single-process v1 (matches the engine's stated at-least-once side-effect contract).
  Revisit if/when a run-lease/reaper lands. No wombat-side change required now, but
  worth a one-line note in `draft_composer`'s taint-order docstring, which currently
  asserts "the ONE `drafts.create` call already happened" without acknowledging the
  uncommitted-step re-run window.

### CR2-10 · `ActionTrailWriter`'s Postgres connection is opened in `assemble_runtime` but never closed at shutdown — reintroduces the leak TK-173/CR-15 removed for `DailyLedger`

- **Where:** `src/wombat/bootstrap.py:657` (`action_trail_writer =
  ActionTrailWriter(dsn)`); teardown at `src/wombat/runtime.py:115-119`.
- **Failure scenario:** when Google creds + a stored Gmail token are present,
  `assemble_runtime` constructs one `ActionTrailWriter(dsn)` shared by
  `draft_composer` and `draft_dispatch`. `ActionTrailWriter` opens a lazy psycopg
  connection on first proposal write and exposes `close()`. But it is not a field on
  `RuntimeBundle` and `runtime._drive_and_serve`'s `finally` closes only `queue`,
  `daily_ledger`, and `pending_journal` — so the trail connection is never closed on
  cooperative shutdown/`KeyboardInterrupt`. This is precisely the leak TK-173/CR-15
  went out of its way to avoid by sharing the one `DailyLedger`. Bounded: the process is
  exiting, so the OS reclaims the socket; impact is a not-cleanly-closed pg connection
  at shutdown, not a runtime leak.
- **Verification status:** taken on reviewer evidence
  (`trail/writer.py:73-86` lazy `self._conn` + `close()`; `bootstrap.py:636-697`
  constructs the writer, absent from the `RuntimeBundle` dataclass and its return;
  `runtime.py:115-119` closes only `queue`/`daily_ledger`/`pending_journal`; the AC2
  e2e test's own teardown likewise never closes the writer); verify at fix time.
- **Proposed disposition:** expose `action_trail_writer` on `RuntimeBundle` (Optional)
  and close it in `runtime.py`'s `finally` alongside the other adapters, or route it
  through the same shared-connection discipline used for `DailyLedger`.

### CR2-11 · `RatingTuner` clamp band `[0.35,0.65]` excludes 3 of 5 default rating params, so the first nightly tune snaps them into band on the very first tune regardless of the outcome signal

- **Where:** `src/wombat/rating/rating_tuner.py:161`.
- **Failure scenario:** `tune()` computes `updated =
  current.with_updates(urgency_base=_clamp(current.urgency_base + delta, 0.35, 0.65),
  load_base=_clamp(current.load_base - delta, 0.35, 0.65))`. On the first tuning night
  for a class, `current` is `default_params_for(class)` because no `rating_params`
  claim exists yet. But three of the five documented defaults
  (`CALENDAR_CONFLICT.urgency_base=0.7`, `MORNING_BRIEF.load_base=0.3`,
  `REFLECTION.urgency_base=0.3`) lie outside the locked `[0.35,0.65]` band. For any
  non-empty outcome corpus the clamp yanks these to the nearest band edge no matter the
  signal direction. Reproduced: `CALENDAR_CONFLICT` with an all-load-bearing corpus
  (should keep urgency high) is pulled *down* from 0.7 to 0.65; `REFLECTION` with an
  all-ignored corpus (should lower urgency) is pulled *up* from 0.3 to 0.35 — a sign
  inversion on the raw delta because the starting value is already below the floor.
- **Verification status — CONFIRMED but severity DOWNGRADED S3→S4 on adversarial
  verification:** the mechanism is real and independently reproduced (repro against the
  real `RatingTuner` + `InMemoryEntityKG`, exact numbers matched). But the "sign
  inversion / value moved opposite to the outcome signal" framing overstates the defect:
  `0.35`/`0.65` are the *locked* min/max `urgency_base`, chosen jointly with surfacing
  sensitivity so every in-band value respects the 12/day surfacing-rate ceiling
  (contract.yaml:1058-1065, 1603). A seed of 0.7 (or 0.3) is by construction outside the
  tuner's legal operating region — the clamp exists precisely to bring such values
  in-band, arguably correctly enforcing its rate-bound invariant. The genuine,
  defensible defect is narrower: the TK-41 per-class defaults were never reconciled
  against the TK-48-locked clamp band, so the designed per-class differentiation
  (`CALENDAR_CONFLICT` elevated to 0.7, `REFLECTION` muted to 0.3) is discarded on the
  first tune with any non-empty corpus. Impact is bounded and one-time: magnitude
  ≤0.05, fires only on the first tune night per class, and thereafter the value is
  in-band and adapts normally. Not recorded: contract.yaml:796 only acknowledges
  defaults sitting outside the band for the TK-27 scoring-monotonicity proof, not the
  tuner snapping them on first tune.
- **Proposed disposition:** reconcile the TK-41 defaults with the TK-48 clamp band:
  either bring the three out-of-band defaults inside `[0.35,0.65]`, or make the tuner
  clamp only the *delta* (not snap the base into band) so an out-of-band default is
  preserved until outcomes actually move it, or record a decision accepting the
  one-time snap-to-band. At minimum add a test asserting the first-night tune direction
  matches the corpus sign for every event class.

---

## S5 — Hygiene

None found in this pass.

---

## Suggested routing (for the architect)

| Finding | Proposed home |
|---|---|
| CR2-1 | New P1 ticket (extend `_extract_host` to cover `hostaddr`) + AC1/AC3 test cases; consider a governance note on TK-150's threat-model completeness |
| CR2-2 | New P1 ticket — key `read_human_input` off the run's actual `AWAITING_HUMAN` step, not static graph position; new e2e test that idles the drain before the approval |
| CR2-3 | New P2 ticket — mint a fresh `BudgetGuard` per dream drive; add a two-successive-drives test |
| CR2-4 | P2 fix ticket — idempotent-membership guard at the top of `PendingSet.add`, before eviction logic |
| CR2-5 | P2 fix ticket — tolerant decode in `_poll_file` + protect drained-but-not-yet-returned push events |
| CR2-6 | P2 fix ticket — `all()` instead of `any()` over resolved addresses (with empty-resolution-refuses fix) |
| CR2-7, CR2-8, CR2-10 | Batch into one P3 hygiene ticket (offset/identity tracking, atomic sidecar write, close-on-shutdown) |
| CR2-9 | Accept-and-record: document the at-least-once re-create window in `draft_composer`'s docstring; no code change |
| CR2-11 | Architect ruling: reconcile TK-41 defaults with the TK-48 clamp band, or record the one-time snap-to-band as accepted; add first-night-direction test either way |

**What was NOT found** (scoped out as non-concerns after review — see appendix below for
the full list): never-send remains structural (`gmail.messages.send` is registered
nowhere; `DraftDispatchStage` dispatches zero capabilities on every path); CON-4
journal-before-side-effect holds on the draft path; the approval gate cannot be bypassed
by a malformed/absent/duplicate human answer; `ReplyIntent` stays structurally clean of
body-derived free text; the once-per-night dream fence is sound (two independent
idempotency layers, no fence/run_id skew); feedback cannot leak into the gate or brief
pathway; the motive-free discipline (CON-6/NG-1) holds; `claims_about` ordering is
correctly newest-first; `PgPendingJournal` append/replay and the Remove-before-Add
eviction ordering (aside from CR2-4's re-add case) are sound.

---

## Appendix A — Raised and refuted (so the work isn't redone)

- **`reply:<message_id>` idempotency defeated by ack — same urgent message re-drafts on
  every poll for the whole 24h lookback window.** Where: `src/wombat/queue.py:207` (ack
  DELETE) + `src/wombat/integrations/gmail/poller.py:203-206` (fixed 24h window, no
  cursor) + `src/wombat/sources/bootstrap.py:203-219` (`GmailWithReplyIntents` re-emits
  `reply:<id>` every poll). **Why refuted:** the ack-defeats-dedup *mechanism* is real
  (queue ack is a DELETE; enqueue's `ON CONFLICT` only guards live rows; the registry
  keeps no persistent seen-set; the poller re-emits `reply:<id>` every poll within its
  fixed 24h window) — but on independent verification the finding did not establish
  that this actually causes the same reply intent to be re-*drafted*: `DraftComposer`'s
  `action_id` is keyed on the reply-intent identity (`draft_composer.py:233`), and
  further tracing showed the redraft claim was not substantiated end-to-end. Do not
  re-raise the "re-drafts on every poll" framing without re-deriving the missing link;
  the underlying ack/dedup mechanism observations above remain accurate if re-used for a
  narrower claim.

## Appendix B — Examined and sound

- Never-send is structural: `gmail.messages.send` is registered nowhere in the codebase
  (grep-confirmed); `DraftDispatchStage.run` dispatches zero capabilities on every path
  (approve → `mark_dispatched` only; reject → `mark_cancelled` only; malformed/absent
  decision → `record_refusal` + raise). The only outbound Gmail capability is
  `drafts.create` (external tier, no trusted-output tag).
- CON-4 journal-before-side-effect holds on the draft path:
  `DraftComposer.record_proposal` executes before `ctx.dispatch(drafts.create)`; a
  `TierViolation` on dispatch records a refusal and re-raises without a side effect.
- `ask_step_index` is computed from the actual `pre_dispatch_stages` list, never
  hardcoded in isolation — the defect (CR2-2) is that the *live* step position drifts
  from the static graph position during steady-state idle-Wait accumulation, not that
  the index is literally hardcoded.
- Approval-gate cannot be bypassed by a malformed/absent/duplicate human answer: an
  answer whose data lacks a valid `decision` triggers `record_refusal` + raise (loud,
  zero dispatch); duplicate `provide_human_input` calls are first-answer-wins/idempotent
  in the engine adapter.
- `ReplyIntent` is structurally clean of body-derived free text beyond a bounded
  excerpt: no `body_text` field; `recipient` is `item.sender` (a header, never parsed
  from the body); `quoted_excerpt` is control-character-stripped and truncated to
  `EXCERPT_MAX_CHARS=280`.
- Loud-skip outbound wiring is all-or-nothing with no partial state: the outbound
  capability/DRAFT-route/dispatch-edge gate all check the same
  `_has_google_client_credentials` + token-store condition, so reply-intent emission
  and draft composition are enabled together; when unwired the drain graph is
  byte-identical to the 5-stage baseline.
- Residency/egress boundary is consistent: `local_residency.check_config`
  residency-checks only `wombat_pg_dsn` and deliberately exempts `deepseek_base_url`
  (ASMP-1); the Gmail API host is not a persistence endpoint and is out of scope for the
  storage-residency guard.
- The reply item's queue key is distinct from the parent message's key
  (`event_key='reply:'+message_id` vs `message_id`), and `idempotency_key`
  length-prefixes the source id so the two never collide.
- Once-per-night dream fence (`DreamTimerStage`/`DreamRunLedger`) mirrors the reviewed
  brief timer: two independent idempotency layers (durable `dream:run` row on the
  shared `DailyLedger`, plus a night-keyed `run_id` engine double-drive guard). Dream
  fires at 02:00 local, brief at 07:00 local, both resolve to the same civil
  `wombat_date` — no fence/run_id skew, no cross-boundary skip.
- Feedback cannot leak into gate/brief: `GateStage` diverts `kind=='feedback'` items
  before building gate items/decision/entries, regardless of `absorb_feedback` success;
  `BriefGatherStage` never drains the queue, so feedback cannot reach it either.
- Outcome claim keying/enumeration, motive-free discipline (CON-6/NG-1), tuner
  division-by-zero/empty-corpus handling, in-memory KG reset residuals, `claims_about`
  newest-first ordering, `PgPendingJournal` append/replay fidelity, boot replay wiring
  in `assemble_runtime`, and `local_residency`'s handling of literal IPv4/IPv6,
  loopback ranges, unix-socket/no-host DSNs, `host=` keyword DSNs, and userinfo tricks
  were all examined directly and found sound.
