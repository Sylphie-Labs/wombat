# 00 — armed live suite (TK-362)

Run date: 2026-08-04. First time every `WOMBAT_TEST_*_LIVE` gate (plus
`WOMBAT_TEST_PG_DSN`) has been armed together and the full suite run as a set.
Per DEC-85(c) this ticket is done because the run happened and every finding
was routed — not because the run was green.

## Exact command

```
uv run pytest -q
```

Run from the repo root, with the seven vars below exported into the shell
first (never written to any file in the repo).

## Gates armed

| gate | armed | how |
|---|---|---|
| `WOMBAT_TEST_PG_DSN` | yes | fresh disposable `postgres:16` container (`wombat-test-pg-tk362`, port 5511), started for this run only |
| `WOMBAT_TEST_GCAL_LIVE` | yes | real OAuth token already present in the OS keyring (`wombat`/`gcal-oauth-token`) |
| `WOMBAT_TEST_GMAIL_LIVE` | yes | real OAuth token already present in the OS keyring (`wombat`/`gmail-oauth-token`) |
| `WOMBAT_TEST_FISH_LIVE` | yes | `WOMBAT_FISH_API_KEY` from `.env`; `WOMBAT_TTS_VOICE_ID` read from the `wombat_settings` Postgres table (the app-configured voice) and exported for this run only |
| `WOMBAT_TEST_SCREENPIPE_LIVE` | yes | screenpipe already running and healthy at `127.0.0.1:3030` |
| `WOMBAT_TEST_CAPABILITY_LIVE` | yes | real `DEEPSEEK_API_KEY`/`DEEPSEEK_BASE_URL` from `.env` |
| `WOMBAT_TEST_PERSONA_EVAL_LIVE` | yes | real `DEEPSEEK_API_KEY`/`DEEPSEEK_BASE_URL` from `.env` |

All seven gates armed — none blocked.

## Counts

```
9 failed, 2625 passed, 1 skipped, 1 warning in 457.97s (0:07:37)
```

Skip count dropped from the ordinary run's ~133 to 1.

## Remaining skip (named individually)

- `tests/voice/test_tts_fish.py:652` — "buffered time-to-first-sound=0.802s;
  sounddevice (voice-cloud extra) is not installed — cannot measure the
  streaming half." Concrete reason: the `voice-cloud` extra
  (`uv sync --extra voice-cloud`) is not installed in this environment, so the
  streamed-playback latency half of the measurement can't run. Not a
  `WOMBAT_TEST_*_LIVE` gate — an optional local dependency.

## Failures (9), routed as ISS-47..ISS-50 — none fixed here

**ISS-47** — 4 failures. Live GCal/Gmail smoke tests never re-supply a real
`GOOGLE_OAUTH_CLIENT_ID`/`GOOGLE_OAUTH_CLIENT_SECRET` after
`tests/conftest.py::_strip_google_env`'s autouse hermeticity strip (TK-254).
Latent since TK-254 landed; first surfaced now because these tests had never
run armed before.

- `tests/integrations/gcal/test_auth.py::test_live_refresh_against_real_google_token_endpoint`
  — `wombat.config.ConfigurationError: CalendarAuth: GOOGLE_OAUTH_CLIENT_ID is
  missing/blank; wombat cannot obtain a Google Calendar credential`
- `tests/integrations/gcal/test_live_wire.py::test_live_composed_stack_issues_one_real_get_and_parses_calendar_events`
  — same `ConfigurationError`, raised from `make_calendar_session`
- `tests/integrations/gmail/test_auth.py::test_live_refresh_against_real_google_token_endpoint`
  — `wombat.config.ConfigurationError: GmailAuth: GOOGLE_OAUTH_CLIENT_ID is
  missing/blank; wombat cannot obtain a Gmail credential`
- `tests/integrations/gmail/test_live_wire.py::test_live_composed_stack_issues_one_real_get_and_parses_gmail_messages`
  — same `ConfigurationError`, raised from `make_gmail_session`

**ISS-48** — 1 failure. Live humor-aside persona eval test has no
sample-fixture dispatch for `Mouth.CHAT`.

- `tests/persona/test_output_effects_live.py::test_humor_aside_heuristic_separates_dry_from_none[chat]`
  — `AssertionError: no humor sample fixture wired for mouth=<Mouth.CHAT: 'chat'>`

**ISS-49** — 1 failure. Live DeepSeek run: directness axis (gentle vs plain)
failed its no-placebo trip-wire.

- `tests/persona/test_output_effects_live.py::test_directness_hedge_lexicon_present_at_gentle_absent_at_plain_and_blunt`
  — `AssertionError: directness (gentle vs plain) axis UNMEASURED: the
  majority-verdict rule could not separate gentle (majority=False) from plain
  (majority=False) — no-placebo trip-wire fired (TK-210 AC3); this axis must
  be flagged in governance, not shipped as measured.`

**ISS-50** — 3 failures. Offline-default/degrade-path unit tests around Fish
TTS/voice-id are not hermetic against a real ambient `WOMBAT_FISH_API_KEY`/
`WOMBAT_TTS_VOICE_ID` — unlike Google OAuth, there is no autouse strip for
these two vars, so the values this run exported to arm
`WOMBAT_TEST_FISH_LIVE` leaked into unrelated unit tests.

- `tests/unit/test_config.py::test_load_config_voice_persona_defaults_stay_fully_offline`
  — `AssertionError: assert '8bc0ef3b96424e6db3cccf6360c69778' is None` (asserted
  `config.wombat_tts_voice_id is None`)
- `tests/voice/test_select.py::test_cloud_tts_missing_required_voice_id_falls_back_to_local_with_loud_log`
  — `AssertionError: a cloud provider class must never be constructed on the
  local path` (Fish adapter was constructed instead of falling back to local)
- `tests/voice/test_select.py::test_build_tts_adapter_with_info_degrade_paths_yield_false_none[fish-blank-voice-id]`
  — `AssertionError: assert TTSBuildInfo(fish_primary=True, fish_model='s2.1-pro')
  == TTSBuildInfo(fish_primary=False, fish_model=None)`

## Not repaired here

Per this ticket's `complexity_budget` and non_goals, no test or source file
was edited to make any of the nine failures pass. ISS-47 through ISS-50 carry
the full detail for the architect to triage and assign follow-up tickets.
