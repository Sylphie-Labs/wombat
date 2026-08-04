# Wombat launch-verification protocol (TK-361, DEC-84, DEC-85)

This document is the phase gate into desktop launch verification. It is
written so a fresh session with no other context can follow it. Read this
whole file before running or writing any sweep.

## Why this phase exists (DEC-84)

A ticket marked **done** means its acceptance criteria passed a runnable
check when it landed. It has **never** meant that a human or an agent
exercised the feature in the running product. The board percentage is not a
launch signal and must never be read as one.

The evidence is in this repo's own history: at contract v2.95 the board was
green and the full test suite passed. Then ONE live drive of a hand-written
walkthrough (the predecessor of this document, `MANUAL_TEST.md`) found two
defects that would have shipped:

- **ISS-15** — a deterministic exit-139 crash loop that killed every boot of
  `python -m wombat` about five minutes in, with zero log output.
- **ISS-16** — the app header reading "Running" while the runtime was dead,
  because it keyed off a handshake file's presence, not a live process.

Neither was reachable from any test in the tree — no test runs a process for
five minutes, and no test observes a header against a corpse. That is why
this phase exists, and why it runs on the real product, not on the test
suite.

## (a) Result vocabulary: PASS / FAIL / BLOCKED

Every check in every sweep resolves to exactly one of three values. There is
no fourth value and no partial credit.

- **PASS** — the check ran, and what was observed matches what was
  expected. A PASS must name an artifact (see the evidence rule below).
- **FAIL** — the check ran, and what was observed did NOT match what was
  expected. A FAIL is routed as a new issue (see finding routing below); it
  is never silently downgraded to a note.
- **BLOCKED** — the check could not be run at all: no credential, no
  hardware, no service available, no operator present for a class B check
  that needed one. BLOCKED is **not** a soft FAIL (the feature may be fine;
  the check simply didn't happen) and it is **not** a PASS (nothing was
  proven). A BLOCKED check must state why it was blocked and what would
  unblock it, so a later sweep can retry it deliberately rather than by
  accident.

## (b) The evidence rule

**Every PASS names an artifact. Prose alone is never a PASS.**

Acceptable artifacts:

- a log line, with its file path and timestamp (e.g.
  `logs/runtime-20260803-091500.log:47`)
- a SQL row count (e.g. `SELECT count(*) FROM wombat_external_items ...` and
  the number returned)
- an HTTP status plus body (e.g. `GET /chat -> 200 {"ok": true}`)
- a screenshot path (from the computer-control MCP's screenshot tool)
- a written-file path (e.g. the brief file, a drained artifact's JSON)

A sweep record that says "chat worked" is not a PASS. A sweep record that
says "chat worked — sent 'hello' at 09:14, held-response logged at
`logs/runtime-20260803-091412.log:88`, screenshot
`planning/verification/evidence/08-voice-loop-chat.png`" is a PASS.

## (c) The DEC-85 three-way check classification

Every check in every sweep is marked with exactly one class. An unclassified
check is a defect in that sweep's script, not an acceptable gap.

- **Class A — agent-driven.** Driveable by an Opus-class agent through the
  computer-control MCP against the real Electron window, plus direct
  observation of logs, SQL rows, HTTP responses, and written files. This is
  the same tool that found ISS-15 and ISS-16, and it is the majority of the
  phase.
- **Class B — operator-only.** Nothing in this repo can hear, speak into a
  microphone, or approve a Google consent screen as the operator. Class B
  covers checks that need a human sense or a human credential: hearing TTS
  output, speaking into a microphone, approving an OAuth consent screen,
  judging whether a persona axis reads differently, physically observing a
  machine restart. These need Jim.
- **Class C — instrumented substitute.** Recorded **alongside** every class
  B check, never instead of it, so a human judgement always has a number
  next to it: measured milliseconds to first sound beside "did it start
  sooner", WAV duration and sample rate beside "did it play", a diff against
  a pinned baseline beside "does it read the same". A future regression then
  has something to fail against instead of a memory of how it sounded.

## (d) Finding routing

A **FAIL is routed as a new ISS-\* to the architect and is never fixed
inside a sweep.** No test file, no source file, and no acceptance criterion
is edited to make a sweep go green. A sweep that starts repairing loses its
own thread, changes the thing it is measuring mid-measurement, and produces
a record nobody can trust. This is the ISS-15 / ISS-16 routing precedent
exactly: both were found by a walkthrough, both were filed as issues, and
both were fixed by separate tickets later.

## (e) The done-bar (DEC-85(c))

**A sweep ticket is done when the sweep RAN against every named check and
every FAIL was ROUTED — not when everything passed.**

This is deliberate. If a sweep ticket's done-bar required every check to
pass, a genuinely broken feature would hold its own verification ticket open
forever, and the pressure to record a soft PASS in order to close the ticket
would be enormous. Whether the PRODUCT passed is asserted in exactly ONE
place: TK-377, the phase close-out. No other ticket in this phase may claim
the product passed, and no sweep is blocked on another sweep's find-rate.

## (f) Per-area file layout

Each sweep owns one file at `planning/verification/`, serving as both its
script and its run record, in this exact order:

1. `planning/verification/00-armed-live-suite.md` — the armed
   `WOMBAT_TEST_*_LIVE` run
2. `planning/verification/01-bring-up-and-liveness.md`
3. `planning/verification/02-queue-gate-drain-mouth.md`
4. `planning/verification/03-chat-and-charter-honesty.md`
5. `planning/verification/04-google-ingestion.md`
6. `planning/verification/05-morning-brief.md`
7. `planning/verification/06-dream-and-user-model.md`
8. `planning/verification/07-behavior-and-reflection.md`
9. `planning/verification/08-voice-loop.md`
10. `planning/verification/09-browser-and-computer-use.md`
11. `planning/verification/10-privacy-safety-egress.md`
12. `planning/verification/11-app-surface.md`
13. `planning/verification/12-persona.md`
14. `planning/verification/13-companion-listener.md`
15. `planning/verification/14-archive-then-wipe.md`
16. `planning/verification/99-close-out.md`

This ticket (TK-361) creates only this file, `protocol.md`. It does not
create the sixteen files above — each sweep ticket creates its own file when
it runs. An empty skeleton file is indistinguishable from a sweep that ran
and found nothing, which is precisely the rubber-stamp failure mode RISK-12
exists to prevent.

## (g) Ordering rule

`14-archive-then-wipe.md` is the **destructive** sweep — it archives then
wipes accumulated state that every other sweep depends on — and it runs
**last**, after every other sweep has completed. `99-close-out.md` (TK-377)
runs after it, and is the only place the product's overall pass/fail is
adjudicated.

---

## Bring-up (absorbed from `MANUAL_TEST.md` Parts A and B)

A start-to-finish walkthrough for getting the product running by hand, on
this machine, with no undocumented knowledge required.

### Start the database

The core runtime refuses to boot without Postgres.

1. Start Docker Desktop if it isn't running.
2. Start the container: `docker start wombat-runtime-db`
3. **Expected:** `docker ps` lists `wombat-runtime-db` with port `5436`
   mapped.

### Start the core runtime

1. From the repo root, run `scripts/wombat-console.ps1`. This is the
   supported launch path: it starts `python -m wombat` inside a visible,
   detached console hosting a relaunch-with-backoff watchdog (DEC-52b), and
   the runtime writes its own per-boot log file. (Running
   `uv run python -m wombat` directly in a foreground terminal also boots
   the runtime, but without the watchdog or the documented kill affordance —
   prefer `scripts/wombat-console.ps1`.)
2. **Expected:** a new log file appears in `logs/` named
   `runtime-<yyyyMMdd-HHmmss>.log`, and the hosted console stays open (it is
   a long-lived process).
3. Open that newest log and check:
   - **PASS:** no line containing `source not wired`, and no
     `invalid_grant` / `RefreshError`.
   - **FAIL:** a line reading `gmail source not wired: stored credential
     failed to refresh` (or the `gcal` equivalent) means the Google token is
     revoked. Wombat keeps running but pulls zero email/calendar. Go do the
     Google section below, then restart the runtime.
4. **Expected:** the chat handshake file (path from the `.env`
   `WOMBAT_CHAT_HANDSHAKE_FILE` setting, e.g.
   `C:\Users\Jim\wombat-data\chat-handshake.json`) exists and was just
   rewritten. It should contain a `port` and a `token`.

To stop the runtime cleanly, run `scripts/stop-wombat.ps1` — this is the
repo's one stop implementation (it force-kills the watchdog host and the
runtime, then proves both gone). To stop and immediately start a fresh one,
run `scripts/restart-wombat.ps1`, which invokes `stop-wombat.ps1` and then
`wombat-console.ps1` in sequence.

### Start the desktop app

1. `cd app`, then `npm start` (first time: `npm install` first, and make
   sure `uv sync --extra settings-app` has been run from the repo root).
2. **Expected:** the Electron window opens. There is no browser URL — the
   UI is the desktop window only.
3. **Expected:** the header shows **Running**, not "Offline" or
   "Checking...".

**Corrected liveness note (supersedes the original MANUAL_TEST.md
heuristic):** the header used to derive "Running" from the mere presence of
the chat-handshake file, checked once at mount. That heuristic is FALSE — it
survives a runtime death, and ISS-16 proved live that its mtime can be fresh
while the port behind it is dead. TK-263 replaced it: the header now calls
`probeChat()` (`app/src/chat.ts`), which re-reads handshake info fresh and
issues a real round-trip HTTP request to the chat port, on an interval. Any
HTTP response (including a 4xx) proves the process alive; a thrown fetch or
null info means Offline. Treat the header's "Running" as trustworthy on the
current tree; do not reintroduce mtime-only reasoning in any sweep.

### Chat

Chat is served by the core runtime process, not the app. The app just reads
the handshake file to find it.

1. With the runtime running, open the app's chat pane and send a message
   like "hello".
2. **Expected:** a reply, or a "held" response (held is fine — that's the
   gate holding the message, still counts as online).
3. **If the header says "Offline":**
   - Is the runtime console still open? If it exited, chat is genuinely
     offline — restart it (`scripts/restart-wombat.ps1`) and the app will
     pick it up on its own (no app restart needed; the header re-probes on
     its interval).
   - Check `.env` has `WOMBAT_CHAT_HANDSHAKE_FILE` set. If it's blank, the
     runtime skips chat entirely.

---

## Google connections (absorbed from `MANUAL_TEST.md` Parts C and D)

Covers Gmail and Google Calendar reconnect, and confirming sync actually
resumed.

### Reconnect Gmail and Calendar

1. In the app, open the **API Keys** view. Find the **Google connections**
   panel — two rows: Google Calendar and Gmail.
2. Read the status on each row:
   - **Connected** — nothing to do.
   - **Expired** — the stored token is revoked. Continue below.
   - **Not connected** — no token stored at all; same steps below.
3. Click **Reconnect** (or **Connect**) on the Gmail row.
4. **Expected:** the row shows "Waiting for you to approve in the
   browser...", and the system browser opens a Google consent screen.
5. Approve in the browser with the correct account.
6. **Expected:** the row flips to **Connected**, and a notice appears:
   "Restart Wombat so it picks up the new connection."
7. Repeat steps 3–6 for the Google Calendar row.
8. **Restart the runtime**: run `scripts/restart-wombat.ps1`. Sources only
   wire at boot; skipping this step means email stays stuck even though the
   rows say Connected.
9. **Fallback if the in-app button fails:** the CLI does the same thing —
   `python -m wombat.integrations.gmail.auth` then
   `python -m wombat.integrations.gcal.auth`, then restart the runtime.

### Confirm email sync actually resumed

1. Open the newest `logs/runtime-*.log` from the post-reconnect boot.
2. **PASS:** no `source not wired` lines; no `invalid_grant`.
3. Wombat polls Gmail on an interval over a rolling 24-hour inbox window, so
   anything that arrived in the last day gets picked up shortly after boot.
   Send a test email and watch for it to be processed (or check the
   `wombat_external_items` table in Postgres for a fresh `first_seen_at`).
4. The morning brief file should show real calendar/inbox content on the
   next run, not empty slices.

### Known gotcha — the 7-day token death

If the Google connection dies again roughly a week after reconnecting, that
is the classic signature of the OAuth consent screen sitting in **Testing**
mode in Google Cloud Console (Testing-mode refresh tokens expire after 7
days). Check the consent screen's publishing status is **In production** —
that makes the token long-lived and this whole problem stops recurring.
