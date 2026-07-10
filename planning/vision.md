# Product Vision & Feature List — wombat ("The Steward")

> Source: `pa-agent-profile.md` + `pa-agent-features.md`. wombat is a personal-assistant
> agent built on the **cog-worx** cognitive-architecture framework (the loop, memory,
> verification, perception, durability — out of the box).

## Problem

Every personal-assistant agent on the market fails the same way: it talks too much. It
delegates the "is this worth interrupting you?" judgment to an LLM, so it becomes a
notification firehose with a nicer voice — untrustworthy, noisy, and quick to be muted.
Self-hosted assistants additionally carry a poor security/privacy reputation. There is no
quiet, trustworthy assistant that owns detection and decision in deterministic code and
keeps the user's data on the user's own host.

## Outcome

A personal assistant ("the Steward") that runs the user's digital estate quietly in the
background and only speaks when it has cleared a deterministic bar — so when it does speak,
it is trusted on sight. The user stops thinking about inbox/calendar maintenance; overnight
work is done by morning; the one nudge that lands is load-bearing, not noise. Understanding
of the user compounds week over week instead of resetting each session. Data stays local;
nothing side-effectful happens off a reviewable trail.

## Target users

- Individuals running an always-on local host (Jetson / small ARM box / VPS) who want a
  private, self-hosted personal assistant.
- Knowledge workers whose calendar + inbox are the daily wedge and who are repelled by
  chatty/over-eager assistants.
- Privacy-conscious users who require local data residency and an auditable action trail.

## Measurable goals

- **Quiet:** the agent stays silent by default; interruptions are the rare earned exception
  (target a hard ceiling on unsolicited surfacings per day, tuned per event class).
- **Trusted on sight:** every surfaced item cleared the deterministic gate (relevance ∧
  importance ∧ user-state ∧ confidence) — 0 LLM-decided interruptions.
- **Morning brief:** one consolidated brief delivered once per morning (not a stream),
  covering overnight triage, conflicts-with-alternatives, and prep notes.
- **Auditable:** 100% of side-effectful actions recorded in a human-readable trail; 0
  unattended sensitive-auth actions (credentials/payments/irreversible handed to the user).
- **Compounding:** the empirical user model sharpens over time via the nightly consolidation
  pass (week-over-week, not per-session reset).

## Principles / constitution (the throughline)

- MUST: the deterministic core owns detection, decision, and integrations. The LLM is a
  **mouth** — called only to render pre-decided output into natural language, never to make a
  runtime decision. (Aligns with cog-worx S9: structure over prompting; never trust the
  model's self-report.)
- MUST: silence is the default; speaking is the exception the agent has to earn.
- MUST: every side-effectful action is logged in a reviewable, human-readable trail.
- MUST: the user model is **purely empirical** — observed behavior + outcomes only.
- MUST: review-before-send for high-consequence outbound; hand off cleanly to the human for
  auth / ambiguous / irreversible decisions.
- MUST: data stays on the user's own host by default.
- MUST: build on cog-worx; port/reuse its loop, memory, durability rather than reinvent.
- NEVER: infer or store *why* the user does something (no motive, no psychological
  profiling, no causal story). Model *what* they do and *what works*.
- NEVER: recite the psychology KB at the user, diagnose, or use a therapy/clinical framing.
  It is internal decision-support scaffolding only.
- NEVER: perform unattended sensitive auth (passkeys/hardware keys/payments).
- PREFER: a cheap model (Haiku-class or local small model) for the mouth — determinism is
  the moat, not any one model.
- PREFER: native, deep integrations (Gmail/Calendar APIs directly) over shallow,
  Zapier-style breadth. Depth is the product; count is marketing.

## Non-goals (the over-engineering guard)

- No theory of motive / no "why." Inferred causes, psychological profiles, and motive are
  not surfaced, not stored, not used by the gate.
- No clinical/therapy function. Decision-support scaffolding only; no diagnosis.
- No dashboards, no nagging. One load-bearing observation over many; no analytics UI.
- No LLM-in-the-loop for runtime decisions. The model never decides whether/when to act.
- No unattended credential entry / sensitive auth.
- No shallow breadth-first integrations in v1 — depth on the proven wedge first.
- No cloud-first data storage — local-first by default.
- (To confirm) voice/ASR/TTS is optional/configurable, not a v1 requirement.

## Constraints

- Runs on an always-on local host; must survive laptop sleep (watch overnight).
- Local-first data residency by default.
- Cheap-model budget for the mouth; the deterministic core does not spend tokens to decide.
- Built on cog-worx (Python 3.13, polyglot substrate: Neo4j + Postgres[pgvector+TimescaleDB]
  + OpenTelemetry); model-agnostic via cog-worx's S4 `Model` seam.
- Browser automation favors accessibility-tree (Playwright) over screenshot computer-use for
  reliability + token efficiency.

## Feature list

### Core engine
- Deterministic event loop — continuous; owns all polling and detection; never calls the LLM to decide what to do.
- Event filter / interruption gate — the central component; decides in code whether an event is worth surfacing; tunable thresholds per event class.
- LLM-as-mouth invocation — model called only to phrase pre-decided output (cheap/local model sufficient).
- Reviewable action trail — every action logged in a human-readable, auditable record.
- Local-first operation — always-on local host; survives laptop sleep to watch overnight.
- Graceful human handoff — recognizes tasks needing the user (auth, ambiguity, irreversible) and hands off cleanly.

### Memory & user model
- Hybrid memory store — vectors (fuzzy recall) + graph (relationships) + key-value (facts).
- Tiered memory management — active working context / recent-recall / long-term archival; explicit movement between tiers.
- Automatic fact extraction — durable facts pulled from interactions/events without manual tagging.
- Empirical user model — observed behavioral regularities + outcome tracking + preference capture; never motive.
- Offline consolidation ("dreaming") — overnight batch pass: review logs/transcripts, extract patterns, merge/dedupe, surface insights.
- Compounding understanding — the model sharpens week over week instead of resetting each session.

### Calendar management
- Native calendar integration (Google Calendar etc.) — deep, not shallow.
- Conflict detection — flags overlapping/impossible scheduling.
- Conflict resolution suggestions — concrete alternatives, not just alerts.
- Schedule realism checks — flags overpacked calendars / recurring blocks that never happen.
- Adherence tracking — which blocks are kept vs. repeatedly moved.
- Smart scheduling — places tasks against observed productive windows / energy patterns.
- Reminder surfacing — time/deadline/context-aware reminders, gated by the interruption filter.

### Email management
- Native email integration (Gmail/Outlook API).
- Overnight triage — sort/prioritize/categorize incoming mail while the user sleeps.
- Draft replies — pre-written in the user's voice, held for review.
- Voice matching — learns and mirrors the user's writing style.
- Task extraction — action items/commitments pulled from threads.
- Priority inference — what needs the user vs. what can wait/be handled.
- Batching — group related messages by context, not one alert each.

### Morning brief (flagship ritual)
- Daily summary — what's on today, delivered once, cleanly.
- Overnight work recap — triage done, drafts waiting, tasks extracted.
- Conflicts surfaced — calendar problems flagged with alternatives already attached.
- Prep notes — context pre-gathered for the day's meetings/tasks.
- Single delivery — one consolidated brief, not a stream of pings.

### Browser / computer use
- Agent-browser automation (default) — Playwright / accessibility-tree control; reliable, token-efficient.
- Session-aware operation — operate the user's existing logged-in sessions where supported.
- Visual computer use (niche) — screenshot-driven control reserved for genuinely visual tasks.
- Web lookups & research — fetch/check/retrieve on the user's behalf.
- Form filling & routine web tasks.
- Login handoff — defer passkey/hardware-key/sensitive auth to the user; no unattended credential entry.

### Behavior & efficiency analysis (motivational layer)
- Event logging — focus blocks, task deferrals, calendar adherence, response latencies, activity rhythms.
- Pattern detection — via the offline consolidation pass, not live token spend.
- Productivity-window modeling — learns when the user is sharpest; protects/schedules around it.
- Honest pattern reflection — surfaces one factual observation when it matters; never diagnoses or asserts cause.
- Single-observation discipline — no dashboards; one load-bearing nudge over many.

### Interruption calculus (the hard part, solved in code)
- Multi-gate filtering — relevance, importance threshold, user-state, confidence all checked before any interruption.
- User-state sensing — cheap local signals (present/at-desk, active/idle, in-a-call) determine *when* a nudge can land.
- Task-boundary timing — intervene at natural breaks, not mid-flow.
- Notification batching — related items grouped by context and priority.
- Silence as default.

### Motivational layer (psychology KB)
- Preloaded psychology KB — curated, evidence-based behavioral levers used internally only (implementation intentions; temptation bundling / Premack; behavioral-activation scheduling; motivational-interviewing phrasing; self-determination theory).
- Lever matching — match observed pattern → likely-effective technique, then record whether it worked.
- Autonomy-preserving by design — user's stated preferences override generic theory every time.
- Avoidance-dip support — apply activation-style structure, not pressure.
- Not clinical — decision-support scaffolding only.

### Privacy & safety
- Data stays local (user's own host by default).
- No action without a trail.
- Review-before-send for high-consequence outbound.
- No unattended sensitive auth (credentials, payments, irreversible).
- Privacy as character, not a toggle.

### Voice & interaction (optional / configurable)
- Always-on listening (optional) — local wake-word / streaming ASR; or push-to-talk.
- Local or cloud TTS — speaks only on rare speak-events; cloud for quality, local for privacy.
- Terse, load-bearing output — no filler, no performance, no credit-seeking.
- Voiced or text-only — full capability without speech.

### Voice providers & configuration surface (added 2026-07-09, Jim's direction)
- STT and TTS are **pluggable provider slots**; the defaults are **local and free** —
  faster-whisper (STT) and pyttsx3 (TTS) — so zero-config wombat stays fully offline.
- Integrated cloud options at launch: **ElevenLabs** (TTS flagship + Scribe STT),
  **Deepgram** (STT flagship + Aura TTS), **Fish Audio** (TTS + STT). Cloud voice is
  **per-provider user opt-in** (chosen in config AND keyed with the user's own API key);
  the default configuration keeps exactly one egress (the DeepSeek mouth). Degrade is
  strictly cloud → local (DEC-28).
- The assistant's **name is user-configurable** (default: "the Steward").
- Configuration happens in a local **Electron + React companion app** over a
  loopback-only Python settings API — intended to grow into wombat's front-end
  (future companion/avatar surface, e.g. a rendered 3D assistant) (DEC-31). In v1 it is
  the configuration surface only: no analytics (DEC-29), no accounts, no packaging or
  installer (DEC-30). Secrets live in the OS keyring; non-secrets in a local settings
  file the operator's `.env` always overrides (DEC-32).
- This is the first **distribution seed** — people other than Jim configuring their own
  wombat — honored in v1 as the config surface only (DEC-30).

### Personality matrix (added 2026-07-09, Jim's direction)
- The personality is **tunable** — "a way to tune the personality so people can have fun
  with it." Name + voice + services + personality = the user-facing identity kit.
- Jim's verbatim framing: **"observant, learns to fit in the right way."**
- Five closed axes (brevity, warmth, directness, humor, proactivity) as named levels —
  never free-text personas; the default IS today's quiet steward, byte-for-byte.
  Personality modulates expression WITHIN the locked frame: the deterministic gate still
  owns whether/when anything surfaces (proactivity only shifts the gate's willingness
  inside a recorded, human-edited band in `wombat_params.yaml` — never a new surfacing,
  never a raised cap), and the constitution bars (no motive talk, no clinical register,
  no nagging, no autonomy creep) are immutable at every setting (DEC-33..37).
- Tunable by voice ("be warmer") through a closed deterministic grammar with a one-line
  deterministic acknowledgment, by the settings app (hot-applied), and — slowly, visibly,
  reversibly — by explicit feedback ("too chatty") folded in nightly; an explicit choice
  always outranks learning (DEC-35/36, DEF-8).

## Open decision (shapes everything downstream)

Define what **"necessary"** means concretely for the first integration (calendar or inbox).
The whole persona hangs on this gate.
