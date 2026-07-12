# wombat — future work roadmap (discussion notes)

**Status: discussion-only.** Nothing here is minted into `planning/contract.yaml`.
Active development is paused (operator decision, 2026-07-10) in favor of hands-on
tuning of the app as it currently stands. These notes capture direction agreed in
conversation so it isn't scattered across chat. Companion doc: `search-architecture.md`.

## The shape everything shares: one brain, many thin clients

wombat's runtime (Python + Postgres, the deterministic core) is the single "brain."
Every UI is a thin client that points at it — the Electron desktop app today, a phone
app and a watch later. The runtime decides; clients capture input and play output.
This is already true on the desktop (the Electron app talks to the runtime over
loopback); the future work is making that same relationship work over a network.

## The real enabler: a networked, hosted single-tenant runtime

Mobile/watch clients can't run the Python runtime or Postgres locally, and iOS kills
long-lived background processes — so the runtime must live on an always-on host and
the devices become remote clients. Concretely this needs:
- Real auth on the chat/settings surfaces (today's per-launch loopback token assumes
  same-machine trust; a remote client needs more).
- Reachability. Recommended: a private mesh (Tailscale/WireGuard) — keeps the
  local-first/residency posture intact, TLS handled, no public ports. Operator decision
  (2026-07-10): **plan on pushing to a live hosted service** so the phone/watch aren't
  limited to LAN range.
- Push notifications (APNs) so the morning brief reaches the phone when the app is closed.
- A TTS-return path: today wombat speaks on the host's speakers; a remote device needs
  the runtime to return synthesized audio to the device to play there.

This is single-tenant hosting (just the operator's own instance). It does NOT require
multi-user work — see below.

## Client family

- **Desktop (Electron): done.** Chat, settings, mic/audio controls, tokenized theme.
- **Phone (Ionic/Capacitor):** reuse the presentational React + the OKLCH design tokens
  (real reuse). Rewrite the platform glue — the Electron main/preload/IPC and the
  mic→drop-dir filesystem write become network calls + Capacitor native capabilities
  (mic, Keychain). Capacitor (the native bridge) is preferred over adopting Ionic's UI
  kit, to preserve the custom design system. Audio model is clip-based push-to-talk
  (record → POST → get spoken reply), which reuses the existing pipeline.
- **Watch:** companion to the phone (bluetooth/wifi range — leans on the phone, not
  independent), the easier watch architecture. Native SwiftUI + WatchConnectivity; no
  web-code reuse. Scope: tap-to-talk, glance at the brief, notification relay. Operator:
  **imperative for ease of access.**

## Desktop voice ergonomics: the "mic key"

The input pipeline exists (mic capture → WAV → drop-dir → Whisper → queue); what's
missing is a global activation affordance. Recommended: Electron `globalShortcut` +
a tray/background mode so the hotkey works with the window closed + a small hidden
renderer for capture + a visible "listening" indicator. Start with a **toggle** hotkey
(tap on/off — clean with the built-in API); true hold-to-talk needs a native key hook
(later). Governance note for whenever this is built: record an explicit posture — the
**mic opens only on an explicit keypress, never ambient, always with a visible
indicator**. Medium-small effort; mostly reuses the existing pipeline.

## OAuth for STT/TTS providers — low priority, verify feasibility first

Most TTS/STT developer APIs (Fish, Deepgram, likely ElevenLabs) are **API-key only** and
don't offer a consumer OAuth flow, so this may simply not be available — verify per
provider before investing. wombat already has a working OAuth flow (Google gcal/gmail)
if one ever needs it. For a single operator, OAuth's benefit over a working pasted key
is thin. **Operator priority: way down the list.**

## Multi-user — a rebuild, not a feature; lowest priority

wombat is single-tenant to the bone: one drainer process-wide (ASMP-2), no tenant key
on any table, one persona/voice/OAuth/keys, no notion of user identity. Genuine
multi-user is a near-total re-architecture (tenant-key the data model, per-tenant
drainers, real accounts + authZ, per-user isolation as a new threat model) — arguably a
different product. **Deliberately NOT pre-building it now** would be speculative scope
for a single user; the disciplined call is to stay cleanly single-tenant.

Key distinction: multiple **devices** for one user (phone + watch + desktop → one wombat)
needs zero multi-user work — that's just the thin-client family above. Multiple **users**
(other people, own wombats) is the rebuild. The clean "one runtime = one user" boundary
keeps both future doors open cheaply: "run N isolated instances behind a router" or a
real multi-tenant rebuild. Operator decision (2026-07-10): **single-tenant is fine for
the foreseeable future; "you own the deployment" is an acceptable long-term posture**
(and on-brand with the local-first/zero-analytics constitution). Below OAuth providers
on the priority list.

## Rough priority order (operator-agreed, 2026-07-10)

1. Hosted single-tenant runtime + auth/reachability hardening (the enabler for phone/watch)
2. Ionic/Capacitor phone client
3. Watch companion
4. Desktop mic key (can happen independently, anytime)
5. OAuth for STT/TTS providers (verify feasibility)
6. Multi-tenant rebuild — only if "other people use it" ever becomes a goal
7. Internet search (see `search-architecture.md`)
