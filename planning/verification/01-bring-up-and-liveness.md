# 01 — cold bring-up and runtime liveness (TK-363, SWEEP 01)

Run date: 2026-08-04, one continuous live session. Vocabulary and evidence
rule per `planning/verification/protocol.md`. Nothing found here was fixed —
findings are routed, not repaired, inside a sweep.

## AC1 — cold bring-up

**PASS.**

- Docker: `wombat-runtime-db` already up (`docker ps`, port 5436 mapped).
  Jim had already closed the runtime and app before this sweep started
  (confirmed no `-m wombat` or `electron.exe` wombat processes running).
- Launch: `scripts/wombat-console.ps1` run 20:17:33 — exit 0, its own
  bounded-wait CIM assert confirmed exactly one ROOT `-m wombat` process.
- Boot log: `logs/runtime-20260804-201733.log` created, non-empty. No
  `gmail source not wired` / `gcal source not wired` / `invalid_grant` lines —
  both Google tokens refreshed successfully against the real OS keyring and
  real Google endpoints (`gmail: stored access token expired — refreshing
  non-interactively`, same for gcal, both followed by normal boot
  continuation with no error).
- Schema preflight: `src/wombat/schema_preflight.py` emits no log lines of
  its own (no `logger`/`print` calls at all — silent unless it must raise).
  So "preflight lines in the boot log" as literal text is not a thing this
  codebase produces; the actual evidence is that the boot proceeded with no
  preflight error and every Postgres-backed feature exercised later in this
  session (gate decisions, chat, settings) worked, which is only possible if
  all nine migrations were already applied cleanly. Recording this precisely
  rather than inventing lines that don't exist.
- Handshake file `C:\Users\Jim\wombat-data\chat-handshake.json` rewritten at
  20:17:40 (7s after boot start), containing a live port. `GET /` on that
  port returned HTTP 404 — a real response, proving the process alive
  (protocol.md's evidence rule: any HTTP response, including 4xx, counts).
- App: `cd app && npm start` opened the Electron "Wombat" window. Screenshot
  `C:\Users\Jim\Downloads\screenshot_20260804_202036_a40e7efa.png` shows the
  hosted watchdog console with the boot log. Screenshot
  `C:\Users\Jim\Downloads\screenshot_20260804_202143_73ddf136.png` shows the
  app header reading **Running**, with real inbox content in the Today view
  (six real Gmail messages) — proving Gmail is genuinely wired live, not
  just that the token refreshed.

## AC2 — singleton lock

**PASS**, tested at two independent layers, both refused, first runtime
unaffected.

- Launcher layer: re-running `scripts/wombat-console.ps1` while the first
  boot was up was refused: `wombat runtime already running (1 matching root
  process(es) found); refusing to start a second instance (ASMP-2).` — exit
  1.
- Application layer: invoking `.venv\Scripts\python.exe -m wombat` directly
  (bypassing the launcher entirely) was refused by the app's own singleton
  guard: `wombat runtime already running: singleton port <port> (config
  field wombat_singleton_port) is already bound — refusing to start a second
  instance.` — exit 1, clean immediate exit, no lingering process.
- First runtime confirmed unaffected: `GET /` on its port still returned 404
  immediately after both refusal attempts.

## AC3 — faulthandler-in-log

**PASS**, via a documented equivalent rather than crashing the monitored
sweep boot itself (preserves continuity for AC4/AC5/AC6, which needed that
same boot to keep running).

- A throwaway, separate process imported wombat's own production
  `_configure_logging()` (`src/wombat/__main__.py`, DEC-53b — the exact
  function `python -m wombat` calls) to set up a real per-boot log file with
  faulthandler enabled against it, then called `faulthandler._sigsegv()` — a
  genuine native access violation, the same mechanism CPython's own test
  suite uses to prove faulthandler wiring.
- Result: `logs/runtime-20260804-202443.log` contains `Fatal Python error:
  Segmentation fault` plus the full thread stack, landed in the runtime-owned
  log file exactly as DEC-53b promises — the ISS-15 lesson (a native fault
  leaves plain logging silent; faulthandler must catch it) is pinned and
  confirmed working on this tree.
- The live sweep boot (port from AC1) was confirmed still responding 404
  immediately after, untouched by this throwaway-process test.

## AC4 — app header truth (ISS-16 re-test)

**PASS. ISS-16 CLOSED** — re-tested and confirmed fixed.

- Before: header read **Running** (see AC1's screenshot,
  `screenshot_20260804_202143_73ddf136.png`, 20:21:43).
- Runtime killed cleanly via `scripts/stop-wombat.ps1` (20:25:29 start,
  20:25:32 complete, exit 0).
- Without restarting the Electron app, the header flipped to **Offline**,
  confirmed by screenshot ~6s after the stop completed (20:25:38) — well
  within TK-263's 5-second `POLL_INTERVAL_MS`. The header now genuinely
  tracks a live chat-port round-trip (`probeChat()`), not a handshake file's
  mtime — the exact lie ISS-16 recorded does not reproduce.

## AC5 — long-run watch (the ISS-15 check)

**PASS — no death, exit status unobserved.** This is the single most
valuable check in the phase and it is clean.

- Fresh boot launched 20:26:04–06 (`logs/runtime-20260804-202607.log`),
  confirmed single root process by the launcher's own assert.
- Watched continuously for **360 seconds (6 minutes)** — past ISS-15's
  roughly-five-minute crash mark — polling the chat port every 18s:

  ```
  t=18s .. t=360s: chat_http=404 at every single check, zero gaps
  ```

- Real items flowed during the window, all logged live: a screenpipe
  `context_switch` event gated (`action=hold`), a chat item gated
  (`urgency=0.5225 load=0.5`), a real `POST
  https://api.deepseek.com/v1/chat/completions` returning 200, a daily-ceiling
  flush-denied decision, and a gmail item gated — the gate, compose dispatch
  and Postgres-backed ceiling logic all exercised live, not idle.
- Process was still alive with exit status unobserved at the end of the
  window. The ISS-15 shape (silent exit-139 crash loop, zero log output)
  did not recur on this boot.

## AC6 — ISS-45 concurrency probe (routed from ISS-45, not optional)

**PASS (recorded) — inconclusive on ISS-45's full severity claim.** Numbers
below are the named artifact per protocol.md; whether they "look broken" is
for the architect reading this alongside TK-364's and TK-375's own
measurements (ISS-45's own stated close trigger).

**Attempt 1** — a typed `POST /chat` message. The gate held the item
(`action=hold`, not a registered ASR `voice_turn`), and
`sinks/speak.py`'s own `held_chat and not voice_turn` skip condition means
speak() never ran for this attempt — text-only "replied" response, no TTS.
Still useful data: during the 4.089s compose call (real DeepSeek round trip),
concurrent `GET /` and `GET /settings` probes every ~0.3s were mostly
1.8–3.5ms, **except one simultaneous stall on both endpoints: 745.8ms /
746.1ms, at t=+0.759s to +1.505s into the compose call.** Root cause not
determined (could be the compose stage's own event-loop use, not speak — no
speak() ran in this attempt); recorded as observed, not diagnosed.

**Attempt 2** — to get a genuine spoken reply, a short WAV
("Hey wombat, what's on my calendar today?") was synthesized locally via
Windows SAPI (no external cost) and dropped into `WOMBAT_ASR_DROP_DIR`,
producing a real ASR-registered `voice_turn`. Confirmed live: ASR picked it
up (20:38:01.855), transcribed via a real `POST
https://api.fish.audio/v1/asr` (200 OK), gated, composed via a real DeepSeek
call, then **a real `POST https://api.fish.audio/v1/tts` returned 200 OK at
20:38:05.733** — a genuine spoken reply was triggered. A `GET /` probe loop
every 0.3s was started as soon as this was observed in the log (~8–10s after
the TTS call returned, limited by real tool round-trip latency reading the
log first) and ran 19s: all responses 9–44ms, **no stall observed** — but
this probe window likely started after buffered playback had already
finished, so it does not cleanly bracket the actual `speak()` call and
**cannot be read as a refutation of ISS-45.**

**Net:** two live attempts, one genuine ~746ms simultaneous stall observed
(cause undetermined, coincides with a compose call, not confirmed
speak-related), one attempt that reached real TTS but missed the timing
window. Recommend a follow-up with a tighter trigger (start probing the
instant the Fish TTS response is logged, in the same process, rather than
across separate tool calls) if the architect wants a conclusive
speak()-window measurement. Not fixed here — ISS-45 stays open pending
TK-364's and TK-375's own measurements per its stated close trigger.

## Clean shutdown

`scripts/stop-wombat.ps1` run 20:40:00, exit 0. Confirmed via CIM query: no
process matching `-m wombat` or the `wombat-watchdog-host` marker remains.
(Two long-lived `python -m wombat.settings_app` processes remain — a
separate, pre-existing companion process untouched by this sweep and out of
`stop-wombat.ps1`'s scope by design, per ISS-24's root-match anchoring.)
`wombat-runtime-db` left running (Docker containers are not part of the stop
path). No destructive/wipe operation performed — that is TK-376's alone.

## Findings summary

No FAILs this sweep. AC6's timing gap in Attempt 2 is not a FAIL of this
sweep's own checks — the probe ran and its numbers are recorded, which is
the done-bar (DEC-85(c)). ISS-45 remains open, fed by this record, closing
per its own stated trigger once TK-364 and TK-375 add theirs.
