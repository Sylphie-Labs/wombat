# wombat DeviceSurface — payload-level wire contract v1

> **Status: LOCKED by DEC-83 (contract v2.219).** This document is the *payload-level*
> completion of DEC-78(c), which locked the route **set** but not the field names, units,
> enum vocabularies, encodings or framing. Both halves of FEAT-15 read this file and only
> this file: the Python side (EP-39/EP-40, TK-339..TK-345) and the Swift side
> (EP-42/EP-43, TK-355..TK-360, mirrored byte-for-byte into `ios/.../WireContract.swift`).
>
> **Changing anything here is a contract edit, not an implementation choice.** Adding a
> sixth route is a new decision (DEC-78(c)). Adding a field, renaming one, or widening an
> enum is an amendment recorded in the contract changelog.

---

## 0. Conventions that apply everywhere

| Rule | Value |
|---|---|
| Transport | Plaintext **HTTP/1.1** over the LAN. **No TLS** (DEC-78(a)); see §7 for the iOS/watchOS consequence. |
| Auth header | `X-Wombat-Device-Token: <plaintext token>` — inherits the shipped `X-Wombat-Chat-Token` / `X-Wombat-Token` convention (source-verified), never `Authorization: Bearer`. |
| Auth failure | **`401`** with body `{"error":"unauthorized"}` on **every** path including unknown ones (DEC-78(b) anti-enumeration; byte-identical to `chat/surface.py`). A `404` is never produced for an unauthenticated request. |
| Path prefix | `/v1/` on every route. A version bump is a new prefix, never a silent reshape. |
| JSON envelope | Every JSON request and response body carries `"v": 1` as its first key. |
| Timestamps | ISO-8601 **with an explicit UTC offset or `Z`**. A naive timestamp is a `400`. |
| Numeric field naming | **Every numeric field name carries its unit as a suffix** — `_bpm`, `_ms`, `_minutes`, `_seconds`, `_kcal`, `_meters`, `_hz`, `_steps`. A unitless numeric name is a spec violation. This is the anti-drift rule; it makes a unit mismatch a *review* failure rather than a *runtime* one. |
| Audio format | Raw **`pcm_s16le`, mono, 44100 Hz** — the value of `voice/stream_playback.py::STREAM_SAMPLE_RATE`, read from that ONE constant and never re-declared. **No RIFF/WAV header** on any reply path (DEC-73(d), DEC-79(a)). |
| Size caps | Pinned per route (§2, §3). Over-cap bodies are rejected **without being read into memory in full**. |

### 0.1 The client-side result trichotomy (required)

Every device-side call site MUST distinguish **three** outcomes, never two:

1. **`unreachable`** — DNS/connect/timeout/TLS-layer failure. wombat is off, asleep, or off-LAN. *Retryable, transient, "wombat is not here right now".*
2. **`unauthorized`** — an HTTP **401**. The token was revoked (TK-338 `revoke`) or wombat's keyring was re-minted. **Not retryable.** The device must surface **"re-pair this device"**, never a spinner.
3. **`ok` / other 4xx / 5xx** — a real answer.

Collapsing 1 and 2 into "offline" is the defect this clause exists to prevent: a revoked watch would retry forever and the operator would never learn why.

---

## 1. Route table (the closed set — DEC-78(c))

| # | Method | Path | Owner ticket | Purpose |
|---|---|---|---|---|
| 1 | `GET` | `/v1/health` | TK-339 | Authenticated liveness **and the format handshake** (§4). |
| 2 | `POST` | `/v1/voice` | TK-340 | One captured utterance, audio bytes (§2). |
| 3 | `POST` | `/v1/biometrics` | TK-341 | One batch of closed-projection samples (§3). |
| 4 | `GET` | `/v1/utterance` | TK-343 | Pull the sealed reply PCM (§5). |
| 5 | `GET` (Upgrade) | `/v1/stream` | TK-345 | Optional phone WebSocket fast path (§6). |

There is no sixth route.

---

## 2. `POST /v1/voice` — audio ingest

Deliberately **not** multipart: the stdlib transport should not grow a multipart parser. The
audio is the raw body; the metadata rides headers.

**Request**

```
POST /v1/voice HTTP/1.1
X-Wombat-Device-Token: <token>
Content-Type: audio/wav
X-Wombat-Captured-At: 2026-08-03T07:12:04-05:00
Content-Length: <n>

<raw audio bytes — the exact file ASRSource scans for>
```

- `X-Wombat-Captured-At` is **required**. Missing or naive ⇒ `400`.
- Staleness: `now - captured_at > stale_audio_window_seconds` (§4) ⇒ **`409`** with
  `{"v":1,"error":"stale_audio","stale_audio_window_seconds":120}`. Nothing is written to
  the drop dir. (DEC-78(i): refuse stale audio, never deliver it late.)
- Body size cap: **10 MiB**.
- Content is validated by extension **and** magic-byte sniff against the set `ASRSource` scans.

**Response `202`**

```json
{"v":1,"accepted":true,"utterance_id":"018f...-uuid","device_id":"<the authenticated device>"}
```

`utterance_id` is minted **server-side at accept**, written into the `LastTurnOriginRegister`
alongside the origin `device_id` (TK-343), and echoed on §5 and §6. It is the **correlation
handle** that lets a device tell its own reply from a DEC-79(c) cross-device fallback reply.

**Idempotency**: none is client-supplied. The event key is `source_id='asr_remote'` + the
sha256 of the audio bytes, which `ASRSource` already computes (TK-340). A duplicate POST
returns `202` with the **same** `utterance_id`.

---

## 3. `POST /v1/biometrics` — closed-projection batch ingest

**Request**

```json
{
  "v": 1,
  "samples": [
    {
      "kind": "resting_hr_daily",
      "started_at": "2026-08-03T00:00:00-05:00",
      "ended_at":   "2026-08-03T23:59:59-05:00",
      "payload": { "bpm": 54 }
    }
  ]
}
```

- Batch cap: **500 samples**, body cap **1 MiB**.
- `kind` ∈ the closed v1 set (DEC-80(a)). `started_at` ≤ `ended_at`, both required.
- `payload` keys are **exactly** the per-kind schema below — no extra key, no missing
  required key, no `null` where a number is declared.
- **ANY** violation rejects the **WHOLE batch** with `400` and writes ZERO rows (TK-341).
  Partial acceptance is impossible.
- **No free text may appear anywhere in the request body.** Every string field in this spec
  is either an ISO timestamp or a member of a fixed enum. There is no field into which a
  HealthKit source-app name, device name, workout title or note can be placed (DEC-80(b)).

### 3.1 Per-kind closed schemas

| kind | payload fields | plausible range (out-of-range ⇒ `400`) |
|---|---|---|
| `sleep_session` | `asleep_minutes` int, `in_bed_minutes` int, `awakenings` int | `0..1440`, `0..1440`, `0..200` |
| `workout` | `activity` enum, `duration_seconds` int, `active_energy_kcal` number, `avg_hr_bpm` int?, `max_hr_bpm` int?, `distance_meters` number? | `1..86400`, `0..20000`, `20..250`, `20..250`, `0..500000` |
| `resting_hr_daily` | `bpm` int | `20..250` |
| `hrv_daily` | `sdnn_ms` number | `1..500` |
| `steps_hourly` | `steps` int | `0..100000` |

`?` = nullable **only** where marked; a nullable field may be omitted entirely or sent as
`null`, never as an empty string.

`NaN` and `Infinity` are rejected (they are not valid JSON numbers regardless).

### 3.2 `activity` enum (closed)

`walking` · `running` · `cycling` · `strength` · `swimming` · `hiit` · `yoga` · `other`

`other` is the deliberate catch-all that keeps free text out: an unmapped `HKWorkoutActivityType`
projects to `other`, it does **not** project to its Apple name.

### 3.3 Idempotency — **server-derived, no client field**

The per-sample idempotency key is
`sha256(kind || '\x1f' || started_at_utc_iso || '\x1f' || ended_at_utc_iso || '\x1f' || canonical_json(payload))`
where `canonical_json` sorts keys and uses no whitespace.

**Rationale, recorded:** a client-supplied `sample_uid` would be an opaque device-trusted
string on a wire whose entire thesis is that nothing opaque crosses it. A server-derived key
needs no new field, cannot be spoofed, and makes an offline phone's re-drain idempotent for
free — the same sample projects to the same bytes.

**Accepted tradeoff, named:** if HealthKit *revises* a sample's value, the projection changes
and a second row appears. That is rare, and a revised measurement is arguably a genuinely new
observation on an append-only ledger. The phone's offline buffer therefore MUST store the
**projected payload bytes**, not the raw `HKSample`, so a redelivery re-projects identically
(TK-357).

**Response `202`**: `{"v":1,"accepted":<int>,"deduplicated":<int>}`

---

## 4. `GET /v1/health` — liveness **and the format handshake**

This route is where the format handshake rides. A device calls it at pair time and on every
reconnect, and caches nothing across a wombat restart.

**Response `200`**

```json
{
  "v": 1,
  "ok": true,
  "device_id": "<the authenticated device>",
  "audio": { "sample_rate_hz": 44100, "format": "pcm_s16le", "channels": 1 },
  "stale_audio_window_seconds": 120,
  "utterance_ttl_seconds": 120,
  "capabilities": { "remote_voice": true, "biometrics": false, "stream": true }
}
```

- `audio.sample_rate_hz` is read from `STREAM_SAMPLE_RATE` — **the same constant the Fish
  request reads**, never a second literal.
- `stale_audio_window_seconds` is the §2 refusal window. **Devices read it here.** A device
  MUST NOT hold its own copy of this number (this is the drift TK-359 exists to prevent).
- `capabilities` reflects the two DEC-78(d) consent toggles as they are actually constructed,
  so a device can say "biometrics are off on wombat" instead of POSTing into a `404`.

---

## 5. `GET /v1/utterance` — pull the sealed reply

**Request**: `GET /v1/utterance` with the device token. No body, no query.

**Response `200`** — headers carry the metadata, body is raw PCM:

```
HTTP/1.1 200 OK
Content-Type: application/octet-stream
X-Wombat-Utterance-Id: 018f...-uuid
X-Wombat-Origin-Device-Id: <device that originated the turn>
X-Wombat-Sample-Rate-Hz: 44100
X-Wombat-Audio-Format: pcm_s16le
X-Wombat-Channels: 1

<raw pcm_s16le bytes, no RIFF header>
```

**Response `204`** — nothing sealed and pending. This is the *ordinary* answer, not an error.

**Single-fetch-then-discard** (TK-343): a successful `200` discards the slot; an immediate
repeat gets `204`. An unfetched utterance expires at `utterance_ttl_seconds`.

**`X-Wombat-Origin-Device-Id` — RETAINED, with its case corrected by DEC-90.** This header was
introduced to disambiguate DEC-79(c)'s cross-device fall-through, where a phone-originated turn
falls through to the watch buffer when no phone session is open. **Under DEC-90 exactly one
device holds a token and exactly one device ever fetches, so that mismatch is currently
unreachable** — there is no second fetching device to confuse.

The header **stays on the wire** and is not struck. Three reasons, ruled at DEC-90(f): it is
already emitted by shipped, done Python (TK-343/TK-339) and removing it would be a wire change
to working code for no gain; it remains the correlation handle pairing a fetched utterance to
the turn that produced it; and the phone still needs it to **name a relayed reply correctly on
the wrist** when it forwards bytes over WatchConnectivity.

The fetching device SHOULD still compare `X-Wombat-Origin-Device-Id` to its own `device_id`
(from §4) and present a mismatch as a cross-device reply rather than as an answer to something
it said. That branch is defensive, not live.

**The privacy property, stated as what is actually enforceable:** wombat has **no push path**
to any device — every byte a device plays was pulled by that device. And wombat only ever
*seals* an utterance that is a reply to a remote-originated turn: brief, draft, reflection and
every proactive surfacing stay local (DEC-79(c)/(d), pinned by TK-343). Those two facts
together are the guarantee. "Only a turn this device originated" is **not** the guarantee and
must not be written as one.

---

## 6. `GET /v1/stream` (WebSocket upgrade) — the phone fast path

Optional (TK-345, P3). If the hand-rolled framing exceeds budget, this route does not ship and
nothing else changes.

- **Upgrade**: RFC 6455, `Sec-WebSocket-Protocol: wombat.audio.v1`. The **`X-Wombat-Device-Token`
  header rides the upgrade request** (`URLSessionWebSocketTask` can set request headers). No
  token in the query string, ever — query strings land in logs.
- **The phone opens the socket. wombat never dials.** (DEC-78(e), NG-10.)
- **Framing**, per utterance:
  1. one **TEXT** frame: `{"v":1,"event":"utterance_start","utterance_id":"...","origin_device_id":"...","sample_rate_hz":44100,"format":"pcm_s16le","channels":1}`
  2. N **BINARY** frames: raw `pcm_s16le` chunks, **whole 2-byte frames only** (frame discipline is inherited from `StreamingAudioWriter`, never re-implemented).
  3. one **TEXT** frame: `{"v":1,"event":"utterance_end","utterance_id":"..."}`
- A socket that closes between `utterance_start` and `utterance_end` is a **partial** reply
  (`played_any=True` on the wombat side, DEC-79(g)). There is no resume.
- wombat sends nothing on this socket except frames belonging to a reply to a remote-originated
  turn. Idle keepalive is standard WS ping/pong only.

---

## 7. iOS App Transport Security — **required (iOS target only)**

> **AMENDED BY DEC-90.** This section previously required the ATS exception on **both** the iOS
> and watchOS targets. The watchOS half is now **MOOT**: the watch makes no network call to
> wombat at all — it reaches wombat only by relaying through the phone over WatchConnectivity,
> which needs no ATS exception and no `Info.plist` networking keys. Declaring it on the watch
> target is not harmful, merely meaningless; the requirement is **iOS-target-only**.

wombat's DeviceSurface is **plaintext HTTP/1.1 with no TLS** (DEC-78(a)), and the phone speaks
`http://` and `ws://` to a LAN address. **ATS blocks that by default.** The iOS target MUST
declare, in `Info.plist`:

```xml
<key>NSAppTransportSecurity</key>
<dict>
  <key>NSAllowsLocalNetworking</key>
  <true/>
</dict>
```

- `NSAllowsLocalNetworking` is the **narrow** key — it permits cleartext to link-local, `.local`
  and private-range addresses **only**. `NSAllowsArbitraryLoads` is **forbidden** in this tree:
  it would grant cleartext to the whole internet to solve a LAN problem.
- `NSLocalNetworkUsageDescription` is a **separate, also-required** key (the iOS 14+ local
  network permission prompt). Both are needed; neither substitutes for the other.
- Declaring these is text and costs nothing (DEC-82(a) tier A). Whether the resulting build is
  actually permitted the cleartext connection is a **tier B** observation — the first Mac
  session confirms it, and `ios/README.md` says so.
- **The watchOS target needs neither key.** WatchConnectivity is not a network transport in the
  ATS sense, and after DEC-90 the watch has no URL to reach.

---

## 8. Pairing QR payload (TK-342 mints it, TK-355 parses it)

The QR encodes **exactly** this UTF-8 JSON, one line, no whitespace:

```json
{"v":1,"host":"192.168.1.42","port":8788,"token":"<base64url, >=32 bytes entropy>","name":"iphone"}
```

- `host` is an IPv4 literal or a hostname. `port` is the DEC-78(a) **fixed configured** port.
- `token` is the **plaintext** per-device token, shown exactly once (TK-342), written straight
  to the Keychain and never to `UserDefaults`, a plist, a log or a source constant (TK-355).
- `name` is the operator-chosen device label, echoed for confirmation only. It is **not** sent
  on any request.
- A QR whose `v` is not `1` is rejected with a plain "this pairing code is from a different
  version of wombat" — not a crash, not a silent partial parse.

> **AMENDED BY DEC-90 — the one-shot token handoff to the watch is DELETED, not deferred.**
> Exactly **one** QR is ever scanned and exactly **one** device token ever exists: the phone's.
> The watch is never a caller, so it is never a token holder — a credential it could never
> present would be a live secret copied to a second Keychain, outside the DEC-75 wipe's reach,
> with a second revocation path and a standing invitation to drift back into watch-direct.
> `WCSession` carries **audio and state**, never a credential.

---

## 9. What is deliberately NOT in this spec

- **Discovery.** No Bonjour/mDNS. Host and port come from the QR (§8) and nowhere else.
- **Token rotation, expiry, refresh or scopes.** A paired device is paired until revoked (TK-338).
- **Any read/config/control/admin/wipe route.** The set in §1 is closed.
- **Any free-text field on any route.** Adding one is a DEC-80(b) reopening, not a field addition.
