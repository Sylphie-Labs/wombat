# 11 — SWEEP 11 restoration ledger (TK-373)

Written **incrementally, before each change**, so a session death mid-sweep leaves a
recovery map rather than a mystery. Every row: the setting, its value BEFORE the
sweep touched it, what the sweep set it to, and the restore proof.

## Baseline at rest — `wombat_settings` (Postgres `wombat-runtime-db`, port 5436)

Captured `2026-08-05` before any control was exercised:

```
docker exec wombat-runtime-db psql -U postgres -d wombat -c "SELECT key, value FROM wombat_settings ORDER BY key;"
```

| key | value BEFORE |
|---|---|
| wombat_assistant_name | `"Snoop"` |
| wombat_observe_mic | `true` |
| wombat_observe_screen | `true` |
| wombat_observe_screenpipe | `true` |
| wombat_observe_webcam | `true` |
| wombat_persona_brevity | `"balanced"` |
| wombat_persona_directness | `"blunt"` |
| wombat_persona_humor | `"playful"` |
| wombat_persona_pins | `{"humor": "2026-08-02T01:27:37.858877+00:00", "warmth": "2026-07-31T22:29:01.360518+00:00", "brevity": "2026-07-30T22:36:12.784900+00:00", "directness": "2026-07-31T22:29:01.360518+00:00", "proactivity": "2026-07-30T23:10:47.239717+00:00"}` |
| wombat_persona_proactivity | `"forward"` |
| wombat_persona_warmth | `"reserved"` |
| wombat_ptt_binding | `"mouse:4"` |
| wombat_speak_full_replies | `true` |
| wombat_spoken_reply_max_chars | `1200` |
| wombat_stt_provider | `"fish"` |
| wombat_tts_provider | `"fish"` |
| wombat_tts_voice_id | `"8bc0ef3b96424e6db3cccf6360c69778"` |
| wombat_user_name | `"Jim"` |
| wombat_voice_enabled | `false` |

**19 rows.** Every key NOT in this table is unset (the app renders it blank with a
placeholder). Restoration therefore has two halves: (a) restore each of these 19 to
the value above, and (b) **DELETE** any key this sweep creates that was not here.

## Pre-existing residue found BEFORE this sweep touched anything

Not caused by this run — recorded so restoration does not silently absorb it:

1. **A paired device `TK373 probe device`** (paired `2026-08-05T21:08:57.448782+00:00`) was
   already present, left by the earlier TK-373 attempt whose session died. Its minted
   token was still rendered on screen. **This sweep will revoke it** at close.
2. **The renderer held stale, unsaved form state** from that dead session — Persona showed
   `TK373Probe` / `JimTK373` / Terse / Neutral / Plain / Dry / Balanced with an armed Save,
   while the store held `Snoop` / `Jim` / balanced / reserved / blunt / playful / forward.
   Cleared with one `Ctrl+R` renderer reload before any control was exercised.
3. `wombat_persona_pins` already carried `2026-08-05T21:52:39` stamps on humor/directness/
   proactivity from that session. **Baseline for restoration is the state THIS sweep found**,
   recorded in the table above — not some earlier pristine state that no longer exists.

## Changes made, in order

| # | Setting | Value BEFORE | Set to | Restored |
|---|---|---|---|---|
| 1 | `wombat_assistant_name` | `"Snoop"` | `"Snoop-TK373"` | YES (post-kill) |
| 2 | `wombat_user_name` | `"Jim"` | `"Jim-TK373"` | YES (post-kill) |
| 3 | `wombat_persona_brevity` | `"balanced"` | `"expansive"` | YES (post-kill) |
| 4 | `wombat_persona_warmth` | `"reserved"` | `"warm"` | YES (post-kill) |
| 5 | `wombat_persona_directness` | `"blunt"` | `"plain"` | YES (post-kill) |
| 6 | `wombat_persona_humor` | `"playful"` | `"comedian"` | YES (post-kill) |
| 7 | `wombat_persona_proactivity` | `"forward"` | `"eager"` | YES (post-kill) |
| — | `wombat_persona_pins` | (baseline table above) | re-stamped by LivePersona on every axis change | YES (baseline JSON re-written) |

## Close-out 2026-08-05: sweep KILLED mid-run at Jim's direction

The driving agent was stopped by the orchestrator while partway through the
persona-settings pass (Jim paused the verification phase). All 8 changed rows above
were restored to their ledger baselines by direct `psql` UPDATE against
`wombat-runtime-db` and re-verified by SELECT (8/8 match). No keys beyond these 8
had been created or changed. TK-373 remains `todo`; no `11-app-surface.md` record
was produced.

**Residue deliberately NOT removed (pre-existing, flagged to Jim):** the paired
device `TK373 probe device` (paired 2026-08-05T21:08:57Z, token minted) left by the
earlier dead TK-373 session is still registered. Revoke via the app's device list
when desired.

