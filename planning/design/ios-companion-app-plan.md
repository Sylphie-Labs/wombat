# iOS + watchOS companion app — watch PTT, return audio, and biometrics

> ## ⚠ SUPERSEDED IN PART BY **DEC-90** (contract v2.262, 2026-08-05) — READ THIS FIRST
>
> **This document's watch-transport analysis is WRONG and must not be built from.** Every claim
> that the watch reaches wombat over its own Wi-Fi — §0 "conclusion 4: *the watch does not need
> the phone*", the §0 platform-facts table, the §2 Path A diagram, the §4.3 decision tree, §4.4,
> the §4.7 transport table and the §6 Phase 4 plan — is **struck**.
>
> **The ruling:** the iPhone is the **only** Apple device that pairs with wombat's DeviceSurface.
> Watch mic and speaker I/O relays through the phone over **WatchConnectivity**. The watch holds
> **no token** and makes **no network call** to wombat. wombat never knows a watch exists.
>
> **Why this document went wrong — the provenance error, named because it matters more than the
> design.** Jim's verbatim **Q-m** answer at line 20 below (*"Phone is often elsewhere — watch
> needs its own playback"*) is about **audio playback** — the watch needs its own **speaker**.
> A research agent extrapolated it into a **transport** claim, and DEC-79/DEC-82/EP-43 then built
> watch-direct networking on a requirement **Jim never gave**. DEC-79's title called it a "JIM
> DIRECTIVE"; it was not. This is the incident behind the **DON'T INVENT PATTERNS** rule in
> `CLAUDE.md`.
>
> **What survives:** everything phone-side and biometric — the HealthKit path (§3, Path C) was
> always phone-mediated and is untouched. **§7's Q-k (is WatchConnectivity fast enough?), which
> this document dismissed as "largely dissolved", is now the single most important open
> performance question in FEAT-15** — it sits on the critical path in both directions.
>
> The body below is **kept unrewritten as the historical record of how the error was made.**
> The live specs are `planning/design/wire-contract.md` and DEC-90 in `planning/contract.yaml`.

**Status: RESEARCH + PLAN, REVISION 4 — FINAL. Ready for architect hand-off.**
**Nothing is decided here. No code, no contract edit.**
Author: research agent. Rev 1 2026-08-01 · Rev 2 (Q-a/b/d/f) · Rev 3 (Q-j) · **Rev 4 (Q-m) — final.**
Audience: Jim + the architect. This document proposes; the architect rules and records.
§7 is written to be lifted straight into `governance` as `open_question` entries.

**Original ask, verbatim:** *"I want to put an opus agent on planning an iOS app build. We will use
the main phone app to connect to watch for biometrics. we will use the phones health features to
share data with us."*

**Jim's answers, verbatim where quoted:**
- **Q-a (watch app?)** — *"watch integration is also for communication when i dont have headphones
  in. It would be a simple app with a push to talk button on the watch. Crazy simple."*
- **Q-b (cadence?)** — *"Real time biometric data is preferred."*
- **Q-d (usage?)** — *"all of the above"* — silent context, grounding, and interruption events.
- **Q-f (connectivity?)** — **LAN-only for MVP.**
- **Q-j (where does the reply come out?)** — *"Real audio streamed back to phone/watch."*
- **Q-m (is the phone on-body?)** — ***"Phone is often elsewhere — watch needs its own playback."***
  → **The watch must be able to play the reply on its own**, without depending on the phone being
  within Bluetooth range. This is the harder path and it is the required one.

**Arc:** rev 1 was a passive biometric pipe. Rev 2 made a watch talk-button the headline. Rev 3
closed the loop with return audio. **Rev 4 makes the watch self-sufficient** — and the central
research finding is that this is *far less painful than it sounds*, because **every Apple Watch
model has Wi-Fi**, and wombat is LAN-only by decision.

---

## 0. Ground truth I verified (not assumed)

All read out of the live tree / contract, except the Apple platform rows (sourced, §Sources).

### Observation / biometric substrate

| Fact | Where |
|---|---|
| `wombat_observations` is `channel/kind/started_at/ended_at/payload(jsonb)/day_key`, append-only, **the module enforces no channel vocabulary** | `src/wombat/observations.py` |
| Retention is **21 days, a pinned module constant**, pruned once at boot (DEC-63 no-knob) | `observations.py`, `runtime.serve()` |
| DEC-68 **explicitly rejected** "observations as SourceRegistry sources feeding the queue — they are not interruption candidates" | contract DEC-68 `alternatives` |
| Consent is **per-channel, default-OFF, structural**: a disabled channel's collector is *never constructed* — "inert by ABSENCE, not by flag-checking" | DEC-68(b); `config.py` `wombat_observe_*`, all `= False` |
| The memory wipe (DEC-75/76) enumerates tables from `information_schema` — biometric rows are swept **for free** | contract DEC-75(b) |

### PTT / voice-in substrate

| Fact | Where |
|---|---|
| **DEC-58 verbatim:** holding the binding "drives the EXISTING capture path (**MicCapture -> encodeWav -> saveCapture -> WOMBAT_ASR_DROP_DIR -> ASRSource**) with **zero new transcription machinery**" | contract DEC-58 |
| **DEC-64 verbatim:** "**zero app changes needed, the existing `app/src/ptt.ts` capture -> drop dir -> ASRSource loop IS the reply channel**" | contract DEC-64 |
| `saveCapture` runs in the Electron **main** process and resolves `WOMBAT_ASR_DROP_DIR` itself | `app/electron/main.ts:89`, `preload.ts:37` |
| `ASRSource` scans non-recursively for `{.wav,.m4a,.mp3,.flac}`; `event_key` = **sha256 of file bytes**; moves to `processed/`/`failed/` | `sources/asr.py:108-112,191-210` |
| The drop-dir is **POLLED**: `DEFAULT_ASR_POLL_INTERVAL_SECONDS = 2.0` | `sources/bootstrap.py:151` |
| Walkie-talkie threading window `LAST_SPOKEN_TTL_SECONDS = 120.0` (pinned) + 600-char cap; `context_hook` stamps `replying_to` | DEC-64(A); `voice/reply_context.py` |
| **DEC-58(b) limits desktop PTT to "while the app window is focused"** | contract DEC-58(b) |

### Streaming TTS substrate — the return-audio seam

| Fact | Where |
|---|---|
| `FishAudioTTSAdapter._speak_streaming` is: `writer = writer_factory()` → `for chunk in transport.stream(...): writer.write(chunk)` → `writer.finish()`; on mid-stream failure `writer.abort()` then raise | `voice/tts.py:163-196` |
| `writer_factory` is an **injected, keyword-only, zero-arg callable**, default `None`, wired at the `voice.select` construction seam | `voice/tts.py:126,135-139` |
| **`StreamingAudioWriter` takes an injected `stream_factory`**, typed `Callable[[], AudioOutputStream]`, where **`AudioOutputStream` is already a `Protocol`: `write(bytes)` / `stop()` / `abort()` / `close()`** | `voice/stream_playback.py` |
| The stream is **raw 16-bit mono PCM at `STREAM_SAMPLE_RATE = 44100`** — one shared constant the Fish request reads for `sample_rate`. **No RIFF/WAV header on this path** (DEC-73d chose `pcm` to sidestep the TK-262/TK-264 poisoned-header class) | `voice/stream_playback.py`; DEC-73(d) |
| Frame discipline: bytes cut to whole-frame multiples, remainder carried to the next `write()` | `voice/stream_playback.py` |
| **The BUFFERED path survives byte-identical as the degrade path** — `wav`, no `latency` field; `AudioPlayer` Protocol takes a complete WAV | DEC-73(d) |
| `PartialSpeechError(played_any=…)` — `True` ⇒ SpeakSink fires `on_spoken` + ONE loud WARNING; `False` ⇒ byte-identical to any other failure | DEC-73(e); `sinks/speak.py:180-186`; `voice/select.py:105-116` |
| `speak()` stays **blocking-until-audio-done**, preserving DEC-64's fire-`on_spoken`-after-heard contract | DEC-73(e) |

### Network posture and routing plumbing

| Fact | Where |
|---|---|
| The only HTTP server is loopback-only: `BIND_HOST = "127.0.0.1"`, per-launch `X-Wombat-Token` | `settings_app/api.py:91` |
| `InputSource` is **poll-only by contract**; push-shaped producers ride `PushSource` (Q-86) | `sources/base.py` |
| **`QueueItem` carries only `idempotency_key`, `payload`, `item_id` — there is NO `source_id` field** | `queue.py:53-62` |
| **`format_payload_fields` renders EVERY payload key**, sorted, into the compose prompt | `compose/templates.py:32-41` |
| NG-9: *"wombat observes the computer it runs on: one screen, one webcam, one mic, one host."* | contract NG-9 |

### Apple Watch platform facts — **new this revision, and they reshape §4**

| Fact | Consequence |
|---|---|
| **Every Apple Watch model — GPS-only included — has Wi-Fi** and can join networks the paired iPhone has previously joined, *even with the iPhone off or absent* | **The GPS-only-has-no-network premise is false.** On Jim's home LAN, a GPS-only watch can reach wombat directly. Cellular only buys *away-from-home*, which LAN-only already excludes. |
| Watch Wi-Fi "isn't always 100% reliable"; it can't join networks needing a captive-portal/secondary auth | The watch path needs an honest failure mode, not an assumption of reachability |
| Audio routes to paired Bluetooth headphones if available, **else the Apple Watch speaker** | Jim's case is explicitly *no headphones* ⇒ watch speaker |
| **~10 minutes of watch-speaker playback costs ~1 hour of battery**; speaker playback **is not supported while the watch is charging** | A real, nameable cost (§4.6) — and "on the charger" is a genuine dead state |

### The four conclusions that matter most

1. **Voice-IN reuse is confirmed by source.** The watch PTT button is a new trigger + transport
   into a shipped, proven pipeline. **Do not build a second conversation pathway.**
2. **Voice-OUT already has its seam.** `AudioOutputStream` is a four-method `Protocol` with an
   injected factory. Every playback destination is one more implementation of it.
3. **Origin routing cannot ride a payload key.** Verified: `format_payload_fields` renders every
   key into the prompt. §4.2 proposes a register instead, on DEC-64's own precedent.
4. **The watch does not need the phone.** Wi-Fi + LAN-only means watch-direct is the *primary*
   path, and phone relay is the *fallback* — the inverse of rev 3's assumption.

---

## 1. Scope — settled

| Concern | Decision |
|---|---|
| **Watch PTT app** | **In scope.** Headline feature. |
| **Return audio** | **In scope.** Phone playback *and* **standalone watch playback** (Q-m). §4. |
| **Biometric data path** | **Read from the iPhone's HealthKit store.** A paired Watch syncs HR/HRV/sleep/steps/workouts/SpO2 automatically — zero watch runtime cost. |
| **Continuous live HR (`HKWorkoutSession`)** | **Separate, explicitly-decided, default-OFF mode.** Not MVP. See Q-b′. |

### The watch app

**A push-to-talk button, and now a speaker.** One hold control, a recording indicator, playback of
the reply. Still small — but no longer "crazy simple," and the plan should be honest that Q-m
moved it from *a button that uploads a file* to *a small autonomous client*. It also **fixes a real
limitation of desktop PTT**: DEC-58(b) binds that to "while the app window is focused," so Jim
cannot talk to wombat from across the room today. The watch is *additional* — DEC-58 untouched.

### The `HKWorkoutSession` correction (carried, still stands)

PTT is **momentary**; continuous high-frequency HR needs an **always-running workout session**.
What went sunk when the watch app was confirmed is the *target, pairing, provisioning and build
plumbing*. What did **not** is a continuous runtime mode with recurring battery drain and
**phantom workouts polluting Jim's Activity rings**. Hence Q-b′.

---

## 2. Data flow — three paths, one listener

### Path A — voice IN (latency-sensitive)

```
Watch: press-and-hold PTT → AVAudioRecorder → short WAV/M4A
      ▼  watch on LAN Wi-Fi → POST direct;  else → WatchConnectivity relay via phone
wombat: POST /voice  ── consent-gated, token-authed
      ▼  writes the file into a REMOTE-ORIGIN drop-dir
─────── EVERYTHING BELOW IS SHIPPED AND PROVEN ───────
ASRSource.poll() (2.0s) → faster-whisper → SourceEvent
      │ context_hook stamps replying_to (LastSpokenRegister, 120s TTL)
      ▼
WombatQueue → gate → compose → speech_shape → SpeakSink
```

**The novel engineering is one box:** an authenticated route that writes bytes into a directory
`ASRSource` already watches. No new source, no registry/ASR/gate/compose change.

### Path B — voice OUT (§4) — one Protocol, three sinks

```
SpeakSink → FishAudioTTSAdapter._speak_streaming → writer_factory()
      ▼  raw 16-bit mono PCM @ 44100, whole frames
   ┌──────────────────────┬───────────────────────────┬────────────────────────────┐
   │ local (today)        │ phone (live session)      │ watch (buffer-then-play)   │
   │ PortAudio stream     │ WebSocket, chunk-as-you-go│ accumulate → publish → pull│
   └──────────────────────┴───────────────────────────┴────────────────────────────┘
        all three are AudioOutputStream implementations — write/stop/abort/close
```

### Path C — biometrics (latency-tolerant)

```
Apple Watch → (Apple's own sync) → iPhone HealthKit store
      │ HKObserverQuery + enableBackgroundDelivery → wakes app
      │ HKAnchoredObjectQuery (persisted anchor per type) → only what's new
      ▼ project to a CLOSED numeric/enum shape, batch
POST /biometrics → ObservationStore.append_segment(channel='biometric', kind=…)
      ▼   wombat_observations — existing table, NO migration needed
```

Proposed kinds: `sleep_session`, `workout`, `resting_hr_daily`, `hrv_daily`, `steps_hourly`.

### Latency budget — voice round trip

| Segment | Estimate | Basis |
|---|---|---|
| Watch record + finalize | ~0.2–0.5s | file close |
| Watch → wombat (direct Wi-Fi) | ~0.1–0.4s | small file, LAN |
| **`ASRSource` poll wait** | **0–2.0s (avg ~1.0s)** | `DEFAULT_ASR_POLL_INTERVAL_SECONDS = 2.0`, verified |
| faster-whisper (`base`, CPU), short clip | ~1–3s | CPU-pinned local ASR |
| gate + compose (DeepSeek) | ~2–8s | cloud mouth |
| Fish time-to-first-chunk | ~0.3–1s | the DEC-73 win |
| **→ first sound, PHONE path** | **~4–13s** | streaming preserved |
| **→ first sound, WATCH path** | **~7–19s** | **+ full synthesis + full transfer — §4.5** |

**The 120s `LAST_SPOKEN_TTL_SECONDS` window is never at risk**, even on the slower watch path — so
`replying_to` threading is safe by a wide margin. Latency here is felt-UX, never correctness.

### Q-d: "all of the above" — how the three tiers attach

1. **Silent context** — ledger rows → the nightly dream pass. Zero new mechanism.
2. **Grounding** — a bounded biometric line into the mouth prompt. DEC-68(d) precedent exactly.
3. **Interruption** — a closed set of events (workout ended; resting HR anomalous; sleep debt
   crossed) become queue items via **`PushSource`** (Q-86). Registry and `base.py` byte-untouched.

Tier 3 is a **narrow, deliberate carve-out** from DEC-68's not-interruption-candidates rule,
justified because Jim asked for it — record it as a named, bounded exception with a closed event
vocabulary, not a general reopening. **Sequencing: tier 1 first.**

---

## 3. Connectivity — LAN-only, settled

- **One listener** on the LAN interface. Bind address is **explicit config, defaulting to
  loopback**, so an unconfigured wombat is byte-identical to today.
- Per-device bearer token mirroring `X-Wombat-Token`, including its **anti-enumeration rule: 401
  on every route, never a 404**. **Two paired devices now** (phone and watch) — the token store
  must be per-device from the start, not a single shared secret.
- Enrollment via QR minted by the **Electron app**. The watch cannot scan a QR; **pair the watch
  through the phone app** (phone enrolls, hands the watch its own token over WatchConnectivity
  once, at setup time — a one-shot config transfer, not a runtime dependency).
- **Narrow surface.** Two POST routes, one WebSocket, one GET for buffered utterance pull. No
  read routes, no config routes, no control routes. Deliberately *not* the settings API widened.
- **Not constructed at all** when the consent toggles are off (§5).
- Hostname/IP entered once at pairing. Bonjour **deferred**.
- iOS/watchOS need **`NSLocalNetworkUsageDescription`**. One Info.plist string per target.

### The connection direction never inverts

wombat never dials a device. The phone and watch are **clients in all paths** — they POST audio
and biometrics, they open the WebSocket, they pull buffered utterances. No inbound-to-device
surface, no device-side server, no background-execution fight, no NAT or address discovery, and
**no additional architectural widening beyond the one LAN listener already owed a DEC.**

### Still needs a DEC

Even LAN-scoped, this is the **first inbound network listener wombat has ever had** and the
**first off-host sensor** — what NG-9's *"one screen, one webcam, one mic, one host"* speaks to.
A phone and watch are not a home-wide camera hive, so this is arguably outside what NG-9 forbids,
**but the architect must rule explicitly rather than let it pass silently.** Same class as ASMP-1
and DEC-28. **Jim's Q-f answer supplies the direction, not the record.**

### Accepted consequences

- **Voice only works at home, on-network, while the laptop is awake.** See Q-g.
- Biometrics buffer and drain gracefully. **Voice does not** — DEC-64's 120s TTL says so
  structurally. **The app should refuse stale audio rather than deliver it late.**

---

## 4. Return audio — design

### 4.1 The seam: one Protocol, three implementations

DEC-73 left exactly the hook this needs. `StreamingAudioWriter` takes an injected `stream_factory`
returning **`AudioOutputStream`** — already a `Protocol` with `write(bytes)` / `stop()` /
`abort()` / `close()`. **Every playback destination is one more implementation of it:**

| Sink | Destination | Shape |
|---|---|---|
| PortAudio stream *(today)* | laptop speakers | live |
| **`RemoteAudioStream`** | phone, over its WebSocket | **live, chunk-as-you-go** |
| **`BufferedUtteranceSink`** | watch | **accumulate → publish → pull** |

`BufferedUtteranceSink.write()` appends to an in-memory buffer; `stop()` seals it and publishes it
as one retrievable utterance; `abort()` discards it. **That is the whole class.**

Why this is the right seam and not merely *a* seam:

- **Frame discipline is inherited.** `StreamingAudioWriter` already cuts to whole frames and
  carries remainders. No sink re-implements it.
- **The wire format is already ideal.** Raw 16-bit mono PCM @ 44100, **no RIFF header anywhere**
  (DEC-73d chose `pcm` precisely to sidestep the poisoned-header class). Nothing re-encodes.
- **One shared constant still governs.** `STREAM_SAMPLE_RATE` is what Fish is asked for; both
  devices read the same number from the pairing handshake. Nothing can disagree.
- **`PartialSpeechError` needs no extension** (§4.7).
- **`voice/tts.py`, `sinks/speak.py` and the speak path stay byte-untouched.**

**Recorded alternative for the watch path:** route it through the **existing buffered-WAV branch**
(DEC-73d keeps it byte-identical as the degrade path, and `AudioPlayer` already takes a complete
WAV). Rejected as the default because it forks routing earlier — at the adapter rather than the
sink — and re-introduces RIFF headers that DEC-73d deliberately removed. Named so the architect
can take it if he prefers one fewer class over one fewer code path.

### 4.2 Routing — reply follows the turn's origin

| Turn originated from | Reply plays on |
|---|---|
| Desktop PTT / Electron app | **Laptop speakers, exactly as today. Byte-identical.** |
| Watch / phone PTT | **The remote sink** (decision tree, §4.3) |
| Brief / draft / proactive surfacings | **Laptop, as today** — not latency-sensitive, and pushing unsolicited audio at a wrist is a different, unrequested feature |

Additive, never a replacement. A wombat with no paired device behaves exactly as it does now.

**The carrier problem, and the verified constraint.** Origin must travel from the ingest route to
`SpeakSink`. Two obvious carriers are ruled out by what I read:

- **A payload key (`origin: "watch"`) — REJECTED.** `format_payload_fields` renders **every**
  payload key, sorted, into the compose prompt (`compose/templates.py:32-41`). An origin field
  would leak into what the mouth reads and could change the reply's wording.
- **`QueueItem.source_id` — DOES NOT EXIST.** Only `idempotency_key`, `payload`, `item_id`.
  Origin is *encoded* in the key, but string-parsing a key inside a sink rots.

**Proposed carrier: a single-slot in-memory register mirroring `LastSpokenRegister` verbatim.**
DEC-64 already threads state between these exact two points with a TTL'd, single-slot, in-memory
register that a restart forgets. A sibling `LastTurnOriginRegister` is the same shape, lifetime,
seam and accepted tradeoffs — written when the remote route accepts an utterance, read by the
factory when the reply is spoken. **Zero prompt pollution, zero queue-schema change, zero new
pattern.** Register the remote ingest as a **second `ASRSource` with its own id** (`asr_remote`)
over its own drop-dir; the registry already drives N sources independently.

**Ticket-time seam to verify:** the factory is bound once at `voice.select` construction, so
per-turn routing means the *factory closure* reads the register at call time. An ordinary closure
change — but confirm against `voice/select.py` before pricing. **This is the one implementation
unknown in §4.**

### 4.3 The decision tree — explicit

```
reply ready to speak
│
├─ turn originated on laptop/desktop
│     └─► LOCAL SPEAKERS — byte-identical to today
│
└─ turn originated remote (watch or phone PTT)
      │
      ├─ live phone WebSocket session open?
      │     └─► PHONE, streamed chunk-as-you-go   ← best UX, DEC-73 win preserved
      │
      └─ else → WATCH, buffer-then-play
            │
            ├─ watch reachable on LAN Wi-Fi?
            │     └─► watch pulls the sealed utterance directly   ← PRIMARY watch path
            │
            ├─ else, phone reachable to watch over Bluetooth?
            │     └─► phone relays via WatchConnectivity transferFile   ← fallback
            │
            └─ else
                  └─► degrade loudly, played_any=False, NO rescue (§4.8)
```

**Phone playback is kept** (per the steer) as the better-UX branch when a live phone session
happens to exist — it costs nothing extra once `RemoteAudioStream` exists, and it preserves
DEC-73's streaming win whenever Jim's phone *is* to hand.

### 4.4 Does the watch need its own network? — **both models do, and both have it**

This is where the research overturned the premise. **The watch-model question is much less
architecture-determining than expected:**

| Model | Wi-Fi | Cellular | Can reach wombat's LAN listener directly? |
|---|---|---|---|
| **GPS-only** | **Yes** | No | **Yes**, on a network the paired iPhone has joined before — *even with the iPhone off or absent* |
| **GPS + Cellular** | Yes | Yes | Yes, plus away-from-home reachability |

**Since Q-f settled on LAN-only, cellular buys nothing wombat can use.** Away-from-home is already
out of scope (Q-g). So **the primary watch path — direct Wi-Fi to the LAN listener — works on
either model**, and Q-n (§7) is a reliability/expectations question rather than an architectural
fork. That is a materially better position than rev 3 anticipated.

**The honest caveats**, which is why the phone-relay fallback stays in the tree:
- Watch Wi-Fi "isn't always 100% reliable," and the watch prefers Bluetooth-to-phone when the
  phone is near — so reachability is opportunistic, not guaranteed.
- The watch cannot join networks requiring a captive portal / secondary auth (fine for a home LAN).
- watchOS background execution is heavily restricted — assume the fetch-and-play happens while the
  app is **foregrounded**, which it is by construction: Jim just held the button.

### 4.5 Buffer-then-play mechanics, and the latency regression — named plainly

**Where the buffer lives: wombat-side.** `BufferedUtteranceSink` accumulates the PCM as Fish
streams it in, seals on `stop()`, and publishes it for the device to pull over one authenticated
GET. **Not** the phone — putting the buffer on the phone would reintroduce exactly the dependency
Q-m says cannot be relied on. wombat is the always-on party here; it should hold the bytes.

**Size:** 44100 Hz × 2 bytes mono = **~88 KB/s**. Spoken text is capped at 400 chars ≈ ~30s ≈
**~2.6 MB worst case**, typically well under 1 MB. A second or two over Wi-Fi.

**The regression, stated without softening.** DEC-73's entire value proposition was *time-to-first-
sound drops to first-chunk latency*. **The watch path gives that up.** Nothing plays until
synthesis completes AND the whole file transfers:

| | first sound after reply is composed |
|---|---|
| Laptop (today) / phone (streamed) | **~0.3–1s** — first chunk |
| **Watch (buffered)** | **~3–6s** — full synthesis + seal + transfer |

That is a real, user-perceptible regression **for the watch case specifically**, and it is
unavoidable given the platform: buffer-then-play is what a constrained, opportunistically-connected
device can do reliably. **Recommendation: accept it in v1, record it explicitly in the DEC, and
pin the escape hatch** — *sentence-chunked publishing*, where the first sentence is sealed and
published as soon as it is synthesized so the watch starts playing while the rest is still being
made. That is essentially DEF-19's phase-2 pipelining aimed at a different seam, it adds gapless-
playback complexity on a watch, and it should not be built until Jim says the delay bothers him.
**Revisit trigger for the architect: Jim uses the watch path and reports the pause before the
reply starts.**

### 4.6 Battery and the charging dead-state — a real cost, named

- **~10 minutes of watch-speaker playback ≈ 1 hour of watch battery.** A ~30s reply is therefore
  worth roughly ~3 minutes of battery. A chatty day is a measurable dent — worth Jim knowing
  before he forms a habit, not after.
- **Speaker playback is not supported while the watch is on the charger.** That is a genuine dead
  state: PTT will work, wombat will reply, and nothing will play. The watch app should say so
  rather than fail silently.
- Bluetooth headphones, if connected, win the route automatically — which is fine and is the
  better-audio path whenever Jim happens to have them in (and is exactly the case he said he
  *doesn't* have).

### 4.7 Transport shape — inbound and outbound now agree

Rev 3 proposed a WebSocket for outbound. **Q-m changes the answer, and the steer's instinct is
right: the shapes should converge.** Neither direction is truly live in the watch-only case, so:

| Path | Shape | Why |
|---|---|---|
| Voice IN (both devices) | **discrete file POST** | already the design; matches `ASRSource`'s file contract |
| Voice OUT → **watch** | **discrete file GET** (pull the sealed utterance) | mirrors inbound exactly; one request, no framing, no socket lifecycle on a constrained device; a failed GET is retryable, a stalled socket is not |
| Voice OUT → **phone** | **WebSocket, retained** | the *only* place true streaming pays off; it is the one device that can hold a live session, and it preserves the DEC-73 win when available |

So the system is **file-shaped by default, with one streaming fast-path for the one device that
can exploit it.** That is a simpler and more defensible story than rev 3's socket-everywhere
proposal: the watch — the device that actually matters to Jim — never opens a WebSocket at all.

**Pull, not push.** The device asks for its utterance; wombat never dials out. This keeps §3's
connection direction intact and means a watch that was momentarily off-network simply fetches
late (or gives up) rather than wombat needing to discover it.

### 4.8 Failure mid-reply — `played_any` already covers it

Trace a mid-reply failure through the **existing** code:

1. The sink's `write()` (or the device's fetch) fails. `stream_playback` never swallows a write
   failure — CON-3, the caller owns the degrade.
2. `_speak_streaming` catches, calls `writer.abort()`, raises **`PartialSpeechError(played_any=…)`**.
3. `SpeakSink` catches it ahead of the broad arm (it is a `RuntimeError` subclass, ordering
   matters), fires **`on_spoken`** and logs **ONE loud WARNING** naming partial playback.
4. `LastSpokenRegister` updates, so `replying_to` still threads — Jim can walk back into range and
   continue the conversation.

**Zero new mechanism.** DEC-73(e)'s posture — *played-partial counts as spoken, because the user
heard the steward start and the register must reflect the heard world* — is exactly right here,
and it was written for a completely different failure. Strong signal the seam is correct.

**`played_any` semantics per sink, and one honest approximation to record:**

| Sink | `played_any=True` means | Honest? |
|---|---|---|
| PortAudio (local) | PortAudio accepted frames | yes |
| Phone WebSocket | frames went to the socket | slight overstatement, bounded by the jitter buffer (sub-second) |
| **Watch buffered** | **the utterance was sealed and fetched** | **the cleanest of the three** — a completed GET is strong evidence it played |

Interestingly the buffered watch path has *better* `played_any` fidelity than the streaming ones,
because "fetched the whole thing" is a stronger signal than "handed bytes to a buffer." Note also
that **DEC-73(e) already recorded a materially identical approximation** (the register holds the
full intended text, not the heard prefix) — consistent precedent, worth one sentence in the DEC,
not a redesign.

**Nothing fetched at all** → `played_any=False` → byte-identical to any other adapter failure, no
`on_spoken`. Correct: nothing was heard.

**No fallback to laptop speakers.** If every remote branch fails, degrade loudly and stop. The turn
came from the watch *because Jim was not at the laptop*, so falling back speaks into an empty room
while logging success — the "lie of silence" inverted, against DEC-73(e)'s whole posture. Cheap to
revisit if Jim disagrees in practice.

**Buffer hygiene:** sealed utterances need a short TTL and single-fetch-then-discard, or wombat
accumulates unclaimed audio of Jim's conversations on disk/in memory. **Propose: in-memory only,
one slot, TTL pinned near DEC-64's 120s, discarded on fetch or expiry** — no new persistence tier,
nothing for the DEC-75 wipe to reach, consistent with `LastSpokenRegister`'s restart-forgets model.

---

## 5. Consent & privacy

### Mapping onto DEC-68's ambient-consent pattern

- **Per-channel, default OFF, app-editable.** Two toggles — `wombat_observe_biometrics` (passive
  body data) and `wombat_remote_voice` (an explicit, user-initiated mic press *and* its spoken
  reply). Genuinely different consents; bundling them would violate DEC-68's per-channel thesis.
- **Structural absence, not flag-checking.** Toggles off ⇒ listener never bound, no port opened,
  no token minted, **no remote sink constructed**. Inert by absence.
- **Derive-then-discard** — biometrics are projected to a closed numeric/enum shape **before
  anything leaves the phone**. Raw sample graphs, heartbeat series and workout routes never
  cross the wire.
- **Double consent** — HealthKit's per-type authorization and the OS mic permission are second,
  independently revocable gates Jim controls from the device.

### Q-97 / DEC-58(f) posture — preserved, not superseded

DEC-58(f) pinned it: *"the mic opens ONLY on an explicit press and ALWAYS with a visible recording
indicator."* Watch PTT is **exactly** push-to-activate — no VAD, no wake word, no continuous
listening — and the watch must show a recording indicator for the whole hold. **This strengthens
rather than bends the posture**; record it as a new trigger for an existing posture.

### Return audio is a new *output* surface — and now an audible one, in public

wombat's voice has only ever come out of one speaker on one machine. It can now play **out loud
from Jim's wrist**, potentially in a room with other people, and — unlike a phone in a pocket —
a watch speaker is inherently un-private. **Nothing in this design lets wombat speak remotely
unprompted**: §4.2 routes only turn-originated replies remotely, and brief/proactive surfacings
stay local. That is a deliberate privacy property, not an accident, and it should be recorded as
one so a future ticket doesn't casually widen it.

### Taint

**Biometrics: mostly not untrusted.** A heart-rate integer is machine-generated first-party
telemetry with no attacker-controlled text. **But HealthKit samples carry free-text metadata**
(source app, device name, user-entered workout names/notes) written by third-party apps.
**Proposal: no free text crosses the wire in v1** — numbers, enums, timestamps only. That makes
the taint question *not arise* rather than answering it, which is DEC-68(c)'s custody thesis
applied literally. Free text later rides DEC-45 grounding-only tier: bounded context to the mouth,
never tool-dispatch input. **DEC-26 stays untouched.**

**Voice: unchanged.** A transcribed watch utterance is the same class of input as a desktop one,
already flowing through `ASRSource` with CON-1-clean payload fields. **Changing where the
microphone and speaker are creates no new custody question** — a dividend of reusing the pipeline.

### Egress

Nothing new leaves the host. Transcribed speech already reaches DeepSeek and Fish (unchanged); a
bounded biometric line joins that existing flow under Q-d tier 2 on DEC-68(d)'s precedent.
**Return audio adds zero egress** — PCM travels wombat → LAN → device, never outward.

### Two gaps to name

1. **The devices hold second copies.** DEC-75/76's wipe is schema-driven over Postgres and sweeps
   `wombat_observations` for free — but the phone's buffer, its per-type sync anchors, and any
   untransferred watch audio are **outside the wipe's blast radius**. Minimum fix: the apps ship
   their own reset, and the wipe dialog names the devices in its what-does-NOT-die column.
   (wombat's own sealed-utterance buffer is in-memory and TTL'd by §4.8, so it needs nothing.)
2. **Revoked HealthKit permission is indistinguishable from no data.** Apple returns empty results
   with no error, deliberately. The sync engine must never read silence as a signal; surface "last
   successful sample" so a revoked permission looks like a problem rather than a quiet day.

### App Review — not on the critical path

Apple's health rules are strict (no ad networks, no analytics, no selling, no health data in
iCloud, granular per-type permissions, privacy-policy disclosure). **None applies to a personally-
provisioned app that is never distributed.**

---

## 6. Phasing

Input to the architect for epic/DEC/ticket scoping. **No ticket IDs, no acceptance criteria.**

**Phase 0 — decisions only.** The connectivity `DEC-*`, the §4 return-audio ruling (including the
accepted watch latency regression and its revisit trigger), the metric set (Q-c), the Apple
Developer Program question (Q-h). No code.

**Phase 1 — wombat side, no Swift.** The consent-gated LAN listener: `POST /biometrics` →
`ObservationStore`; `POST /voice` → write into the remote drop-dir. Per-device token auth, bind
config, size caps, sha256 idempotency. **Fully `curl`-verifiable before any Apple code exists** —
`curl` a WAV in, hear wombat answer on the laptop. An unusually strong first slice.

**Phase 2 — iOS app, foreground.** SwiftUI. Pairing (QR from Electron). HealthKit authorization.
"Sync now." Persisted anchor per type. Batched POST. No background execution yet.

**Phase 3 — background biometric sync.** `HKObserverQuery` + `enableBackgroundDelivery`,
background `URLSession`, offline buffering and drain-on-reconnect, chunked historical backfill.
Expect **hourly-ish** wakes — the platform ceiling and Apple's own recommendation.

**Phase 4 — watch PTT app (voice IN).** watchOS target. Hold-to-talk + recording indicator.
`AVAudioRecorder` → direct POST over Wi-Fi, phone relay as fallback. Watch pairing via the phone
(§3). Stale-audio refusal. **Reply still plays on the laptop at the end of this phase** — Phase 4
is deliberately shippable and testable without any return audio.

**Phase 5a — return audio to the PHONE.** `RemoteAudioStream` (the `AudioOutputStream`
implementation), the WebSocket route, `LastTurnOriginRegister` + origin-aware factory closure,
phone-side jitter buffer and playback.

**Phase 5b — return audio to the WATCH. *(the phase Jim actually needs)*** `BufferedUtteranceSink`,
the sealed-utterance GET route with TTL and single-fetch discard, watch-side fetch + `AVAudioPlayer`
on the watch speaker, the full §4.3 decision tree, charging/no-speaker and unreachable states
surfaced honestly.

> **Why split 5a/5b, and can either move?** They are genuinely different builds — a live socket
> and a chunk-as-you-go sink versus a buffered sink, a pull route and a constrained-device player.
> **5a is the cheaper one and it de-risks 5b**: it lands the origin register, the routing decision
> tree, the factory-closure change and the first `AudioOutputStream` implementation — everything
> 5b then reuses. **But 5b is where Jim's value is** (Q-m: the phone is often elsewhere), so if the
> architect wants value soonest, 5a can be skipped and its shared parts absorbed into 5b. **My
> recommendation: build 5a first as a cheap de-risk, but treat it as optional, not load-bearing.**
> The wombat half of both can be built and proven with a fake LAN client before either device app
> exists — that is where all the risk lives.

*Note Phase 4 could reasonably move earlier overall — it is the feature Jim asked for, and Phase 1
is its only hard dependency. Sequencing 2/3 first is a risk-ordering choice, not a dependency.*

**Phase 6 — wombat uses the biometrics.** Tier 1 (dream-pass facts) → tier 2 (bounded grounding
line) → tier 3 (closed-vocabulary `PushSource` events through the gate). In that order.

**Phase 7 — continuous live HR.** *(only per Q-b′)* `HKWorkoutSession`, default OFF, with battery
and Activity-ring costs accepted explicitly and up front.

**Explicitly out of v1:** writing back to HealthKit; medical or clinical interpretation; push
notifications from wombat to a device (APNs = cloud dependency + new egress class); replacing
desktop PTT (DEC-58 stands, the watch is additive); remote **unprompted** speech (brief/proactive
surfacings stay local — §5); **sentence-chunked publishing** (the §4.5 escape hatch, trigger
recorded); chunked/streaming *input* audio; OS-global desktop PTT (separately deferred); Bonjour
discovery; standalone watch operation away from the home LAN.

---

## 7. Open questions

### Answered by Jim — for the architect's record

- **Q-a — watch app?** **ANSWERED.** *"...a simple app with a push to talk button on the watch.
  Crazy simple."*
- **Q-b — cadence?** **ANSWERED.** *"Real time biometric data is preferred."* → see **Q-b′**.
- **Q-d — usage?** **ANSWERED.** *"all of the above"* → all three tiers; tier 3 needs a recorded,
  bounded carve-out from DEC-68.
- **Q-f — connectivity?** **ANSWERED.** LAN-only for MVP. **A `DEC-*` is still owed** (§3).
- **Q-j — where does the reply come out?** **ANSWERED.** *"Real audio streamed back to
  phone/watch."*
- **Q-m — is the phone on-body?** **ANSWERED.** *"Phone is often elsewhere — watch needs its own
  playback."* → **standalone watch playback is required.** Resolved into §4.3–4.7, and **cheaper
  than feared: every watch model has Wi-Fi, so watch-direct is the primary path.**

### Resolved by research — proposed defaults, architect to ratify

- **Q-k — is WatchConnectivity fast enough?** **LARGELY DISSOLVED.** With watch-direct Wi-Fi as
  the primary path both ways, WatchConnectivity is now only the *fallback* relay. Where it is
  still used: `sendMessageData` while reachable, `transferFile` otherwise. Worth measuring in
  Phase 4, no longer on the critical path.
- **Q-l — must watch PTT work without the phone nearby?** **ANSWERED BY Q-m: yes**, and the
  research says it can — Wi-Fi on every model, LAN-only by decision.
- **Routing** — reply follows the turn's origin; laptop turns stay local and byte-identical;
  brief/proactive stay local. Carried by a `LastSpokenRegister`-shaped origin register, **not** a
  payload key (verified: payload keys render into the prompt).
- **Playback device** — full decision tree at §4.3: phone-if-live-session, else watch buffered,
  else loud degrade with no laptop rescue.
- **Transport** — file-shaped both directions for the watch; WebSocket retained only for the phone
  fast-path (§4.7).
- **Mid-reply failure** — inherit `PartialSpeechError(played_any=…)` unchanged; loud WARNING; **no
  fallback to laptop speakers**.
- **Watch latency regression** — accepted for v1, named in §4.5, escape hatch (sentence-chunked
  publishing) pinned with a revisit trigger rather than built.

### Genuinely open — for Jim

**Q-n — Which Apple Watch do you have: GPS-only, or GPS + Cellular? (NEW)**
**Lower stakes than expected.** Both have Wi-Fi and can reach wombat on your home network without
the phone, and cellular buys only away-from-home reachability — which LAN-only already excludes.
So this is **not an architectural fork**; it tells us how reliable to expect the direct path to be
and whether "voice anywhere" (Q-g) would even be possible later. Also worth knowing the model year,
since watch-speaker quality and battery vary.

**Q-b′ — "Real time": not-stale, or live-by-the-second?**
*Not stale* (wombat knows today's sleep and resting HR within minutes) is a cheap Phase-3
background sync. *Live* (second-by-second pulse) needs an **always-running `HKWorkoutSession`**
with recurring daily battery cost and **phantom workouts polluting your Activity rings**. PTT does
**not** pay for this — a momentary button and a continuous session are different things.

**Q-c — Which biometrics matter to you?**
Proposed default set: **resting heart rate, HRV, sleep (duration + stages), daily activity/steps,
workouts.** Available but not proposed: blood oxygen, respiratory rate, wrist temperature, mindful
minutes. Every type is a separate authorization prompt — keep the list short and deliberate.

**Q-e — Retention?**
`wombat_observations` prunes at **21 days**, pinned in code, not a setting. Plenty for rhythm
detection, useless for "how has my resting HR trended this year." Long trends mean a separate
table with its own retention, not a knob on this one.

**Q-g — Accepting that LAN-only means the watch does nothing away from home?**
Biometrics buffer and drain gracefully. **Voice does not** — a 4-hour-old utterance is not a
conversation. If you want it working anywhere the answer is an overlay mesh (Tailscale/WireGuard),
decided now rather than retrofitted. Note §4.5: return audio at ~88 KB/s is comfortable on a LAN
and clearly not on cellular, so "voice anywhere" is a bigger change than it looks.

**Q-h — Apple Developer Program, $99/year?**
Without it, a personally-provisioned build's signing expires every **7 days** and must be
reinstalled from Xcode — a treadmill that will make you stop using it, and materially worse now
that a *watch* app is involved. (Whether the HealthKit entitlement is usable on a free personal
team should be confirmed at build time rather than assumed either way.)

**Q-i — Is this biometrics + PTT only, or the seed of a full phone-side wombat?**
With return audio accepted, the devices now have **inbound audio, outbound audio, pairing and
auth** — most of what a device-side wombat front end would need. If that is where this is heading
(the way the Electron app grew into wombat's front end per DEC-31), say so now: it barely changes
Phase 1 today and would be expensive to retrofit later.

---

## Sources

- [Background Support for HealthKit in iOS — Rajveer](https://medium.com/@rajveer.kaur.k19/background-support-for-healthkit-in-ios-aaa0c05fb6e3)
- [Getting Apple Health Data Into Your Backend — Open Wearables](https://openwearables.io/blog/getting-apple-health-data-into-your-backend)
- [Apple HealthKit API: What Data You Can Access and How — Open Wearables](https://openwearables.io/blog/apple-healthkit-api-what-data-you-can-access-and-how)
- [What You Can (and Can't) Do With Apple HealthKit Data — Momentum](https://www.themomentum.ai/blog/what-you-can-and-cant-do-with-apple-healthkit-data)
- [HKObserverQuery and BackgroundDelivery Are Highly Unstable on watchOS 26 — Apple Developer Forums](https://developer.apple.com/forums/thread/803194)
- [How to monitor heart rate in background without affecting Activity Rings? — Apple Developer Forums](https://developer.apple.com/forums/thread/810690)
- [FAQ — ResearchKit & CareKit (Watch data reaches iOS HealthKit without a watch app)](https://www.researchandcare.org/faq/)
- [Use your Apple Watch without your iPhone nearby — Apple Support](https://support.apple.com/en-us/108300)
- [Everything a GPS-only Apple Watch can do without an iPhone — iMore](https://www.imore.com/heres-what-apple-watch-can-do-without-iphone)
- [Choose an audio destination on Apple Watch — Apple Support](https://support.apple.com/guide/watch/choose-an-audio-destination-apd0110baf0e/watchos)
- [Play music on Apple Watch — Apple Support (speaker battery cost)](https://support.apple.com/guide/watch/play-music-apd70768b20b/watchos)
- [How do I ensure AVAudioPlayer plays through the physical speaker on Apple Watch? — Hacking with Swift](https://www.hackingwithswift.com/forums/watchos/how-do-i-ensure-avaudioplayer-plays-through-physical-speaker-on-apple-watch/8039)
- [Creating a Watch App that Supports Audio Recording — iOS Guru](https://medium.com/@ios_guru/creating-a-watch-app-that-supports-audio-recording-906af9806db0)
- [Using transferFile and sendMessage in Watch Connectivity SwiftUI — Bryan Vernanda](https://medium.com/@bryan.vernanda/using-transferfile-and-sendmessage-in-watch-connectivity-swiftui-edee23c69286)
- [NSLocalNetworkUsageDescription — Apple Developer Documentation](https://developer.apple.com/documentation/bundleresources/information-property-list/nslocalnetworkusagedescription)
- [iOS local network privacy permission explained — PTKD Journal](https://ptkd.com/journal/ios-local-network-privacy-permission)
- [Companion App Networking — Home Assistant Companion Docs](https://companion.home-assistant.io/docs/troubleshooting/networking/)
- [App Review Guidelines — Apple Developer](https://developer.apple.com/app-store/review/guidelines/)
- [Choosing a Membership — Apple Developer](https://developer.apple.com/support/compare-memberships/)
- [Sideloading your own iOS app without paying Apple $99/year — filipstachura.com](https://filipstachura.com/posts/ios-sideloading/)
