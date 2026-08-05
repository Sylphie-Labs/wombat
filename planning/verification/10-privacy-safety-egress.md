# 10 — Privacy, safety, the action trail and a real egress audit (TK-372, SWEEP 10)

Run date: 2026-08-05, per `protocol.md`, one sweep at a time per DEC-86. Class A
throughout — nothing here needed Jim.

**Headline: the egress audit is clean except for one host, and the two promises
that were supposed to be structural turn out to be conventions.** Wombat's own
code contains no analytics and the wire agrees — across the *entire* log history
only four external hosts have ever been contacted. But `huggingface.co` is one of
them, it is contacted unconditionally at every boot before any user action, and it
carries a User-Agent the upstream library itself calls telemetry. Separately, the
human-readable action trail **has never been rendered** (a fourth instance of the
built-but-never-run pattern), and the DEC-26 taint invariant is **not** enforced by
a structural guard — an adversarial re-bind is accepted and external survives the
latch.

| AC | Check | Class | Result |
|---|---|---|---|
| AC1 | trail read end to end; every side effect present, written BEFORE it happened | A | **FAIL** — ordering holds, but the human-readable trail is never rendered and failed actions read `pending` forever |
| AC2 | taint latch exercised adversarially incl. `taint_drops_external=false` re-bind | A | **FAIL** — the structural guard does not refuse; external survives the latch |
| AC3 | network-level egress enumeration across a full session | A | **FAIL on the assertion half, PASS on the method half** — the permitted set has four members and the wire has five (`huggingface.co`). *Corrected by the architect from the sweep's "PASS with one non-permitted destination" — see the ruling section below.* |
| AC4 | every high-consequence outbound enumerated deliberately and attempted | A | **PARTIAL** — enumeration complete; only one path is reachable and it 403s before the review gate |
| AC5 | no analytics/telemetry in app or runtime (DEC-29), corroborated by the wire | A | **FAIL (third-party)** — wombat's own code is clean; a transitive dependency emits boot telemetry with no opt-out |

---

## Session shape and what was driven

One continuous live session against the real product, the real `.env`, the real
Postgres (`wombat-runtime-db`, port 5436) and real Google credentials.

- **Runtime restarted under the sampler** at `19:23:17Z` via
  `scripts/restart-wombat.ps1`, deliberately, so **boot egress** was captured
  (launcher PID 7536, worker PID 46964). Boot log:
  `logs/runtime-20260805-152321.log`.
- **Chat** — 3 turns over `POST /chat`, the same loopback transport the pane uses
  (2.271 s / 3.066 s / 2.961 s), plus a 4th liveness turn at close.
- **Ingestion** — Gmail/Calendar polling ran throughout on its own interval;
  `screenpipe` context-switch items were gated live.
- **Voice** — driven to a real `surface_immediate` and a real Fish TTS call.
- **Dreaming** — tonight's fence had already fired (`dream:run = 1` for
  2026-08-05), so its egress is taken from **today's actual dream run**
  (`logs/runtime-20260805-091501.log:13-25`) rather than a forced re-fire; no
  ledger value was reset to manufacture one.
- **Brief** — likewise already fired (`brief:run = 1`); egress from the same log.
- **The Electron app was launched mid-session** (`19:38:52Z`) so the app's own
  processes were inside the audit scope, not just the runtime.

Two probe items were enqueued **through wombat's own `WombatQueue.enqueue` seam**
(not raw SQL) at `19:33:02Z`, following the TK-366 probe precedent:
`tk372-generic-probe-1` (generic, high urgency) and `tk372-draft-probe-1` (draft).
Both were gated, composed and dispatched by the live runtime.

---

## AC3 — the egress audit — **PASS with one non-permitted destination**

This is the check that had never been performed in any form. It is recorded here
in full so it is reproducible.

### Enumeration method (three independent methods, cross-checked)

**Scoping first, because this host makes naive capture useless.** This machine runs
thirteen unrelated containers (`sylphie-*`, `pkg-ai-neo4j`, `drift-detector-*`,
`memory-pkg-timescale`) plus the agent session itself. A whole-host capture would
drown the audit and manufacture false CRITICALs, so every method below is scoped to
**wombat's own process tree**.

*A scoping trap worth recording:* the first sampler filtered processes by
executable path (`...\wombat\.venv\...`) and **silently missed the only process
doing network I/O**. The runtime's worker child reports its image path as the
uv-managed base interpreter
(`...\uv\python\cpython-3.13.11-windows-x86_64-none\python.exe`), not the venv
shim. Path-based scoping produced an empty connection table that looked like a
clean result. The corrected rule scopes on **command line**, and any future egress
check must do the same:

- `python.exe` whose CommandLine contains `-m wombat` (runtime launcher + worker,
  and `wombat.settings_app` when the app spawns it)
- `powershell.exe` whose CommandLine contains the watchdog's own function name
- `electron.exe` / `node.exe` under the wombat `app` directory
- **plus every transitive descendant** of the above

All match fragments are built by string concatenation so the sampler's own command
line (which contains the repo path) cannot self-match — the TK-370 lesson.

**Method 1 — per-PID TCP sampler.** `Get-NetTCPConnection -OwningProcess <pid>` for
every in-scope PID, sampled every **200 ms** for ~36 minutes, PID set re-resolved
every 2 s so the restart and the app launch were both picked up. **6 541 endpoint
samples** across **19 distinct PIDs**.
Script: `scratchpad/egress-sampler2.ps1`.
Raw: `scratchpad/egress-run2/conns.csv` (578 KB),
`scratchpad/egress-run2/pids.csv` (118 KB).

**Method 2 — DNS cache correlation.** `Get-DnsClientCache` snapshotted every 5 s
throughout, giving the IP→hostname mapping at connect time rather than at analysis
time. Raw: `scratchpad/egress-run2/dns.csv` (960 KB). Forward resolution of the
four candidate hosts was used as a cross-check.

**Method 3 — wombat's own request log.** Every httpx call logs its full URL, so
`logs/*.log` is a per-request, URL-level egress record covering the **entire
history of the product on this machine**, not just the sampler window. This is the
method that makes the audit historical as well as live.

### The raw destination list

Every distinct remote endpoint contacted by a wombat-scoped process during the
session (loopback ports collapsed to one row):

| Remote address | Resolved host | Port | Proc / PID | Samples | First seen (UTC) | Classification |
|---|---|---|---|---|---|---|
| `127.0.0.1` | loopback — Postgres 5436, settings_app 3030, chat + internal ports 55234/55241/55248/57628/57629/58222/58223/60381/60382 | various | python 43352, 46964, 9020 | 4631 | 19:22:54 | **permitted** (loopback) |
| `3.173.21.63` | `api.deepseek.com` | 443 | python 46964 | 594 | 19:25:13 | **permitted** (configured model provider) |
| `2001:4860:4840:400::` | `www.googleapis.com` / `gmail.googleapis.com` | 443 | python 43352, 46964 | 373 | 19:22:54 | **permitted** (Google APIs) |
| `2001:4860:4841:400::` | `www.googleapis.com` / `gmail.googleapis.com` | 443 | python 46964 | 24 | 19:38:44 | **permitted** (Google APIs) |
| `2001:4860:4842:400::` | `www.googleapis.com` / `gmail.googleapis.com` | 443 | python 43352, 46964 | 248 | 19:22:54 | **permitted** (Google APIs) |
| `2001:4860:4843:400::` | `www.googleapis.com` / `gmail.googleapis.com` | 443 | python 46964 | 135 | 19:28:23 | **permitted** (Google APIs) |
| `2001:4860:4845:400::` | `www.googleapis.com` / `gmail.googleapis.com` | 443 | python 46964 | 106 | 19:28:29 | **permitted** (Google APIs) |
| `2607:f8b0:4002:c10::5f` | `oauth2.googleapis.com` | 443 | python 46964 | 53 | 19:38:30 | **permitted** (Google APIs) |
| `2606:4700::6812:64` | `api.fish.audio` | 443 | python 46964 | 2 | 19:33:08 | **permitted** (explicitly-selected cloud voice provider) |
| `2600:9000:28bc:7e00:17:b174:6d00:93a1` | `huggingface.co` | 443 | python 46964 | 363 | 19:23:21 | **NOT PERMITTED — routed** |
| `2600:9000:28bc:c200:17:b174:6d00:93a1` | `huggingface.co` (inferred) | 443 | python 43352 | 12 | 19:22:54 | **NOT PERMITTED — routed** |

The `c200` address fell out of the DNS cache before the first snapshot (it belonged
to the *pre-restart* runtime's boot, roughly two hours earlier). It is recorded as
`huggingface.co` on strong but inferential grounds: it shares the exact CloudFront
suffix pattern `2600:9000:28bc:XX00:17:b174:6d00:93a1` that `huggingface.co`
forward-resolves to across all eight of its AAAA records, and `huggingface.co` is
the only non-permitted host in the entire log history.

**Whole-history corroboration (method 3).** Across **every** `logs/runtime-*.log`
ever written on this machine, the complete set of hosts is four:

```
241  api.deepseek.com
146  api.fish.audio
 50  huggingface.co
  1  gmail.googleapis.com
```

(Google traffic is under-represented here only because `google-api-python-client`
uses `httplib2`/`requests` rather than httpx; the TCP sampler independently shows
sustained Google connections. The point of this table is what is *absent*.)

Exact URLs, which matter for the ruling below:

```
241  https://api.deepseek.com/v1/chat/completions
111  https://api.fish.audio/v1/tts
 35  https://api.fish.audio/v1/asr
 47  https://huggingface.co/api/models/Systran/faster-whisper-base/revision/main
  3  https://huggingface.co/api/agent-harnesses
```

**No analytics host. No telemetry beacon. No crash reporter. No unexplained
destination.** Three independent methods agree on that.

### The one non-permitted destination

`huggingface.co` is contacted **at every boot, unconditionally, before any user
action**, by the faster-whisper ASR model loader. Live evidence from this session's
own boot:

```
logs/runtime-20260805-152321.log:6
2026-08-05 15:23:21,814 INFO wombat.sources.asr: ASR inference pinned to device=cpu (DEC-59), model=base
2026-08-05 15:23:22,250 INFO httpx: HTTP Request: GET https://huggingface.co/api/models/Systran/faster-whisper-base/revision/main "HTTP/1.1 200 OK"
```

The sampler shows the matching TCP connection opening at `19:23:21.843` — and
**still `Established` three and a half minutes later** at `19:26:51`, i.e. the
connection is pooled and held open, not a one-shot fetch.

Note this fires even though the model is already cached locally: it is a *revision
check*, so the product phones a third party at every boot to ask whether its local
model is current. Per the sweep's instruction this is classified **needs-ruling**
rather than being adjudicated here, with the context recorded exactly. A literal
reading of AC3 ("the list contains ONLY ... and any other destination ... is a
CRITICAL finding") makes it CRITICAL; the architect owns which reading binds.

### Scope limits, stated rather than glossed

- A 200 ms sampler can in principle miss a connection shorter than one sample
  interval. This is why method 3 exists: wombat's own per-request URL log is
  complete by construction for everything routed through httpx, and it agrees.
- **No Electron/node process of the app was ever observed making a non-loopback
  connection** — no outbound row in 6 541 samples is attributed to any app process.
  That is a genuine (and good) result for the app, but it is an absence-of-evidence
  claim at 200 ms resolution rather than a packet-level proof.
- UDP was not enumerated per-PID; DNS resolution on Windows is performed by the
  system resolver service, not in-process, so wombat's own UDP surface is expected
  to be empty. Not proven here.

---

## AC1 — the action trail — **FAIL**

### What passes: write-before-act ordering genuinely holds

Verified live, on a real side effect, end to end. The draft probe surfaced and
dispatched:

```
logs/runtime-20260805-152321.log:25-28
15:33:28,746 gate decision: item_id='tk372-draft-probe-1' item_kind='draft' event_class='draft_reply' action='surface_immediate' urgency=1.0 load=0.6
15:33:28,749 compose dispatch: item_id='tk372-draft-probe-1' item_kind='draft' composer_name='draft_composer'
15:33:29,374 POST https://api.deepseek.com/v1/chat/completions 200 OK
15:33:30,375 ERROR draft_composer: gmail.drafts.create failed — NO draft was created ...
```

with the trail row written at `2026-08-05 19:33:30.251217+00` — **before** the
`403` at `15:33:30,375`. The source ordering is explicit
(`draft_composer.py`, comment verbatim): *"JOURNAL BEFORE ANY GMAIL CALL — a kill
between this write and the dispatch below still leaves the proposal row behind."*
The summary is genuinely plain language, no decoding needed to understand *what*
was proposed:

> `Draft a reply to jctisdale1988@gmail.com — Re: TK-372 privacy sweep draft probe: Acknowledged on TK-372. Egress remains dormant until further notice; no probes will send.`

### Defect 1 (CRITICAL) — the human-readable trail has never been rendered

CON-4's promise is a *human-readable* trail. That artifact is
`ActionTrailRenderer` — the Q-89 plain-language, append-only `wombat-trail.log`
that deliberately carries **no `action_id`s and no JSON** so a human can read it
without decoding. It is fully built and fully tested.

**It is never constructed and it has never run.**

- `grep -rn "ActionTrailRenderer\|wombat-trail.log\|\.render()" --include=*.py src/`
  → outside `trail/renderer.py` itself, the only hit anywhere in `src/` is a
  **docstring mention** in `trail/reader.py:10`.
- `bootstrap.py` wires the **writer** (`ActionTrailWriter(dsn)` at
  `bootstrap.py:1208`) and `runtime.py` closes it — but nothing anywhere
  constructs the **renderer**.
- The module says so itself: *"NO polling loop, NO daemon — a later consumer drives
  `render()` (Q-89 ruling 3)."* **There is no later consumer.**
- No `wombat-trail.log` exists anywhere: not in the repo, not in
  `C:\Users\Jim\wombat-data\` (which contains only `brief.md`,
  `chat-handshake.json`, `voice-drop/`).

So today the only way to read the action trail is a SQL query against
`action_trail_projection`. AC1's bar is "readable by a human without decoding".
`docker exec ... psql -c "SELECT ... FROM action_trail_projection"` is decoding.

This is the **fourth consecutive sweep** to find a done feature that has never once
executed in production — after ISS-64 (reflection never composed), ISS-66 (browser
never wired) and ISS-63 (ratings on a test double).

### Defect 2 (MAJOR) — a failed side effect is recorded as `pending` forever

When the Gmail call fails, `draft_composer` calls `record_refusal(...)` and logs
*"the proposal row is refused rather than parked for approval"*. But
`record_refusal` is an
`INSERT INTO action_trail_projection ... ON CONFLICT (action_id) DO NOTHING`
(`trail/writer.py:142-157`) — and `record_proposal` has **already inserted that
exact `action_id`** moments earlier. The refusal is therefore a **guaranteed silent
no-op** on the provider-failure path; it returns `ALREADY_PRESENT` and the row
keeps `status = 'pending'`.

Observable on live data — the table contains exactly **two rows in its entire
history**, both from 403'd attempts, and **both read `pending`**:

```
 seq | action_type |         target          |          proposed_at          | status
-----+-------------+-------------------------+-------------------------------+---------
   1 | draft_email | jctisdale1988@gmail.com | 2026-08-05 15:10:25.564225+00 | pending   <- TK-366's probe
   2 | draft_email | jctisdale1988@gmail.com | 2026-08-05 19:33:30.251217+00 | pending   <- this sweep's probe
```

The trail asserts that two drafts are sitting in Gmail awaiting Jim's approval.
Neither exists. For an audit surface whose entire purpose is telling a human what
the machine did, stating the opposite of the truth is the failure mode that matters
most. Note this also means the `blocked` status and the renderer's `[BLOCKED ...]`
line are unreachable on this path.

---

## AC2 — the taint latch, adversarially — **FAIL**

Run against the live installed code (the same `cogworx` and `wombat.safety`
modules PID 46964 has imported), via `scratchpad/taint_probe.py`. Four escalating
attacks:

| # | Attack | Result |
|---|---|---|
| A1 | construct `StageToolPolicy(taint_drops_external=False)` | **CONSTRUCTED — no refusal** |
| A2 | mutate the frozen `EXTERNAL_DISPATCH_POLICY` in place | **REFUSED** — pydantic `frozen_instance` ValidationError |
| A3 | re-bind the attacker policy onto a stage, then taint | **external SURVIVED the latch** |
| A4 | `ToolGate.bind_policy(attacker_policy)`, then taint | **ACCEPTED — external SURVIVED the latch** |

Verbatim from the probe:

```
A1: RESULT: CONSTRUCTED (no refusal) -> allowed_tiers=frozenset({'write','read','external'}) taint_drops_external=False
A3: sanctioned bind  -> taint_drops_external = True
    adversarial bind -> taint_drops_external = False
    before taint: effective_tiers = ['external', 'read', 'write']
    AFTER taint : effective_tiers = ['external', 'read', 'write']
    >>> external SURVIVED the latch: True
A4: sanctioned policy, tainted  -> ['read', 'write']
    bind_policy(attacker) ACCEPTED
    attacker policy, tainted    -> ['external', 'read', 'write']
    >>> external SURVIVED the latch: True
```

**The latch itself is correct.** Under the sanctioned policy, tainting drops
external exactly as DEC-26 requires (A4 line 1: `['read', 'write']`). The failure
is that **nothing structurally prevents a different policy from being bound.**
`StageToolPolicy` is a frozen pydantic model whose `taint_drops_external: bool =
True` has **no validator refusing `False`**, and `ToolGate.bind_policy` accepts any
policy handed to it.

So the DEC-26 invariant — stated in `tier_policy.py` as *"no code path may EVER
construct or bind a `StageToolPolicy(taint_drops_external=False)`"* — is enforced
by exactly two things, both of them static:

1. the sanctioned constant being immutable (A2 confirms this holds), and
2. `tests/safety/test_tier_policy.py` asserting no other construction site exists
   **under `src/wombat`**.

That is a property of the source tree, not a guard in the running product. AC2's
bar is "the structural guard refuses the re-bind — DEC-26's invariant tested
against the running product rather than the source", and by that bar it fails.
This is precisely the distinction DEC-84 exists to draw.

**Not exploitable today**, and it should be read that way: no wombat code path
constructs such a policy, `bind_external_tier` is called at exactly two sites, and
the browser capability that would make an escape valuable is entirely unwired
(ISS-66). This is a defence-in-depth gap, not a live hole.

**Honest limit:** the probe runs in a separate process against the same installed
modules; it was not injected into PID 46964's address space. Genuinely external
content *was* processed by the live runtime in this session (the Gmail-derived
draft item reaching `draft_composer`, which is the one stage that calls
`bind_external_tier`), but the adversarial re-bind itself was necessarily exercised
out-of-process.

---

## AC4 — every high-consequence outbound action — **PARTIAL**

Enumerated deliberately from the capability registrations and the stage list, not
sampled. The product has exactly **one** capability at `tier="external"` that is
registered at boot.

| # | High-consequence outbound action | Reachable in the running product? | Attempted / result |
|---|---|---|---|
| 1 | `gmail.drafts.create` (`draft_composer.py:172`, registered `bootstrap.py:1203`) | **YES** — the only one | **Attempted live.** Trail row written first, then `403 Forbidden` from `https://gmail.googleapis.com/gmail/v1/users/me/drafts` (ISS-57, by construction). No draft created. |
| 2 | Send an email | **Structurally absent** — no send capability exists anywhere; `DraftDispatchStage` dispatches **zero capabilities on every path**, approval only marks the trail and the human sends from Gmail | Nothing to attempt — never-send is structural |
| 3 | Write to Google Calendar | **Structurally absent** — no gcal write capability registered | Nothing to attempt; live chat also refused the request |
| 4 | Browser form submit (`stages/form_submit.py`) | **NO** — zero references in `bootstrap.py`, `runtime.py`, `pathways/` | **Unreachable** (ISS-66) |
| 5 | Browser login handoff (`stages/login_handoff.py`) | **NO** — same | **Unreachable** (ISS-66) |
| 6 | Generic approved dispatch (`stages/dispatch_approved.py`) | **NO** — zero references in `bootstrap.py`, `runtime.py`, `pathways/` | **Unreachable** |

**Why this is PARTIAL rather than PASS.** The AC asks that each action "is held for
review and none completes without explicit human approval". Five of the six are
satisfied *vacuously* — they are structurally absent or unreachable, so nothing can
complete unapproved. The sixth, the only reachable one, **403s before it ever
reaches its `AwaitHuman` park**, so the approval gate itself was not observed
holding anything in this session. The never-send guarantee is verified structurally
(`draft_dispatch.py` dispatches nothing on any path, which is a stronger property
than a gate) but the *review* half has still never been watched working end to end
in production.

**One thing worth stating plainly.** On the draft path the Gmail draft is created
**before** the approval park — by design, documented as the taint-order proof. So
the first outbound write to Google happens with no human having seen it. That is
intended behaviour, not a defect, but it means "review before send" is precisely
what it says: review before *send*, not review before *write*.

---

## AC5 — analytics and telemetry — **FAIL (third-party), wombat's own code clean**

### Wombat's own code: clean, and the wire corroborates

- **No analytics SDK in the runtime.** No `posthog`, `sentry`, `mixpanel`,
  `segment`, `amplitude`, `datadog`, `newrelic`, `bugsnag`, `rollbar`, `scarf`,
  `statsig` or similar in `.venv/Lib/site-packages/`.
- **No analytics in the app.** Nothing matching those names in
  `app/package.json`.
- **No tracking call sites.** Grepping `src/` and `app/src/` for
  track/analytics/telemetry/beacon/gtag/usage-report idioms returns only
  *prohibitions* — e.g. `behavior/event_log.py:20` *"there is NO
  dashboard/analytics query anywhere in this module (NG-3)"*.
- **The wire agrees independently** (the non-goal's requirement): four hosts ever,
  none of them an analytics endpoint.

`opentelemetry` (api/sdk/semantic-conventions 1.42.1) **is** present in the venv —
pulled in transitively by **cog-worx**, not by wombat. It is inert here: wombat
never imports it (`grep -rn opentelemetry --include=*.py src/ tests/` → zero hits),
**no OTLP exporter package is installed**, cog-worx configures no exporter or
endpoint, and no collector was contacted on the wire. Recorded because a manifest
scan alone would have flagged it and the wire audit is what cleared it — the
inverse of the failure mode the non-goal warns about.

### The finding: a transitive dependency emits boot telemetry, with no opt-out

`huggingface_hub` (via faster-whisper) enriches its User-Agent on every request:

```python
# .venv/Lib/site-packages/huggingface_hub/utils/_headers.py:183-189
if not constants.HF_HUB_DISABLE_TELEMETRY:
    if is_torch_available():
        ua += f"; torch/{get_torch_version()}"
    agent = detect_agent()
    if agent:
        ua += f"; agent/{agent}"
```

and fetches an agent-harness registry whose own source comment reads *"fetching the
registry is best-effort **telemetry**, never block the caller for long"*
(`utils/_detect_agent.py:48`). That fetch is the
`https://huggingface.co/api/agent-harnesses` call seen 3 times in the logs — 3 and
not 47 because it is cached for 24 h; the cache exists on this machine at
`C:\Users\Jim\.cache\huggingface\.agent_harnesses.json` (5 698 bytes), listing 25
harnesses including `claude-code -> {CLAUDECODE: '*'}`.

**`HF_HUB_DISABLE_TELEMETRY` is set nowhere** — not in `.env`, not in
`pyproject.toml`, not in `src/`. Measured live in the venv:

```
HF_HUB_DISABLE_TELEMETRY = False
HF_HUB_OFFLINE           = False
detect_agent()           = 'claude-code'
USER-AGENT ON THE WIRE   = unknown/None; hf_hub/1.23.0; python/3.13.11; agent/claude-code
```

So the literal bytes wombat sends to a third party at boot report the hf_hub
version, the **Python version**, and the **AI agent harness** the process is
running under.

**Confound, stated rather than hidden:** `agent/claude-code` appears because *this*
boot was restarted by the agent session and inherited its environment. On a boot
Jim starts himself the UA would read `unknown/None; hf_hub/1.23.0;
python/3.13.11` — still version telemetry, still to a non-permitted host, still
with no opt-out configured. The `agent/` component is an artifact of the sweep; the
rest is not.

Whether DEC-29's "absolute" posture binds only wombat's own code or everything the
product puts on the wire is a scope question for the architect, not for this sweep.
It is raised, not resolved.

---

## Findings routed

- **(new, CRITICAL)** — The human-readable action trail has **never been
  rendered**. `ActionTrailRenderer` is constructed nowhere in `src/`; no
  `wombat-trail.log` exists on disk; the only consumer of `action_trail_projection`
  in production is the writer. CON-4's readable-without-decoding promise is unmet.
  Fourth instance of the built-but-never-run pattern.
- **(new, CRITICAL)** — `huggingface.co` is contacted **unconditionally at every
  boot, before any user action**, by the faster-whisper model-revision check, and
  is outside AC3's permitted set. Connection is pooled and held open. Classified
  **needs-ruling** per this sweep's instruction; a literal reading of AC3 makes it
  CRITICAL.
- **(new, MAJOR)** — DEC-26's `taint_drops_external` invariant is **not enforced by
  a structural guard**. An adversarial `StageToolPolicy(taint_drops_external=False)`
  constructs freely and `ToolGate.bind_policy` accepts it; external then survives
  the taint latch. Enforcement today is a frozen constant plus a source-tree test.
  Not exploitable on the current tree.
- **(new, MAJOR)** — A failed side effect is recorded in the trail as `pending`
  forever. `record_refusal`'s `ON CONFLICT DO NOTHING` cannot overwrite the
  `record_proposal` row it always collides with. Both rows in the table's entire
  history are false `pending`s.
- **(new, MAJOR)** — Third-party boot telemetry with no opt-out:
  `HF_HUB_DISABLE_TELEMETRY` is unset anywhere in the repo, so wombat reports
  hf_hub version, Python version and detected agent harness to huggingface.co at
  boot. Wombat's own code contains no analytics; this rides in transitively.
- **(observation, MINOR)** — `api.fish.audio/v1/asr` appears **35 times** in the log
  history: user speech has at times been sent to the cloud voice provider for
  transcription. `api.fish.audio` is a permitted destination so this is not an AC3
  violation, and this session's boot pinned ASR local
  (`ASR inference pinned to device=cpu (DEC-59), model=base`). Recorded because it
  bears on the local-first residency story (CON-7/NG-7), not because it breaches
  the permitted set.

## State left behind

- **Runtime UP and healthy** — launcher PID 7536, worker PID 46964, restarted
  deliberately at `19:23:17Z` under the sampler. Final liveness check at close:
  `POST /chat` → `HTTP 200 {"status": "replied", ...}`.
- **Electron app left running** (launched `19:38:52Z`), with its `wombat.settings_app`
  backend (PIDs 14428 / 9020).
- **Two probe rows remain in the queue's history and one new trail row remains**
  (`seq = 2`, `pending`). Left in place deliberately — it is the evidence for the
  AC1 finding, exactly as TK-366 left `seq = 1`.
- **No product state otherwise changed.** No config file, no `.env`, no settings
  table, no ledger value, no persona, no capability tier, no source and no test
  file was edited. No Gmail or Calendar state was created or modified (the draft
  403'd; nothing was sent). No `HF_HUB_*` variable was set — deliberately, since
  setting one would have made the audit come out clean, which this ticket's first
  non-goal forbids.
- Sampler stopped; raw artifacts frozen in the scratchpad (paths named in AC3).

## Artifacts

- `logs/runtime-20260805-152321.log` — the boot driven under the sampler
- `logs/runtime-20260805-091501.log:13-25` — today's dream + brief run, for their egress
- `scratchpad/egress-sampler2.ps1` — the sampler (scoping rules in its header)
- `scratchpad/egress-run2/conns.csv` — 6 541 raw per-PID endpoint samples
- `scratchpad/egress-run2/dns.csv` — DNS cache snapshots for IP→host correlation
- `scratchpad/egress-run2/pids.csv` — the audited PID set over time
- `scratchpad/taint_probe.py` — the AC2 adversarial probe
- `scratchpad/inject_probes.py` — the queue probes, via `WombatQueue.enqueue`
- `scratchpad/chat_probe.py` — the `POST /chat` driver

(Scratchpad root:
`C:\Users\Jim\AppData\Local\Temp\claude\C--Users-Jim-OneDrive-desktop-Code-wombat\36077954-6992-497a-83c3-56c3933c92c0\scratchpad`)

## What this sweep does NOT claim

Per DEC-85(c) the sweep ran against every named check and every finding was routed.
It does **not** assert FEAT-11 has PASSED — three ACs failed and one is partial.
It does **not** adjudicate whether `huggingface.co` is acceptable egress or whether
DEC-29 binds transitive dependencies; both are raised for the architect and
deliberately left unresolved here. Nothing was fixed. TK-377 alone adjudicates.

---

# Architect's ruling (2026-08-05, contract v2.259–v2.261)

Routed to the architect-of-record after the sweep closed. **DEC-89** rules all six
ambiguities; **ISS-67..ISS-72** carry the findings at architect-judged severities
(the sweep proposes, the architect disposes). No fixes — repairs are post-phase per
`protocol.md`. **No escalation to Jim was spent; the reasoning for that is at the end.**

**Claims spot-checked independently before being trusted** (not taken on the
sweep's report): the renderer has no consumer anywhere under `src/`; `record_refusal`
is `INSERT ... ON CONFLICT (action_id) DO NOTHING` and `draft_composer.py:282`
computes **one** `action_id` handed to both `record_proposal` (:286) and
`record_refusal` (:303, :321), so the collision is certain; `StageToolPolicy` in the
cog-worx source this venv actually loads has **zero validators** and
`bind_policy` (policy.py:185) accepts anything; `_headers.py:183-189` matches
verbatim; the whole-history host count re-ran to 244 / 147 / 50 / 2 — four hosts,
no analytics endpoint.

## The rulings

**(a) `huggingface.co` is CRITICAL — ISS-67.** The literal AC3 reading binds, and
not as literalism. The call is made by the **local faster-whisper adapter — the one
chosen because it is offline** — at every boot before any user action, from
`asr.py:144` constructing `WhisperModel` with no `local_files_only`. It falsifies
DEC-28's headline in terms ("the DEFAULT configuration has exactly ONE egress"), and
**TK-195, the lesion test DEC-28 cites as its proof, is a construction spy**: it
asserts no cloud *class* is instantiated, which is true and beside the point. A
source-tree proof stood in for a wire property. That is DEC-84's thesis, and it is
the most useful thing this sweep established.

**(b) Bounded in the same breath.** The request carries **no user content** — no
audio, transcript, mail, calendar or fact. **CON-7/NG-7 residency is NOT breached.**
What is breached is the egress invariant, plus DEC-29 via the User-Agent. A CRITICAL
that overstates its blast radius trains the next reader to discount the next one.

**(c) DEC-29 binds the WIRE, not the authored source.** Clarifies DEC-29; does not
supersede it. A product is not analytics-free because the analytics arrived by
dependency — and this ticket's own third non-goal already forbids accepting a
dependency's absence as proof of egress absence. The narrow reading makes AC5
unfalsifiable and lets any future `uv add` reintroduce telemetry with the posture
still reading green. Note the **direction**: the broad reading *strengthens* a locked
decision and is within delegated authority; the weakening reading would have been
Jim's to make.

**(d) CON-4's "every side effect" means every WORLD-CHANGING act.** Model calls and
TTS synthesis are **not** trail events. DEC-19 pairs the trail with the AwaitHuman
approval gate, so its unit is a *reviewable action*, not a request; and the Q-89
artifact is deliberately plain-language and decode-free, which 241 rows saying "asked
the model to phrase something" destroys utterly. **This is a scoping ruling, not a
dismissal** — content egress is a real concern that already has a home in
ASMP-1/DEC-28 and in this sweep's own AC3, which is exactly where it got caught.
**AC1's finding therefore stays at the two defects recorded and does not balloon.**

**(e) The out-of-process AC2 probe is sufficient.** A validator's absence is
process-independent and cog-worx is a single editable install, so the probe imported
the identical module the worker imports. The one thing it could not show — whether
any assembled wombat path constructs such a policy — the sweep answered by call-site
enumeration. **TK-377 must not re-litigate the method.**

**(f) The inferred IP is accepted and is not load-bearing** — method 3 names the host
directly 50 times; the inference adds only *which* PID.

**(g) Draft-before-approval is consistent with CON-5, settled here** so it stops
being re-raised each sweep. CON-5 guards irreversible/high-consequence acts; a draft
in the user's own folder is neither, and **sending** is the act CON-5 is about. It
does mean review-before-send is review before **SEND**, not before **WRITE** — the
record should keep saying that plainly.

**(h) The 35 historical Fish `/v1/asr` calls are not a finding** — checked, not waved
past. `wombat_stt_provider` is a **separate** closed-literal config field defaulting
to `local`, and this host's `.env` sets only `WOMBAT_TTS_PROVIDER`. Per-axis opt-in
works exactly as DEC-28 designed it; a TTS selection never dragged the STT axis to
the cloud.

## Severities as disposed

| ISS | Severity | Note on the disposition |
|---|---|---|
| ISS-67 | **CRITICAL** | huggingface.co boot egress. Sweep said needs-ruling; ruled per (a)+(b). |
| ISS-68 | **CRITICAL** | Trail never rendered. CON-4 is a MUST and its artifact has never run. **Fourth consecutive sweep** to find a done-but-never-run feature. |
| ISS-69 | MAJOR | Taint guard is a source-tree test, not a runtime guard. Not exploitable on this tree. |
| ISS-70 | MAJOR | **Deliberately not raised to CRITICAL** — the safety-critical direction is an action that *happened* and is *absent*; this over-reports rather than conceals, and write-before-act ordering was proven live. |
| ISS-71 | MAJOR | HF boot telemetry. Same connection and largely the same fix as ISS-67; filed separately, **sized together**. |
| ISS-72 | MINOR | Both reachability gaps already owned by ISS-57 and ISS-66; exists so the coverage gap is stated rather than inferred. |

## The fix shape pinned on ISS-67, because the naive fix breaks first run

`HF_HUB_OFFLINE=1` or an unconditional `local_files_only=True` means a **fresh
install with no cached model can never obtain one** — contradicting the other half of
DEC-28 (zero-config wombat works out of the box). A later reader would hit that and
revert the fix. The correct shape is **conditional**: permit the fetch exactly once
as first-run *provisioning* when the model is genuinely absent, never on a subsequent
boot. Pin `HF_HUB_DISABLE_TELEMETRY` regardless, as defence in depth. **Verification
bar: proven on the wire by re-running this sweep's sampler across a boot — never by
reading the flag.** Reading the flag is what TK-195 already did.

## Two instances of DEC-84 in one sweep

TK-195 proved a construction property and the wire disagreed (ISS-67); the DEC-26
invariant is likewise asserted by a source-tree test rather than a runtime guard
(ISS-69). Both are the same shape: **a property of the source standing in for a
property of the running product.** That is now this phase's headline finding, and
it composes with ISS-63/64/66/68 — the board counts code, not liveness.

## Why nothing was escalated

Each candidate resolved against the locked frame. The huggingface repair *restores*
DEC-28 rather than changing it. The CON-4 scoping is an interpretation the trail's
own design (DEC-19 + Q-89) already implies. The DEC-29 broadening runs in the
strengthening direction. The only reading that would have required Jim is the
**weakening** one — DEC-29 binding wombat's own code only — and it is rejected.
