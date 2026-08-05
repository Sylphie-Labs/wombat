# 04 — Google ingestion, NARROWED to the draft-reply hold (TK-366, SWEEP 04)

Run date: 2026-08-05, one continuous live session per `protocol.md`, one sweep at
a time per DEC-86. Class A throughout.

**This sweep was narrowed by DEC-88 before it ran.** Read that ruling first — it
records why, on whose evidence, and what it costs. The short version: Jim
attested from daily use that Google ingestion works, TK-365 had already
corroborated the input half incidentally, and the one check nothing on this host
had ever exercised was kept.

## Checks DROPPED by the re-scope — named individually, never inferred

Per TK-366's second acceptance criterion. **Evidence for all four: operator
attestation by Jim, 2026-08-05**, verbatim — *"we dont need to test 2. It
already works fine. I can attest to that."*

| dropped check | what it would have proven | corroboration on record |
|---|---|---|
| Google connections panel tells the truth on both rows | a displayed status matches the real stored credential state | **none — genuinely uncovered.** If the panel lies (the ISS-16 shape, "Connected" against a dead credential) this phase will not catch it. Stated, not hidden. |
| Reconnect through the real consent screen | the operator recovery path from the incident MANUAL_TEST.md was written for | none — uncovered, and it needs an operator anyway |
| Restart-to-rewire requirement | that skipping the restart leaves ingestion dead while the row reads Connected | none — uncovered |
| Poll lands gcal + gmail rows with fresh `first_seen_at`; gmail projection carries no `body_text` | ingestion actually ingests, and DEC-45/DEC-26 hold at the table | **substantially covered by TK-365**: 637 gmail rows in the real store, the five most recent matching the provider exactly by subject and sender, both tokens refreshing non-interactively at boot with no `not wired` and no `invalid_grant`, and the gmail payload keys observed directly as exactly `['message_id','priority_band','received_at','sender','subject']` — no body key |
| Conflict detection with deterministic alternatives | two overlapping events produce a conflict and no write reaches Calendar | none — uncovered |

## AC1 — the draft reply is created and HELD, never sent

**FAIL.** Two independent defects, both routed. Nothing was sent, and no draft
exists.

### Getting there took a repair first (ISS-55 → TK-378)

The first two attempts never reached the draft path at all. A draft-kind item
was enqueued into the real `wombat_queue` — a real `ReplyIntent` wire payload,
recipient set to **Jim's own address** so that a draft, if one were created,
could not reach a third party — scored to surface, and the gate **held** it:

```
gate: in-call detected — holding the immediate-voice arm
gate decision: item_id='tk366-draft-probe-1' item_kind='draft' action='hold'
```

Jim was not on a call. `SteelSeriesCVGameSense.exe` — a peripheral vendor's
always-on background service — held the only ACTIVE session on the capture
endpoint, which read as in-call and suppressed **every** surfacing arm. Routed
as **ISS-55** (with **ISS-56** for the log line that claimed a narrower hold
than it took). Jim ruled the product should change rather than his desktop, so
**TK-378** was minted under DEC-88 and built **outside this sweep** (protocol.md
(d): a sweep never repairs what it finds). After TK-378 landed, the live repro
inverted — `probe_in_call()` returns `False` with SteelSeries still running.

A third attempt was still held, this time **legitimately**: the machine had been
idle 8+ minutes while the sweep ran through scripts, so `presence_hold` held
surfacing exactly as designed. Recorded because it matters for reproducing this:
a real keystroke was issued to make presence ACTIVE (`idle_ms=13313`,
`surfacing_permitted=True`) before the final attempt. That is test setup, not a
product change.

### What then happened, in order

```
11:10:23,886 gate decision: item_id='tk366-draft-probe-4' item_kind='draft'
             action='surface_immediate' urgency=0.9885416666666667 load=0.525
11:10:23,888 compose dispatch: item_id='tk366-draft-probe-4' composer_name='draft_composer'
11:10:24,868 POST https://api.deepseek.com/v1/chat/completions "HTTP/1.1 200 OK"
11:10:25,709 CRITICAL __main__: wombat runtime terminating on unhandled exception
             requests.exceptions.HTTPError: 403 Client Error: Forbidden for url:
             https://gmail.googleapis.com/gmail/v1/users/me/drafts
```

Artifact: `logs/runtime-20260805-110610.log`.

**Clause by clause:**

| AC1 clause | result | evidence |
|---|---|---|
| a Gmail DRAFT exists | **FAIL** | `403 Forbidden` from the real `drafts.create` call; provider-side draft ids **29 before, 29 after, identical sets** |
| visible in the action trail BEFORE it was created | **PASS** | `action_trail_projection` gained its **first row ever**: `action_type='draft_email'`, `target='jctisdale1988@gmail.com'`, `proposed_at=15:10:25.564Z`, `status='pending'` — written *before* the create attempt, so the taint-order property TK-78 was designed around genuinely holds |
| no message was sent | **PASS** | `in:sent newer_than:1d` → **0 messages**, checked at the provider with wombat's own credential |
| the AwaitHuman approval step genuinely blocked | **NOT REACHED** | the runtime died before the park; unproven either way, and named here rather than counted as a pass |

### ISS-57 (CRITICAL) — the path cannot work on any host

`GMAIL_SCOPES` is pinned to `gmail.readonly` **only**, and
`assert_gmail_readonly_scopes` raises on any credential carrying more —
including `gmail.compose`. The `drafts.create` capability is built from that
same readonly-guarded session. A readonly token cannot create a draft, so the
capability 403s **by construction**. This is not an expired token or a skipped
consent: it is a credential forbidden from doing the thing the capability exists
to do.

The pin is deliberate and was correct for its era — its own comment says a
send-capable token must not exist on this host *before TK-78's review-before-send
machinery exists*. TK-78 has since landed; nothing widened the scope. Nothing
could catch it: `draft_composer.py`'s docstring says the live path is exercised
only by an operator smoke test, and every test in that module dispatches a
**fake** capability. The operator smoke was evidently never run. `action_trail_
projection` holding zero rows before today is the corroboration — the path had
never executed once in production.

**This must not be "fixed" by widening the scope on reflex.** `gmail.compose` is
the narrowest scope that can create a draft and it also permits *sending* — the
exact thing CON-5/NG-5 exist to prevent. Retiring the feature is an honest option
alongside widening it. Architect's call.

### ISS-58 (CRITICAL) — and the error kills the runtime

Independent of *why* the call failed: the exception propagated out of
`DraftComposer.run` → `ctx.dispatch` → the cogworx engine → `_run_drain_pump` →
`serve()`, and the process died. `draft_composer.py` guards its **mouth** call
(degrades to a template body) and leaves the **capability dispatch** immediately
after it unguarded — so any provider-side failure on that one call is fatal to
the whole process: chat, the LAN listener, the settings API and the drain pump
all share it.

Blast radius as observed was one crash, not a loop: the watchdog respawned in
~7s (`runtime-20260805-111032.log`, DEC-52b working), and the respawned boot did
not re-crash because the queue row had already been ack-consumed before the
dispatch. That is ordering luck, not a guarantee — a producer that re-emits the
same draft item on re-poll would re-enter the same dispatch every boot, which is
the ISS-15 shape.

## Provider state after the sweep

Checked with wombat's own read-only credentials, before and after:

- Gmail drafts: **29 → 29, byte-identical id sets**. No draft created.
- Sent in the last day: **0 → 0**. Nothing sent, at any point.
- Calendar events today + 7 days: **identical**, no write of any kind.

## Findings routed

| id | severity | what |
|---|---|---|
| **ISS-57** | CRITICAL | `drafts.create` 403s by construction — the credential is readonly by design and the scope guard forbids anything broader. The draft-reply feature has never worked and cannot. |
| **ISS-58** | CRITICAL | An HTTP error on that capability dispatch is unhandled and **terminates the runtime**. Any transient provider failure on that call kills the whole product. |
| ISS-55 | high | A background service holding the mic reads as in-call and suppresses all surfacing. **REPAIRED by TK-378** (below) and re-verified live. |
| ISS-56 | minor | The in-call log line understated what it holds. **REPAIRED by TK-378.** |

## What this sweep did NOT cover

- Everything in the dropped-checks table above.
- Whether `AwaitHuman` genuinely blocks — unreachable until ISS-57/ISS-58 are
  ruled on. It is the remaining unproven half of CON-5 and should be re-driven
  by whatever ticket fixes them, not assumed.
- Whether a real non-NORMAL-band email produces a draft item at all: every
  recent message triages NORMAL, so the production producer has never emitted
  one. This sweep drove the item directly, exactly as TK-364 drove the queue.

## State this sweep consumed

Four synthetic draft-kind items (`tk366-draft-probe-1..4`); three were held and
sit in the pending set, one surfaced and produced the single `pending`
action-trail row above. One real DeepSeek call. **No Gmail or Calendar state
changed at the provider.** No daily ceiling or flush latch was spent. The
`pending` trail row is left in place rather than deleted — it is the evidence.

## Final state

The runtime is **UP** (watchdog-respawned, `runtime-20260805-111032.log`).
`.env` unmodified. Nothing was sent. No test, source, prompt, charter or persona
file was edited to make any observation come out — the one source change this
session (TK-378) is a separately-ticketed repair of a finding, built after it was
routed, and re-verified against the original repro.
