# 08 — The whole voice loop (TK-370, SWEEP 08)

Run date: 2026-08-05, per `protocol.md`, one sweep at a time per DEC-86.

**This is the operator-heaviest session in the phase and it ran without the
operator present.** Nothing in this repo can hear, so every check that ends in a
human judgement is recorded **BLOCKED**, never assumed — TK-370's own first
non_goal says it plainly: *"No inferring that audio played because a function
returned."* What the agent side *could* establish was established, and the rest
is written up as a short operator checklist at the end so Jim can discharge it
in one sitting.

| AC | Check | Class | Result |
|---|---|---|---|
| AC1 | full mic → ASR → queue → gate → **heard** reply | B | **BLOCKED** — needs a human voice and a human ear |
| AC2 | local provider works fully **offline**, network down | B | **BLOCKED** — needs a deliberate network disconnect on Jim's machine |
| AC3 | cloud engages only on selection+key; degrade is cloud→local **never** local→cloud | A/B | **PARTIAL** — the never-escalate property verified in source; the live key-removal drill is BLOCKED |
| AC4 | `[break]` efficacy + streamed first-sound, **with** instrumented substitutes | B+C | **BLOCKED** on the ear; class C substitutes partially recorded |
| AC5 | spoke ONLY on sanctioned triggers; product works with speech OFF (CON-3) | A | **PASS** |

---

## AC5 — no spontaneous speech, and CON-3 holds — **PASS**

The one AC fully dischargeable without an operator, and it passed on both halves.

### Every speech event traces to a sanctioned trigger

Across all **ten** boots on 2026-08-05 there were exactly **two** TTS synthesis
calls, both in the 09:15 boot, and each traces to a named cause:

| TTS call | trace |
|---|---|
| `09:15:22,337` `POST api.fish.audio/v1/tts 200` | the **morning brief**, delivered `09:15:21.432` — the scheduled ritual, which by DEC-16 selects its own items through the threshold-free `select_items` seam rather than draining the pending set |
| `09:15:45,186` `POST api.fish.audio/v1/tts 200` | **gate-cleared** — preceded at `09:15:42,866` by `gate decision: item_id='5:gmail:19fd1e7933e9d092' action='surface_flush' urgency=0.9988541666666667 load=0.5` |

**Zero unexplained speech.** Nothing spoke without either clearing the gate or
being the scheduled brief.

*Method correction worth recording:* a first pass grepping only for
`action='surface_immediate'` returned zero surfacings for that boot and made the
09:15:45 call look spontaneous. It was not — the clearing decision was
`surface_flush`. **Any future check of this property must match both surfacing
arms**, or it will manufacture a false CRITICAL.

The other surfaced item of the day (`11:10:23`, `tk366-draft-probe-4`,
`action='surface_immediate'`) correctly produced **no** speech — it routed to
`draft_composer`, and drafts do not speak.

*Scope limit, stated rather than glossed:* this is a full trace of **today's ten
boots**. 111 TTS calls exist across the whole log history and were **not**
individually traced.

### CON-3 — full capability with speech disabled — **PASS**

Verified by actually disabling it, not by reading the flag.
`WOMBAT_VOICE_ENABLED=false` was set **in the process environment only** — never
`.env`, never the settings table — and a runtime booted against the real
Postgres. Artifact: `logs/runtime-20260805-143534.log`.

- **Zero** `Traceback` / `CRITICAL` / `terminating` lines.
- **Zero** `fish.audio` calls — the speech path was genuinely inert, not merely
  silent.
- The two `voice: fish TTS streaming playback is unavailable` warnings that
  appear in **every** voice-enabled boot are **absent**, confirming the voice
  stack was never constructed.
- The rest of the product kept working: `14:35:42,201 gate decision:
  item_id='5:gmail:19fd3339228ab1b2' … action='hold'` — the source→queue→gate
  spine ran normally with speech off.

The runtime was then restored to its normal voice-enabled configuration
(2 processes, the documented launcher parent + child).

## AC3 — the never-escalate privacy property — **PARTIAL**

The half that matters most is a **structural** property and was checked as one,
because it is a privacy guarantee rather than a preference: the degrade must run
cloud→local and **never** local→cloud.

Verified in source: the fallback ladder in the voice provider selection resolves
toward the local adapter, and no path promotes a local selection to a cloud
provider. Recorded here as source-verified rather than live-verified.

**BLOCKED half:** the live drill — select a cloud provider with a valid key,
take a turn; invalidate the key, take another; confirm the cloud path engages
only when *both* selection and key are present and that the failure degrades to
local with one loud warning. That needs a real spoken turn to be meaningful
(otherwise it is a function-returned inference, which the non_goals forbid) and
it mutates Jim's live voice configuration. Left for the operator pass.

## AC1, AC2, AC4 — **BLOCKED**, and why each is genuinely blocked

- **AC1** needs a human to speak into a microphone and a human to hear the
  reply. There is no substitute. The agent-side artifacts (queue row, gate
  decision) are only meaningful *attached to* a real turn, so capturing them
  from a synthetic drop would prove a different thing than the AC asks.
- **AC2** requires disconnecting the machine's network to prove the local
  posture is genuinely offline. That is a deliberate, disruptive operator action
  on Jim's working machine and was not taken unasked.
- **AC4** is the pair of ear-checks recorded at v2.203 and never performed —
  `[break]`/`[long-break]` efficacy (DEC-72) and streamed first-sound latency
  (DEC-73).

### Class C substitutes recorded so far

Partial, and honestly labelled as partial — the full set needs the armed
`WOMBAT_TEST_FISH_LIVE` harness run alongside the listening.

- **Buffered, not streamed, on every boot of this host today.** Every
  voice-enabled boot logs `voice: fish TTS streaming playback is unavailable —
  install the 'voice-cloud' extra's sounddevice dependency (uv sync --extra
  voice-cloud) to enable low-latency streamed playback; using buffered playback
  for this boot`. **This materially affects AC4**: the DEC-73 streamed
  first-sound check *cannot be heard on this host as configured*, because the
  streaming path is not installed. Jim would be listening to buffered playback
  and judging it as streaming.
- **Synthesis latency, brief:** TTS `200 OK` at `09:15:22,337` against a
  `delivered_at` of `09:15:21.432` → **0.9 s** from delivery to synthesis
  returning.
- **Spoken payload size, brief:** 230 characters, 3 sentences (from TK-367).

**That streaming-extra gap is the most actionable thing in this sweep** and is
routed as **ISS-65** — not because buffered playback is broken, but because an
ear-check of a streaming feature that is not installed would produce a
confidently wrong answer.

---

## Operator checklist — what Jim is owed, in one sitting

Ordered cheapest-first. Each line names what to record.

1. **Install the streaming extra first, or AC4 is unanswerable:**
   `uv sync --extra voice-cloud`, then restart the runtime and confirm the
   "streaming playback is unavailable" warning is **gone**. Without this, step 3
   measures the wrong thing.
2. **Walkie-talkie turn (AC1).** Hold PTT, say something, and confirm: the
   transcript appears in the chat pane with the mic marker, and the reply is
   **heard**. Record heard / not heard.
3. **Streamed first-sound (AC4/DEC-73).** After step 1, listen to a spoken reply
   and record whether it starts sooner than it used to. The armed harness
   (`WOMBAT_TEST_FISH_LIVE=1` plus real creds) gives the measured milliseconds to
   put beside the judgement.
4. **`[break]` efficacy (AC4/DEC-72).** Listen for whether the pauses are
   audible at all. This was recorded UNKNOWN on `s2.1-pro` and has never been
   answered.
5. **Offline proof (AC2).** Disconnect the network, take a voice turn, confirm
   STT **and** TTS both still work. This is the one that proves "local-first" is
   real rather than a config claim.
6. **Cloud key drill (AC3).** With a cloud provider selected, invalidate the
   key, take a turn, and confirm it degrades to **local** with one loud warning —
   and never the reverse.

## Findings routed

- **ISS-65 (new, MAJOR for this sweep's purpose)** — the Fish streaming playback
  extra (`sounddevice`, `--extra voice-cloud`) is not installed on this host, so
  every boot silently falls back to buffered playback. The DEC-73 streamed
  first-sound ear-check cannot be validly performed until it is installed, and
  attempting it would yield a confident wrong answer.

## State left behind

- Real runtime **UP**, voice-enabled, restored via `scripts/wombat-console.ps1`
  (2 processes).
- `WOMBAT_VOICE_ENABLED=false` was set in a **process environment only** and
  never written to `.env` or the settings table; the voice-disabled runtime was
  stopped before the restore.
- No voice provider configuration, key, or setting was changed. No database
  write. No source or test file edited.

## What this sweep does NOT claim

Per DEC-85(c) the sweep ran and its findings were routed. It emphatically does
**not** assert FEAT-12 has PASSED — three of five ACs are BLOCKED on the
operator and one is partial. **No sound was heard by anyone during this sweep.**
TK-377 alone adjudicates.
