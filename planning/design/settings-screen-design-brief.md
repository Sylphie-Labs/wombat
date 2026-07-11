# Settings screen — design brief

> **Iteration 4 (2026-07-11) is the binding design; the iteration-3 LAYOUT is
> Jim-approved.** Sections 1–6 are the iteration-1 record; §9 iteration 2;
> §10 iteration 3; §11 (Iteration 4) wins where they conflict. Iteration 4
> changes exactly two things — placeholder colors routed through theme-token
> variables, and richer event/inbox/notepad card components — and moves
> nothing else. Jim still owns the final palette (token-file swap).

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

---

## 10. Iteration 3 (2026-07-11) — Jim's rejection, and the response

### 10.1 Jim's critique (verbatim-in-spirit)

"I don't know what I'm looking at. It's not a design — it's a color scheme /
token library. Just focus on designing the interface. I will work on colors.
Just make sure the colors are pointed to a theme and not hardcoded."

### 10.2 What changed

1. **Color is out of scope; Jim owns the palette.** The brand/palette board
   (iteration-2 Board 0) is deleted. §9.1 point (3) — the entire "midnight
   burrow" palette proposal — is void. The mock is rendered in neutral grays
   (white/gray wireframe values) so nothing on it reads as a color proposal.
2. **The mock is now interface boards only.** Every board is an application
   screen someone could build: real layout, regions, realistic content, and
   interaction states. All meta-exposition is off the canvas — no legend, no
   hierarchy-tier badges, no research citations, no token names, no design
   commentary inside the screens. Annotations live in small captions below
   each board.
3. **Structure carries forward from iteration 2 unchanged** (Jim did not
   object to it): app shell with header (mark + wordmark + runtime status)
   and left nav (Today first, settings grouped: Persona / Voice & Audio /
   API Keys / System); persistent collapsible chat pane (320 px) on every
   view; Today as default landing (morning brief, time-bucketed Upcoming,
   readonly Inbox highlights, Steward's notepad, per-card sync times); slim
   26 px controls with the 16/24/32 px spacing rhythm; segmented chips for
   the persona axes with the generated profile-sentence lead; auto-save per
   control + persistent restart strip; first-class degraded/empty/loading
   states; write-only keyring key rows with configured dots. Branding stays
   *in* the layouts (header wordmark + mark, chat identity = assistant's
   chosen name) with no dedicated board and no explanation — it is simply
   part of the header and chat design.
4. **Assumed read-API contracts (§9.2) unchanged** — the architect recorded
   DEC-45/DEC-46 against them; the mock still designs to those shapes.
5. **Open questions (§6) unchanged** — all four still stand.

### 10.3 Color policy (the whole of it)

Every color in the Phase 2 implementation references a theme token — the
TK-225 `theme.css` / `tokens.ts` discipline: components consume token names
only, zero hardcoded color values anywhere in component code, so Jim can
re-theme the entire app by editing one file. That is the only color
commitment this design makes.

### 10.4 Mock (artifact of record for iteration 3)

`planning/design/settings-screen-mock.html` — rewritten as five neutral-gray
interface boards:

1. Today view, full shell (header / rail / content / chat), populated.
2. Settings · Persona.
3. Settings · Voice & Audio (restart strip active, missing-key warning).
4. Settings · API Keys and Settings · System (two full shells).
5. States: Today degraded-storage, Today empty, Today loading + chat while
   wombat is down, settings storage-unreachable read-only, failed save,
   settings hard failure — compact small frames, each labeled.

The iteration-2 HTML content and the iteration-1 Lucid link (§7) are stale.

---

## 11. Iteration 4 (2026-07-11) — targeted revision on an approved layout

### 11.1 Jim's feedback (verbatim-in-spirit)

**The layout is APPROVED as of iteration 3:** "this is a much better layout.
I like the side panel chat. Keep that. The layout in general feels right."
Two revisions only:

1. "We just want to use any color as placeholders and let me change the token
   file when I'm ready. If you do everything black and white now, it will be
   a pain in the ass to go back and manually find all the places to change."
2. "The individual components for rendering events and such is extremely
   bare. We need event cards or something like it."

Nothing else moves: shell (header / rail / content / 320 px chat pane),
Today-as-landing, four settings categories, 26 px slim controls with the
16/24/32 spacing rhythm, segmented persona chips + profile sentence,
auto-save + restart strip, write-only key rows, all degraded/empty/loading
states, and all §2/§6 exclusions and open questions carry forward verbatim.

### 11.2 Change 1 — placeholder colors through token variables

The mock's CSS defines every color as a `:root` custom property named
**exactly like the implementation's `theme.css` tokens** (`--color-surface-
canvas/panel/elevated`, `--color-ink-primary/muted`, `--color-border-
default/strong`, `--color-brand`/`-hover`/`-ink`, `--color-accent`,
`--color-danger`/`-ink`, `--color-positive`, `--color-focus`) and no rule
uses a literal color — the mock itself demonstrates the swap-one-file
re-theme. The applied values are placeholders (the iteration-2 midnight-
burrow set, which I still stand behind as a placeholder); they are
deliberately unexplained and unpresented — no swatch board, no rationale on
canvas, one caption line total. §10.3's color policy stands as the whole
color commitment.

### 11.3 Change 2 — event / inbox / notepad data-display components

Settings keep the 26 px control slimness; DATA DISPLAY gets more generous
card components. Pattern grounding: agenda lists put a time block left with
a vertical separator/accent edge before the detail, start time prominent,
distinct all-day treatment ([ServiceNow Horizon — events list](https://horizon.servicenow.com/native-mobile/components/mobile-component-events-list),
[ui-patterns — event calendar](https://ui-patterns.com/patterns/EventCalendar),
[Setproduct — schedule/events template](https://www.setproduct.com/freebies/schedule-events-template)).

- **Event card** (contained card per event, scales 1→~10): left **accent
  edge** (3 px) + a fixed-width **time block** — start time prominent
  (15 px semibold, tabular nums), end time beneath it muted; then title
  (600 weight) with a **meta line** of small icons (location pin,
  attendee count, source). **Today's next event** is emphasized: brand
  accent edge, elevated surface, a "next" chip; other events carry a
  neutral edge. **All-day events** drop the time block for an "All day"
  pill and a dashed edge. Later-bucket events put the weekday atop the
  time block ("Mon" / "09:00") with detail decreasing with distance.
- **Inbox card** (honest to the DEC-45 five-field gmail projection:
  `message_id, subject, sender, received_at, priority_band` — **no
  snippet exists in the store**): sender **initial block** (28 px rounded
  square, brand-tinted, first letter), **subject** as the primary line,
  sender name + received-at as the meta line, **priority chip** right
  (needs-reply = filled, FYI = outline; labels map from `priority_band`
  pending the recorded follow-up amendment). Row click = "Open in Gmail"
  only. *Recorded follow-up:* a stored snippet field would enable a
  preview line; the design works without it and iteration-3's "row click
  opens snippet" caption is retracted as unhonest to the store.
- **Notepad entry**: timeline treatment — marker dot + entry text, with
  a muted "noted HH:MM" timestamp per entry; readonly, transparency
  surface unchanged.
- Board 5 keeps the same states but its loading state now shows
  card-shaped skeleton frames matching the new component heights (still
  plain, no shimmer), and degraded/empty bodies render inside the card
  shells so the components' off states are designed too.

### 11.4 Mock (artifact of record for iteration 4)

`planning/design/settings-screen-mock.html` — rewritten: same five boards
as iteration 3, all colors via the token variables above, Boards 1 and 5
carrying the new event/inbox/notepad components. Everything else is a
faithful carry-forward of the approved iteration-3 layout.
