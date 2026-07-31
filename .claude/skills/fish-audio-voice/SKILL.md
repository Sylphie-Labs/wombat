---
name: fish-audio-voice
description: How to drive Fish Audio TTS expressively — the marker vocabulary ((laughing), (break), emotions/tones), the API surface (model header, prosody), and exactly where/how wombat's voice pipeline must carry markers without tripping its own sanitizers. Use for ANY work touching Fish TTS, speech shaping, spoken-reply naturalness, or the voice pipeline's expressive controls.
---

# Fish Audio expressive voice — working reference

Verified against docs.fish.audio 2026-07-31. Wombat's adapter: `src/wombat/voice/tts.py`
(`FishAudioTTSAdapter`), speak path: `src/wombat/stages/speech_shape.py` (DEC-55/DEC-69).

**PINNED (DEC-72, contract v2.183): `wombat_fish_model` default `s2.1-pro` — square-bracket
markers; steward subset `[calm] [curious] [sympathetic] [soft tone] [chuckling] [sighing]
[break] [long-break]`.** The S1 sections below remain for reference (the tag CONCEPTS carry
over; S2 accepts the same names in brackets, plus free-form — free-form emission is
deliberately unused in v1, DEF-17). `s2.1-pro-free` is the zero-credit variant. The
validator is an EMISSION-POLICY check: ANY bracketed token outside the subset — fixed,
free-form, or invented — rejects the whole text to silence. `[break]`/`[long-break]`
efficacy on s2.1-pro is a recorded unknown: confirm by ear at the armed smoke. The
contract (DEC-71/DEC-72) is the record; if this file and the contract disagree, the
contract wins. Three Jim-pinned invariants: (1) tag emission is
offered to the shaping model ONLY when a Fish adapter was actually constructed (resolved
API key + fish provider + S2 model) — no key, no tags, instruction byte-identical to the
tag-free form; (2) the instruction carries a DEFINITIONS block (each tag: one-line
semantic + placement guidance), sourced from the same vocabulary module the validator
reads — never two lists; (3) ordering is shape → validate → speak: only
validator-passed text ever reaches `adapter.speak()`.

## 1. The API surface

- `POST https://api.fish.audio/v1/tts`, `Authorization: Bearer <key>`.
- **The engine is selected by a `model` HTTP HEADER** — values: `s1`, `s2-pro`, `s2.1-pro`,
  `s2.1-pro-free`. **Wombat currently sends NO model header**, so calls ride Fish's default
  engine and expressive markers are NOT guaranteed to work until the adapter pins one.
  This is the first wiring gap for any expressiveness ticket.
- Body: `text`, `reference_id` (Jim's cloned-voice id — markers compose WITH the cloned
  voice, no conflict), `format` (`wav` today; also mp3/pcm/opus), optional:
  - `prosody: {"speed": 0.5–2.0, "volume": <dB shift>}` — global pace/loudness knobs.
  - `temperature`, `top_p` (0–1) — expressiveness/diversity of the delivery.
  - `chunk_length` (100–300), `latency` (`low|balanced|normal`).
- Response = audio bytes (chunked streaming). Legacy path: whole body buffered, played
  via `WinsoundPlayer` (full RIFF/WAV image, `format: "wav"`). DEC-73 (TK-330..333)
  moves the fish path to STREAMED playback: `format: "pcm"` + `latency: "low"` +
  one shared `STREAM_SAMPLE_RATE` constant, chunks written to a sounddevice
  `StreamingAudioWriter` (`voice/stream_playback.py`) as they arrive — no RIFF bytes
  on the path (sidesteps Fish's bogus-length WAV header class). Validation always
  precedes the first streamed byte; heard-partial counts as spoken
  (`PartialSpeechError` semantics). Buffered path remains the loud fallback.

## 2. Marker syntax — S1 vs S2

- **S1: fixed tags in PARENTHESES** — `(happy) That worked.` Closed vocabulary (below).
- **S2 family: free-form descriptions in BRACKETS** — `[warm, slightly amused] That worked.`
  Not limited to fixed tags.
- Markers do NOT count toward token/char billing and add no latency (per Fish docs).
- Never wrap markers in quotes; never invent S1 tags outside the vocabulary.

**Wombat rule: S2 BRACKET syntax (Jim's ruling 2026-07-31 — he uses S2; check the
current DEC-71-family ruling in planning/contract.yaml for the pinned model value).**
Both syntaxes survive wombat's sanitizers (verified: nothing in `_FORBIDDEN_PATTERNS`
or `_strip_markdown_tokens` matches bare parens or bare brackets — the link regexes
need the full `[text](url)` pair). The closed-vocabulary guarantee is preserved on S2
by EMISSION POLICY: the shaping prompt only ever emits wombat's allowed tag subset in
bracket form, and a deterministic exact-equality validator rejects any bracketed token
outside it — S2 accepting free-form beyond our subset is fine and deliberately unused
in v1. One adjacency hazard: a tag immediately followed by a parenthesized clause
(`[break] (see below)`) matches the markdown-link pattern on the full-reply strip path
— the shaped path must never emit that adjacency (tags are voice-only, so this only
binds if tags ever reach the full-reply path).

## 3. The S1 vocabulary (closed set)

**Placement: emotion tags at the START of a sentence, one (max 3) per sentence.
Tone markers and sound effects may appear anywhere; put effects right after the word
they punctuate. Pauses go where the silence goes.**

- **Emotions (49):** (happy) (sad) (angry) (excited) (calm) (nervous) (confident)
  (surprised) (satisfied) (delighted) (scared) (worried) (upset) (frustrated) (depressed)
  (empathetic) (embarrassed) (disgusted) (moved) (proud) (relaxed) (grateful) (curious)
  (sarcastic) (disdainful) (unhappy) (anxious) (hysterical) (indifferent) (uncertain)
  (doubtful) (confused) (disappointed) (regretful) (guilty) (ashamed) (jealous) (envious)
  (hopeful) (optimistic) (pessimistic) (nostalgic) (lonely) (bored) (contemptuous)
  (sympathetic) (compassionate) (determined) (resigned)
- **Tones (5 on S1):** (in a hurry tone) (shouting) (screaming) (whispering) (soft tone)
  — `[emphasis]` exists only on S2; there is NO S1 `(emphasis)` tag. For emphasis on S1
  use word choice, punctuation, or a (break) before the stressed word.
- **Paralinguistic effects (10 on S1):** (laughing) (chuckling) (sobbing) (crying loudly)
  (sighing) (groaning) (panting) (gasping) (yawning) (snoring)
- **Ambience (rarely wanted for wombat):** (audience laughing) (background laughter)
  (crowd laughing)
- **Pauses:** (break) short, (long-break) long.
- Informal spellings work without tags: "Ha, ha, ha" laughs; "Hmm," "Well…" hesitate.

Persona fit note: wombat is a quiet steward. The useful subset is small — (soft tone),
(calm), (chuckling), (sighing), (break)/(long-break), occasionally (curious)/(sympathetic).
(screaming)/(hysterical)/ambience tags are essentially never in-character; a validator
whitelist should reflect the persona subset, not the whole S1 set.

## 4. Wombat integration map (where markers must flow, and where they must NOT)

Two speak paths exist (DEC-69):

1. **Default shaped path** — `SpeechShapeStage` makes a SECOND mouth call with
   `_SPEECH_SHAPE_INSTRUCTION` (speech_shape.py:106) to produce a voice-only summary,
   validated by `_shape_speech_text` (`_FORBIDDEN_PATTERNS` no-placebo: any markdown/URL
   ⇒ reject to silence). **This is THE place to emit markers** — extend the instruction
   to offer the (persona-subset) vocabulary. Parenthesized tags pass the validator
   untouched today. Because this text is voice-only, tags never pollute the chat pane.
2. **Opt-in full-replies path** (`wombat_speak_full_replies=True`) — deterministic
   sanitize of the SAME text the pane shows (`_sanitize_full_reply_text`). **Do NOT ask
   compose to emit markers**: they'd render as literal "(happy)" in the chat pane
   (exactly the DEC-69 misalignment class). If this path ever gets expressiveness, it
   needs its own tag-injection step after sanitize, or tags stay out of it entirely.

Sanitizer interactions (verified in source, speech_shape.py:118-234):
- `_LEADING_LABEL_PATTERN` strips `Word: ` at string start only — `(happy) Hello` is
  safe (parens aren't in the token class). But `(break)` as the very first token is
  fine too; no colon, no match.
- The char budget (`wombat_spoken_reply_max_chars`, default 400) counts marker chars
  even though Fish doesn't bill them — budget accordingly when tags are added.
- `_truncate_at_word_boundary` can cut a trailing tag mid-parenthesis on the full-reply
  path; a truncation-aware guard must drop a half tag rather than speak "(laugh".

Model-header wiring: `FishAudioTTSAdapter.speak` builds the request at
voice/tts.py:96-109 — add `model` to headers (config-driven, e.g. `wombat_fish_model`,
DEC-43 settings tier, restart-to-apply like voice_id). Until that lands, markers reach
Fish's default engine with undefined behavior — test before assuming.

## 5. Testing recipe

- Unit: fakes-only through `VoiceTransport`/`AudioPlayer` injection (the adapter was
  built for it) — assert the header carries the model, the body text carries tags
  verbatim, tags survive `_shape_speech_text` round-trip.
- Live ear-checks are ARMING-VAR gated per house rules (the DEC-62 idiom):
  `WOMBAT_TEST_FISH_LIVE=1` + real key + Jim's `reference_id`, one short utterance per
  marker under test — e.g. `(soft tone) Your first meeting is at nine. (break) Nothing
  else needs you before then.` Live smokes cost API credit; never in the plain suite.
- Known live gotcha history: `.env` keys shadow the settings table (DEC-43 precedence);
  a 402 means Fish credit ran out — the adapter raises and SpeakSink degrades loudly.

## 6. Constraints that bind any expressiveness work

- DEC-55c/f: shaped speech comes from the shaping call or nothing — never rewrite,
  never fall back to composed text; the no-placebo validator stays reject-not-fix.
- CON-3: adapter failures raise; SpeakSink owns the degrade. Don't catch in the adapter.
- Gate custody: expressiveness changes HOW wombat sounds, never WHEN it speaks.
- Persona axes (DEC-33/FEAT-14) are the natural home for "how expressive" — a marker
  policy should read the live persona, not hardcode a mood.
