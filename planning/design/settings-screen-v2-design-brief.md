# Settings screen v2 — design brief

Phase-1 deliverable (ux-designer agent, 2026-07-30). **Supersedes**
`settings-screen-design-brief.md` for the settings surface; the approved
iteration-3/4 SHELL (header / left rail / content / 320 px collapsible chat
pane, Today as landing), the 26 px slim-control + 16/24/32 spacing rhythm,
segmented chips, auto-save + restart strip, and write-only key rows all
**carry forward unchanged** — Jim approved them and nothing here moves them.
What changes: the settings surface grows from 4 thin categories to 5 intent
groups with real range, per Jim's ask: "the current settings are very
limited. I would love if we could spruce up the settings page with more
robust settings and greater range."

Contract with myself: the mock and the eventual implementation follow this
document; deviations get written back here with a one-line reason.

## 1. Research grounding (patterns, not vibes)

Carried forward from v1 (still binding): category sidebar for settings
(Setproduct/Toptal), segmented controls over dropdowns for ≤5 options
(NN/g), degraded = persistent banner + read-only legible values (Carbon/
PatternFly), auto-save per control (macOS/Slack). New for v2:

1. **Progressive disclosure is the mechanism for "greater range" without
   re-creating the big form** — essential settings upfront, an "Advanced"
   disclosure per category for the power knobs; consistent placement
   across every category so the pattern is learnable.
   ([NN/g — progressive disclosure](https://www.nngroup.com/articles/progressive-disclosure/),
   [UXPin — progressive disclosure](https://www.uxpin.com/studio/blog/what-is-progressive-disclosure/),
   [LogRocket — types & use cases](https://blog.logrocket.com/ux-design/progressive-disclosure-ux-types-use-cases/))
2. **AI-assistant personality settings converge on presets + trait
   segments + free-text "about you"** — ChatGPT's 2025/26 Personalization
   tab stacks three layers: personality presets (Default/Professional/
   Friendly/Candid/...), per-trait adjusters (warmth, enthusiasm) with a
   visible default position, and persistent custom instructions ("about
   you" + "how to respond"). Wombat's five-axis matrix already IS the
   trait layer; v2 adds the preset row and the about-you layer as designed
   homes (backend pending — see tiers).
   ([WebProNews — ChatGPT personality sliders](https://www.webpronews.com/chatgpt-unveils-personalization-presets-sliders-for-tailored-ai/),
   [TestDevLab — ChatGPT trait controls](https://www.testdevlab.com/blog/openai-rolls-out-chatgpt-enthusiasm-controls))
3. **Every tunable shows its default and offers reset** — the "default"
   tag from v1 grows into a per-control affordance: a muted "default" tag
   when unedited; an inline "reset" ghost affordance when edited. Advanced
   numeric knobs especially need this (a user who breaks their gate
   thresholds must have a one-click way home).

## 2. Full inventory — three tiers (nothing dropped silently)

Tier legend: **(a)** already editable in the app UI · **(b)** exists in
backend config but not exposed (needs admission/plumbing, no new concept)
· **(c)** doesn't exist yet — **requires new backend field/decision**; the
mock designs the home, the architect scopes the ticket. The design NEVER
renders a placebo: tier (b)/(c) controls appear in the mock as the target
state, and each board's caption names exactly which controls await backend.

### Persona & Conversation

| Setting | Today | Tier |
| --- | --- | --- |
| 5 persona axes (brevity/warmth/directness/humor/proactivity) | GET/PUT /settings, hot-apply | (a) |
| Assistant name (`wombat_assistant_name`) | GET/PUT, restart-tier | (a) |
| Conversational register (chat/voice looseness) | personability arc in design NOW | (c) — designed home |
| Your name (speaker-name awareness) | personability arc | (c) — designed home |
| About you (getting-to-know-Jim store, free text) | personability arc | (c) — designed home |
| Persona presets row (Steward/Professional/Friendly/Candid/Custom) | — | (c) |

### Voice & Audio

| Setting | Today | Tier |
| --- | --- | --- |
| Voice replies on/off (`wombat_voice_enabled`) | AudioPanel toggle | (a) |
| Push-to-talk binding (`wombat_ptt_binding`) | one-shot capture | (a) |
| Input device + Record/Mute mic test | AudioPanel (local, unpersisted) | (a) |
| STT provider / TTS provider | selects | (a) |
| TTS voice ID (`wombat_tts_voice_id`) | free text | (a) |
| Cloud STT model (`wombat_stt_model`) | API admits; UI omits | (a-admitted, unrendered) |
| Local transcription model (`wombat_asr_model`, tiny/base/small/medium) | env-only | (b) — needs APP_EDITABLE admission |
| Spoken-reply length cap (`_MAX_SPEECH_CHARS` 400) | pinned in speech_shape.py | (c) |
| Walkie-talkie reply window (`LAST_SPOKEN_TTL_SECONDS` 120) | pinned in reply_context.py | (c) |
| TTS voice PICKER (list provider voices, not a raw ID) | — | (c) — nice-to-have, raw ID stays |

### Briefs & Digests (new group)

| Setting | Today | Tier |
| --- | --- | --- |
| Morning brief time (07:00) | wombat_params.yaml; **TK-97 forbids runtime knob** | (c) — needs superseding decision + params bridge |
| Nightly reflection time (02:00) | wombat_params.yaml; TK-52 same posture | (c) — same, advanced |
| Interrupt aggressiveness preset (Quiet/Balanced/Chatty) | maps onto `urgency_threshold` (0.75) + personality band | (c) |
| Max interruptions per sender class per day (`per_class_daily_ceiling` 3) | wombat_params.yaml | (b/c) — params bridge |
| Item decay TTL (`decay_ttl_seconds` 24 h) | wombat_params.yaml, advanced | (b/c) — params bridge |
| Quiet hours (no voice interruptions HH:MM–HH:MM) | — | (c) |
| Brief content toggles (calendar / inbox / notepad sections) | — | (c) |

**Deliberately NOT user-tunable, said out loud:** the `rating_tuner` block
(LOCKED — TK-48 proved band+ceiling must move jointly; a UI knob here
breaks a proven bound), `load_flush_threshold`/`flush_min_age`/
`max_pending` (gate mechanics with no user-meaningful frame), presence
thresholds, sweeper cadence, dream budget internals.

### Accounts & Keys

| Setting | Today | Tier |
| --- | --- | --- |
| Google gmail/gcal connect + status | GET /google/status, POST connect | (a) |
| ElevenLabs / Deepgram / Fish API keys (write-only, keyring) | PUT /keys/{provider} | (a) |
| DeepSeek/Google-OAuth/DSN credentials | operator .env | excluded (DEC-32 posture stands) |

### System

| Setting | Today | Tier |
| --- | --- | --- |
| Restart wombat + outcome | RuntimeControls | (a) |
| Storage status line (settings + external stores) | derivable from GETs | (a) |
| Daily token ceiling (100 000) / per-conversation spend cap ($0.50) | wombat_params.yaml | (b/c) — params bridge, advanced |
| Model response wait (`mouth_model_timeout_seconds` 10 s) | wombat_params.yaml | (b/c) — params bridge, advanced |
| Data retention: external items 30 d, notepad 14 d | pinned constants | (c) — advanced |
| Timezone (read-only display, "America/New_York (system)") | resolve_wombat_zone | (c) — needs additive read-only API field (v1 open Q4, now designed in) |

Operator-tier fields stay excluded per standing decisions:
`wombat_timezone` (editing), `wombat_asr_drop_dir`, `wombat_brief_path`,
`wombat_chat_handshake_file`, `wombat_feedback_file`,
`wombat_singleton_port`, `wombat_pg_dsn`, poll intervals.

## 3. Top user tasks, ranked

1. **Tune personality/conversation feel** (most-revisited; hot-apply; the
   personability arc makes this the growth center).
2. **Control when/how wombat interrupts** (the new Briefs & Digests group —
   Jim's DEC-63/64 reports were both about interruption/conversation
   behavior; this is where "robust" pays rent).
3. **Voice setup & troubleshooting** (PTT, device, providers, mic test).
4. **Accounts/keys** (episodic, high-stakes).
5. **Restart / check system health** (whenever a restart-tier change lands).

## 4. Information hierarchy

**Primary (visible at a glance):** the settings rail group grows to five —
**Persona & Conversation · Voice & Audio · Briefs & Digests · Accounts &
Keys · System** (grouped by intent, not backend origin; still inside the
4–7 rail guidance). Degraded-storage banner outranks everything when
active. Each category opens with its non-control summary artifact (v1
pattern, kept): persona profile sentence; voice signal-chain readout;
briefs "next brief 07:00 · 3 interrupts today" line; keys configured-dots
row; system status line. Restart strip stays persistent app-wide.

**Secondary (one interaction):** TTS voice ID (only when TTS ≠ local);
missing-key inline warning with jump-link; per-control "reset to default";
restart outcome detail; quiet-hours time pickers (revealed by its toggle).

**Tertiary (behind the per-category Advanced disclosure — consistent
placement, always last in the category):** Voice → cloud STT model, local
transcription model, reply window, spoken-reply cap. Briefs → urgency
threshold (raw), item decay, nightly reflection time. System → budgets,
model wait, retention. Advanced controls always show their default value
and a reset affordance.

**Grouping rationale:** Persona & Conversation = "who it is and how it
talks" (all hot-apply persona plus the incoming register/about-you arc —
one home that grows). Voice & Audio = "the physical channel." Briefs &
Digests = "when it speaks up on its own" — the proactive-behavior dial
Jim keeps filing reports about, previously homeless. Accounts & Keys =
credentials + connections (keyring/OAuth semantics, degrade-immune, own
space). System = runtime lifecycle, health, budgets, retention.

## 5. Interaction notes

- **Auto-save per control** (approved v1 model, unchanged): segmented/
  toggle/stepper → immediate one-field PUT; text fields save on blur/Enter.
  Hot-apply fields confirm "Saved — applies next turn"; restart-tier fields
  feed the persistent restart strip. Params-bridge fields (if/when built)
  ride the same idiom — one write path from the user's seat.
- **Default honesty + reset:** unedited = muted "default" tag; edited =
  value shown plain with a small "reset" ghost affordance. Advanced numerics
  render as compact steppers/inputs with unit labels ("s", "chars", "/day"),
  never bare text boxes.
- **Tier honesty in the mock:** tier (b)/(c) controls are drawn as the
  target state; each board caption names which await backend. Nothing in
  the implementation ships a dead control — Phase 2 builds only what the
  architect has minted fields for, and this brief's tier table is the
  scoping source.
- **Aggressiveness preset ↔ raw values:** the Quiet/Balanced/Chatty
  segmented control writes a preset; the Advanced raw threshold shows the
  effective value and flips the preset to "Custom" if edited directly (the
  ChatGPT preset/slider layering).
- **Restart strip, key rows, PTT capture, mic test:** all v1-approved
  behaviors verbatim.

### Degraded / empty / loading states (first-class)

1. **Loading:** rail immediate; content "Loading settings…" plain line.
2. **`storage_unavailable: true`:** persistent banner — "Settings storage
   is unreachable — showing defaults, not your saved values. Read-only
   until it's back." + [Retry]. All settings read-only but legible.
   **Accounts & Keys stays fully writable** (keyring + OAuth are not the
   pg store) — banner's second line says so. **Restart stays available.**
3. **PUT 503:** control reverts, inline "Couldn't save — settings storage
   unavailable," GET re-checked to raise the banner.
4. **GET rejects:** full-content error panel + [Retry]; no half-form.
5. **Voice drop-dir unconfigured / no input devices:** v1 postures verbatim.
6. **Advanced params not yet bridged:** if Phase 2 lands before a params
   bridge, the affected Advanced rows simply don't render (tier honesty) —
   never a disabled placebo row.

### Design-system compliance

Tokens only (`theme.css` names via `tokens.ts`) — zero hardcoded colors;
existing Panel/Button/Field/Select/Indicator components plus the v1-scoped
segmented control; new primitives needed: compact stepper, toggle switch,
time field, disclosure — all consuming existing tokens; lucide icons;
no gradients; animation allowlist unchanged. Mock rendered in neutral
grays through `:root` tokens named exactly like `theme.css` (Jim owns
values).

## 6. Open questions for Jim (taste only)

1. **Interrupt aggressiveness: preset-first or numbers-first?** Mock shows
   Quiet/Balanced/Chatty chips with raw threshold under Advanced (preset
   writes the numbers; direct edit flips to "Custom"). Say the word to
   surface raw numbers instead.
2. **Brief time as an in-app setting?** TK-97/TK-52 deliberately made
   brief/reflection times file-edit-only. The mock designs the control;
   exposing it needs a superseding decision. Want it user-tunable, or is
   file-edit fine for something you set once?
3. **Persona presets row** (Steward/Professional/Friendly/Candid/Custom
   above the five axes, ChatGPT-style) — include, or are the five axes
   alone the right amount of control?
4. **Fifth rail item vs. folding:** mock adds "Briefs & Digests" as its own
   rail entry (it's task #2). Alternative: fold into System. One word flips.

## 7. Mock

`planning/design/settings-screen-v2-mock.html` — six neutral-gray interface
boards, all colors via `:root` tokens named like `theme.css`; captions
below each board carry tier notes (no meta on canvas): 1 Persona &
Conversation · 2 Voice & Audio (Advanced open) · 3 Briefs & Digests ·
4 Accounts & Keys · 5 System · 6 States (storage-unavailable read-only,
failed save, loading). Orchestrator transcribes to Lucid.

## 8. Deviation log

(Empty — append during Phase 2 with one-line reasons.)
