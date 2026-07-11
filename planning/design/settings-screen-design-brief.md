# Settings screen — design brief

> **Iteration 2 (2026-07-11) supersedes parts of this document.** Sections 1–6
> below are the iteration-1 record, kept for history. Jim rejected the
> iteration-1 mock; the binding design is now **§9 Iteration 2** (which widens
> scope from "settings screen" to the app shell). Where §9 conflicts with
> §§1–6, §9 wins. What §9 does NOT touch (inventory, no-placebo exclusions,
> degraded-state semantics, auto-save model, keyring rows) carries forward
> unchanged.

Phase-1 deliverable (ux-designer agent, 2026-07-11). Contract with myself: the
Lucid mock and the eventual implementation follow this document; deviations get
written back here with a one-line reason. Designed against the **post-DEC-43
API** (TK-242): `GET /settings` may return `storage_unavailable: true`;
`PUT /settings` returns 503 when storage is down. Key-vault routes
(`PUT /keys/{provider}`, keyring-backed) are unaffected by the pg degrade —
that asymmetry is designed in, not glossed.

## 1. Research grounding (patterns, not vibes)

1. **Category sidebar for settings** — the desktop convention (macOS System
   Settings, Slack Preferences, Linear settings): a fixed left rail of 4–7
   task-named categories keeps orientation visible while editing. Guidance:
   group by how the user thinks, not how the backend is built; 4–5 top-level
   categories. ([Setproduct — settings UI](https://www.setproduct.com/blog/settings-ui-design),
   [Toptal — settings UX](https://www.toptal.com/designers/ux/settings-ux),
   [Eleken — navigation patterns](https://www.eleken.co/blog-posts/ux-navigation-design))
2. **Segmented controls beat dropdowns for ≤5 mutually-exclusive options**
   (NN/g dropdown guidance; Mobbin/Eleken segmented-control guides). The five
   persona axes have 2–3 levels each — hiding them in `<select>`s (the current
   form) is the canonical dropdown misuse. All options visible at once =
   the personality becomes *scannable as a profile*, not nine clicks of
   discovery. ([NN/g — dropdowns](https://www.nngroup.com/articles/drop-down-menus/),
   [Mobbin — segmented control](https://mobbin.com/glossary/segmented-control))
3. **Degraded = persistent banner + read-only fields, not disabled-and-mute**
   (Carbon read-only-states pattern; PatternFly banner; web.dev offline UX;
   the Google-Docs offline banner). Values stay legible, the banner says what
   still works, and a Retry affordance exists.
   ([Carbon — read-only states](https://carbondesignsystem.com/patterns/read-only-states-pattern/),
   [PatternFly — banner](https://www.patternfly.org/components/banner/design-guidelines/),
   [web.dev — offline UX](https://web.dev/articles/offline-ux-design-guidelines))
4. **Save-on-change with inline confirmation** (macOS System Settings, Slack
   preferences): settings screens with per-control effects don't hold a page
   of dirty state hostage to one Save button at the bottom. Wombat's fields
   already split into hot-apply (persona) vs restart-tier (DEC-32/DEC-37);
   auto-save per control plus ONE persistent "restart pending" strip is more
   truthful than the current whole-form Save that fires both notices at once.

## 2. Full inventory (nothing dropped silently)

Information the screen must show:

| Item | Source | Tier |
| --- | --- | --- |
| `wombat_assistant_name` | GET/PUT /settings | restart-tier |
| `wombat_persona_brevity` (terse/balanced/expansive) | GET/PUT | hot-apply |
| `wombat_persona_warmth` (reserved/neutral/warm) | GET/PUT | hot-apply |
| `wombat_persona_directness` (gentle/plain/blunt) | GET/PUT | hot-apply |
| `wombat_persona_humor` (none/dry) | GET/PUT | hot-apply |
| `wombat_persona_proactivity` (minimal/balanced/forward) | GET/PUT | hot-apply |
| `wombat_voice_enabled` (bool) | GET/PUT | restart-tier |
| `wombat_stt_provider` (local/deepgram/elevenlabs/fish) | GET/PUT | restart-tier |
| `wombat_stt_model` (string) | GET/PUT (admitted; current form omits it) | restart-tier |
| `wombat_tts_provider` (local/deepgram/elevenlabs/fish) | GET/PUT | restart-tier |
| `wombat_tts_voice_id` (string) | GET/PUT | restart-tier |
| Key configured booleans (elevenlabs/deepgram/fish) | GET /settings `keys` | keyring |
| `storage_unavailable` flag | GET /settings (TK-242) | degrade signal |
| Mic input devices | `listInputDevices()` (TK-224) | local |
| Drop-dir-not-configured state | `saveCapture` result | degrade signal |
| Restart outcome (restarted/failed+detail/busy) | `wombatRuntime.restart()` | runtime |

Actions the screen must offer: edit every field above; enter/replace an API
key per provider (write-only, never echoed); pick input device; record / stop
/ mute (real capture into the ASR drop-dir — doubles as "test your mic");
toggle voice on/off; restart wombat; retry after a failed load.

**Deliberately NOT shown, with reasons:**

- `wombat_timezone` — TK-228 non-goal pins it operator-`.env`-tier, NOT
  app-editable; it isn't even in the GET response. Rendering it would need an
  additive read-only API field (no ticket asks for that). No-placebo rule:
  don't render what we can't fetch. Flagged as an open question below.
- DeepSeek/Google/DSN credentials — DEC-32 flagged follow-up stands (TK-242
  non-goal repeats it).
- Output-volume control — DEC-39: winsound has no gain seam; a slider would
  be a placebo.
- Chat pane — a conversation surface, not a setting. It currently shares the
  single window with the form; this brief scopes the settings screen and
  assumes the shell gives settings its own surface (see open questions).

## 3. Top user tasks, ranked

1. **Tune the personality** (most-revisited: it's the point of a persona
   matrix, and it hot-applies — instant gratification loop).
2. **Set up / switch voice providers + keys** (episodic but high-stakes: a
   provider without its key is a silent brick today).
3. **Test the mic / pick input device** (setup-time and troubleshooting).
4. **Restart the runtime** (whenever a restart-tier change lands).
5. **Rename the assistant** (rare, one-time-ish).

## 4. Information hierarchy

**Primary (visible at a glance, no interaction):**

- Category rail: **Persona · Voice & Audio · API Keys · System** (4 groups —
  inside the 4–5 guidance; grouped by task, not by API model).
- The degraded-storage banner, when active — it outranks everything (it
  changes what every control means).
- Within Persona: the five axes as segmented controls — the whole personality
  readable as one profile without opening anything.
- Within Voice & Audio: voice on/off, STT provider, TTS provider, input
  device, Record/Mute.
- The "Restart needed" strip (persistent, with an inline Restart button) the
  moment any restart-tier change saves.

**Secondary (one interaction away):**

- TTS voice ID — revealed only when TTS provider ≠ local (a local provider
  has no voice-ID concept; showing it always is form-noise).
- Key entry fields — the API Keys category; additionally, a provider select
  whose chosen provider lacks a key shows an inline warning with a "add key"
  jump-link (task 2's failure mode caught where it happens).
- Restart outcome detail (success line / failure detail under the button).

**Tertiary (behind "Advanced"):**

- `wombat_stt_model` — free-text model name inside Voice & Audio → Advanced
  disclosure. Admitted by the API, currently unexposed; power-user knob.
  (Open question below — include or keep omitting.)

**Grouping rationale:** Persona = "how it behaves" (all hot-apply — one
consistent save story per category). Voice & Audio = "how we talk to each
other" (providers, devices, the voice gate, mic test). API Keys = credentials
(write-only semantics + keyring backing deserve their own explained space,
and they keep working during the pg degrade — a property that would be
confusing if keys were interleaved with degraded fields). System = the
runtime lifecycle (restart) and the app/storage status line.

## 5. Interaction notes

- **Save model: auto-save per control.** Segmented click / select change /
  toggle → immediate `PUT` of that one field (the existing touched-field
  patch discipline, narrowed to one field); text fields (name, voice ID,
  STT model, keys) save on blur or Enter with an explicit small "Save"
  affordance while dirty. Inline per-control feedback: "Saved" (persona adds
  "applies next turn"), or the restart-tier fields feed the persistent
  restart strip instead of a per-field notice.
- **Restart strip:** appears after the first restart-tier save; lists nothing
  fancy — "Changes pending restart" + [Restart wombat] inline. Restart button
  states: idle / Restarting… (disabled) / "Wombat restarted." / loud failure
  with detail (TK-239 postures, unchanged).
- **Key fields:** write-only forever — placeholder "Enter new key", never a
  value; `Indicator` dot shows configured state from the GET booleans; saving
  a key shows "Key stored" and flips the dot; keys are restart-tier (feed the
  strip).
- **Defaults honesty:** a `null` field renders the default value with a muted
  "default" tag (persona axes: the DEFAULT_MATRIX levels), so Jim can tell
  "I chose terse" from "terse is the fallback."

### Degraded / empty / loading states (first-class)

1. **Loading:** category rail renders immediately; content area shows a plain
   "Loading settings…" line (no spinner theater — animation allowlist).
2. **Storage unavailable (`storage_unavailable=true`):** persistent banner at
   the top of the content area, danger-adjacent but calm: "Settings storage
   is unreachable — showing defaults, not your saved values. Settings are
   read-only until it's back." + [Retry] (re-runs GET). All settings controls
   render **read-only** (values legible, not grayed-to-oblivion; segmented
   controls non-interactive). **API Keys stay fully writable** — the banner's
   second line says so ("API keys are stored separately and still work").
   **Restart stays available** (it's a process control, not storage).
3. **PUT 503 (storage died between GET and save):** the control reverts to
   its last-known value with an inline error "Couldn't save — settings
   storage unavailable", and the screen re-checks GET to raise the banner.
4. **Settings API itself unreachable (GET rejects):** full-content error
   panel — "Can't reach wombat's settings service" + detail + [Retry]. No
   half-rendered form.
5. **Voice drop-dir not configured:** Record/Mute/device controls disabled
   WITH the explanation line (TK-224 posture, kept verbatim).
6. **No input devices / no mic permission:** device select shows "No input
   devices found" placeholder; Record surfaces its own error on attempt
   (existing audio.ts behavior).

### Design-system compliance

Tokens only (`tokens.ts`), Panel/Button/Field/Select/Indicator/Icon base
components; segmented control is ONE new component in `app/src/components/`
consuming brand/ink/surface tokens + the shared `focusRing`; lucide icons
(e.g. `RefreshCw` restart, `KeyRound` keys, `Mic` audio, `SlidersHorizontal`
persona, `Settings2` system); no gradients; no new animation classes (the
allowlist's `transition-colors`/`transition-shadow` suffice); the degraded
banner uses existing `danger`/surface tokens — if a calmer "warning" hue is
wanted, that's the reserved magenta-point discussion, NOT an ad-hoc literal.

## 6. Open questions for Jim (taste)

1. **Sidebar rail vs single scroll with sticky section headers** — mock shows
   the rail (desktop convention, room to grow, keeps Persona one click from
   anywhere). One word flips it.
2. **Auto-save vs explicit Save button** — mock shows auto-save per control
   (matches hot-apply persona semantics + the restart strip). Say the word to
   keep a page-level Save.
3. **Expose `wombat_stt_model` under Advanced?** — API already admits it;
   current form deliberately omits it. Mocked inside a collapsed Advanced
   disclosure; trivially removable.
4. **Timezone display** — showing it read-only would need an additive API
   field (a new ticket). Not mocked. Want it?

## 7. Mock locations

- Lucid (6 pages, one per board): https://lucid.app/lucidchart/1ef7aae8-4766-4fbb-b924-4f1dd407439d/edit
- HTML wireframe (same content, browser-openable): `planning/design/settings-screen-mock.html`

## 8. Deviation log

(Empty — append here during Phase 2 with one-line reasons.)

---

## 9. Iteration 2 (2026-07-11) — Jim's rejection, and the response

### 9.1 Jim's critique (verbatim-in-spirit) and how each point is answered

**(1) "The buttons for the settings are too big, it still looks like one big
form. Thinner buttons. Make sure they are spaced between elements so nothing
is smashed together."**

Response — three concrete changes:

- **Compact control heights.** Segmented controls, selects, text inputs, and
  secondary buttons drop to a 26 px control height (13 px text, 4 px vertical
  padding); segment options become pill-chips rather than a heavy boxed bar.
- **Open whitespace rhythm.** Row-to-row spacing inside a card goes to 16 px,
  card-to-card to 24 px, and the content column gets 32 px side padding — the
  compact controls buy the room; the room is spent on air, not more controls.
- **Killing the form-ness with density contrast, not just spacing.** Each
  settings category opens with a *summary artifact* that is not a control:
  Persona opens with a one-line generated profile sentence ("John is terse,
  neutral, and plain — no humor, balanced proactivity."); Voice & Audio opens
  with a one-line signal-chain readout ("Mic → local STT → gate → ElevenLabs
  voice"); API Keys opens with the three configured-state dots as a glance
  row. Persona axes lay out on a two-column grid (label-above-chips), so the
  category reads as a profile card, not a stack of labeled rows. The result
  is varied blocks of different densities, not one homogeneous form.

**(2) "There is no branding."**

Response — wombat gets an identity: **wombat — The Steward.**

- **Mark:** a simple geometric *burrow arch* — a semicircular arch sitting on
  a baseline, inside a circle. Reads as a wombat burrow (shelter, quiet
  competence), draws with two shapes in Lucid, works at 16 px. Construction
  shown on the mock's Board 0.
- **Wordmark:** lowercase `wombat` (the project's own casing) set semibold,
  with the epithet `THE STEWARD` in letterspaced small caps beneath/beside it.
- **Presence in the shell, not a corner logo:** the mark + wordmark anchor a
  persistent app **header bar** (with runtime status at the right); the chat
  pane is titled with the assistant's *chosen name* ("John") plus the mark;
  the brand hue carries every interactive fill; empty states speak in the
  steward's voice ("Nothing needs you right now."); assistant chat messages
  carry the mark as their avatar. The brand is a system, applied at the shell
  level (header, nav, chat identity, tone of microcopy), per standard
  app-shell practice ([Design Systems Collective — shell as layout engine](https://www.designsystemscollective.com/component-shell-a-layout-engine-for-modern-apps-57e59d3f6951)).

**(3) "I don't like the colors. White yellow and off white isn't a good look."**

Response — full palette replacement (DEC-39 still binds: bright,
color-theory-guided, tokenized in the one `@theme` block, complete theme, no
gradients, Tailwind):

- **Scheme: "midnight burrow" — dark analogous-cool base with a bright violet
  brand and a complementary cyan counterpoint.** Surfaces are deep
  indigo-slate (OKLCH hue ≈ 265, chroma ≈ 0.02) — *not* pure black, layered
  lighter as they elevate (canvas 18 % L → panel 22 % → elevated 26 %), per
  dark-UI elevation practice ([Toptal — dark UI](https://www.toptal.com/designers/ui/dark-ui-design),
  [Zeplin — dark-mode palettes](https://blog.zeplin.io/dark-mode-color-palette/)).
  Ink is warm-cool near-white (93 % L), never pure white.
- **Hue logic:** brand **violet** (hue ≈ 295) sits analogous to the surface
  hue (265), so interactive fills feel *of* the interface rather than pasted
  on; the focus/info **accent cyan** (hue ≈ 200) sits across the wheel from
  violet-adjacent warm hues, giving unmistakable focus states; **danger
  coral** (hue ≈ 25) and **positive mint** (hue ≈ 155) stay off the
  brand/accent axes so semantics never read as brand shades. Accents run
  bright (L 70–78 %) against the dark base — that is where DEC-39's "bright"
  lands in a dark scheme: luminous accents on deep surfaces, chroma kept
  moderate so the dark theme doesn't vibrate ([Netguru — dark theme tips](https://www.netguru.com/blog/tips-dark-mode-ui),
  [OKLCH contrast](https://medium.com/@vyakymenko/color-contrast-with-oklch-prefers-reduced-motion-and-motion-design-ethics-089c0c8897d0)).
- All values are mock-stage proposals; implementation re-derives exact OKLCH
  numbers against WCAG 4.5:1 for body text when Phase 2 edits `theme.css`.
  Same token names (`surface-*`, `ink-*`, `brand*`, `accent`, `danger*`,
  `positive`, `focus`) — a `theme.css`-only re-theme by construction (TK-225).

**(4) "Make sure there is a chat pane. So you can communicate in a chat bot
like manner."**

Response — scope widens from settings screen to the **app shell**, and chat
becomes a first-class, always-present surface:

- **Persistent right-side chat pane** (~340 px, collapsible to an edge tab),
  visible from every view — the Cursor/Cline side-panel pattern, chosen over
  a chat-is-the-only-view layout because wombat's chat must coexist with
  glanceable information surfaces ([UX Collective — where AI sits in the UI](https://uxdesign.cc/where-should-ai-sit-in-your-ui-1710a258390e),
  [Setproduct — AI chat anatomy](https://www.setproduct.com/blog/ai-chat-interface-ui-design)).
- The pane is the existing TK-223 chat, restyled: bubbles (user right,
  steward left with the mark), the honest `held` state and the
  "wombat is not running" degraded state kept verbatim in meaning, composer
  pinned at the bottom. No history persistence, no streaming, no typing
  theater — TK-223's scope is not silently expanded.
- Chat identity = the assistant's chosen name, so renaming in Persona visibly
  renames the chat header (branding and settings reinforcing each other).

**(5) "There is no information that we migrate from gmail and calendar. There
is no calendar view, or upcoming meetings or anything at all."**

Response — a **Today** view becomes the app's default landing surface,
composed of four cards over the (concurrently-being-designed) Postgres-backed
read API:

- **Morning brief** — latest delivered brief (today 07:00), first lines
  inline, "Open full brief" expands.
- **Upcoming** — agenda list, time-bucketed **Today / Tomorrow / Later this
  week** with detail decreasing with distance (the established agenda
  pattern: bucket by time, most detail nearest to now —
  [ui-patterns — event calendar](https://ui-patterns.com/patterns/EventCalendar),
  [Eleken — calendar UI](https://www.eleken.co/blog-posts/calendar-ui)). List
  view, not a month grid — a personal 7-day window is agenda-shaped.
- **Inbox highlights** — gmail-derived items wombat judged worth surfacing
  (needs-reply vs FYI tags), readonly, "open in Gmail" as the only action
  (wombat's scopes are readonly; the design never implies it can send).
- **Steward's notepad** — wombat's scratch/working-memory surface, readonly
  list of what wombat is currently holding/tracking. Transparency surface,
  not an editor.
- A **"last synced HH:MM" staleness line** per data card; degraded/empty/
  loading states are first-class (see 9.4).

### 9.2 Assumed read-API contract (architect must confirm or flag)

Designed against these shapes; the architect designing DEC-43 persistence
binds to them or flags mismatches — the mock treats them as assumptions, not
facts:

- `GET /events?window=7d` → `{ events: [{ id, title, start, end, all_day, location?, attendees_count?, source: "gcal" }], synced_at, storage_unavailable: bool }` — stored (not live-proxied) events; `storage_unavailable: true` ⇒ degraded card.
- `GET /inbox/items?window=48h` → `{ items: [{ id, subject, from, received_at, category: "needs_reply" | "fyi", snippet? }], synced_at, storage_unavailable }` — gmail-derived, readonly.
- `GET /brief/latest` → `{ delivered_at, body_markdown }` or 404-equivalent empty ⇒ "No brief yet — next one at 07:00."
- `GET /scratch` → `{ notes: [{ id, text, noted_at }], storage_unavailable }` — wombat's working memory, readonly in the app.

Degrade posture mirrors TK-242's settings shape (`storage_unavailable`
boolean on the 200 response), so the app has ONE degraded-storage idiom.

### 9.3 Revised shell + hierarchy (supersedes §4's frame)

**Shell anatomy (every view):** header bar (mark + wordmark left; runtime
status dot + Restart affordance right) · left nav rail (**Today**, then a
SETTINGS group: Persona, Voice & Audio, API Keys, System) · content column ·
persistent right chat pane. One rail, two levels — the iteration-1 four-
category rail survives as the rail's settings group; Today is the default
landing view (settings are episodic, information is daily).

**Primary (at a glance):** header brand + runtime status; Today's three data
cards + brief; the chat pane with composer; degraded-storage banner when
active (still outranks everything); within settings categories, same
primaries as §4.

**Secondary (one interaction):** full brief body (expand); event details
beyond title+time (click row); inbox snippet (click row); chat pane collapse/
expand; TTS voice ID (conditional, unchanged); restart outcome detail.

**Tertiary (behind Advanced):** `wombat_stt_model` (unchanged, open question
3 stands).

**What carries forward unchanged from iteration 1 (Jim did not object):**
four-category settings organization; segmented controls over dropdowns (now
thinner chips); auto-save per control + the persistent restart strip;
first-class degraded states; write-only keyring key rows with configured
dots; every no-placebo exclusion in §2 (timezone, DeepSeek/Google/DSN keys,
output volume). Open questions 1–4 in §6 all still stand — the shell does
not moot any (Q1 narrows to "rail settings-group vs separate settings page
with sub-nav": mock shows the single grouped rail).

### 9.4 Degraded / empty / loading — additions for the new surfaces

1. **Today, storage unavailable:** one banner atop the content column
   ("Wombat's memory store is unreachable — Today can't show calendar, inbox,
   or notes right now.") + [Retry]; each data card renders its degraded body
   ("Unavailable — storage offline") rather than vanishing; the morning-brief
   card follows whichever source it has (file-based brief keeps rendering if
   readable). Chat and settings-rail navigation stay fully alive.
2. **Today, empty (healthy but nothing there):** steward-voiced empty states
   — "No meetings in the next 7 days." / "Nothing in the inbox needs you." /
   "Notepad is empty." Empty ≠ error; no banner.
3. **Today, loading:** card skeletons as plain "Loading…" lines (animation
   allowlist — no shimmer).
4. **Chat, wombat not running:** pane stays visible with the honest
   "Wombat is not running — start it to chat." line + disabled composer
   (TK-223 posture); header runtime dot goes hollow.
5. All iteration-1 settings degraded states (§5) stand verbatim.

### 9.5 Mock

- **HTML mock (the artifact of record for iteration 2):**
  `planning/design/settings-screen-mock.html` — rewritten; Board 0 brand +
  palette, Boards 1–2 shell/Today (populated + degraded/empty/loading),
  Boards 3–6 settings (slim controls) incl. degraded. The orchestrator
  transcribes this to Lucid; the iteration-1 Lucid link in §7 is now stale.

### 9.6 Iteration-2 research additions

- App-shell / side-panel chat: [UX Collective](https://uxdesign.cc/where-should-ai-sit-in-your-ui-1710a258390e), [Setproduct AI chat](https://www.setproduct.com/blog/ai-chat-interface-ui-design), [Design Systems Collective shell](https://www.designsystemscollective.com/component-shell-a-layout-engine-for-modern-apps-57e59d3f6951)
- Agenda/time-bucketing: [ui-patterns event calendar](https://ui-patterns.com/patterns/EventCalendar), [Eleken calendar UI](https://www.eleken.co/blog-posts/calendar-ui)
- Dark palette practice: [Toptal dark UI](https://www.toptal.com/designers/ui/dark-ui-design), [Zeplin dark-mode palette](https://blog.zeplin.io/dark-mode-color-palette/), [Netguru dark theme](https://www.netguru.com/blog/tips-dark-mode-ui)
