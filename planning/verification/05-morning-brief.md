# 05 — The morning brief (TK-367, SWEEP 05)

Run date: 2026-08-05, one continuous live session per `protocol.md`, one sweep
at a time per DEC-86.

**Read this first — it shapes every result below.** This sweep ran in the
afternoon of a day whose brief had **already fired at 09:15:21**. That is not a
handicap; it is the single best condition available for AC2 (the exactly-once
fence, which is the property that actually protects the user) and it makes AC1's
evidence *real production output* rather than something manufactured for the
sweep. It does mean AC1's fire was reconstructed from artifacts rather than
watched, which is stated plainly below and never glossed.

Nothing was forced by editing a ledger value or the brief time — TK-367's
non_goals forbid both, and no exception was taken.

| AC | Check | Class | Result |
|---|---|---|---|
| AC1 | one brief, real content, ledger marked, content verified item by item | A | **PASS** (with one named partial: the calendar half contributed nothing, because the calendar was genuinely empty) |
| AC2 | no second brief, across re-fire and full restart | A | **PASS** — the strongest artifact in this sweep |
| AC3 | honest empty state (no events *and* no mail) | A | **BLOCKED** — the condition cannot be created on this host; two partial observations recorded |
| AC4 | spoken, and terse rather than a full read-aloud | B | **BLOCKED** — needs Jim's ear. Class C substitute recorded |

---

## AC1 — one brief, real combined content, ledger marked — **PASS (partial named)**

Today's brief fired on the day's first boot after 07:00 (the miss-catch fire
path), on a wombat-day it had not yet run. Every artifact survives.

**Artifacts.**

- `C:\Users\Jim\wombat-data\brief.md`, entry
  `[run=wombat-brief-2026-08-05] delivered_at=2026-08-05T09:15:21.432886-04:00`
- boot log `logs/runtime-20260805-091501.log`
- ledger row: `SELECT value FROM daily_ledger WHERE ledger_name='brief:run' AND
  wombat_date='2026-08-05'` → **1**

**The delivered text, verbatim:**

> Mail's flagged. "Jim, Urgent: Your Capital Bank, N.a. account is in Default!"
> — from admin@connect.halstedfinancialservices.com. Sounds like a phishing
> expedition, not a bank statement; delete and don't click the "cure."

**Checked item by item against the store, not read for plausibility** — this is
the part of the AC that matters, so it was done as a lookup, not an impression:

| brief claim | store row | match |
|---|---|---|
| subject `Jim, Urgent: Your Capital Bank, N.a. account is in Default!` | `wombat_external_items` source=gmail | **verbatim** |
| sender `admin@connect.halstedfinancialservices.com` | same row | **verbatim** |
| "Mail's **flagged**" (i.e. exactly one item worth raising) | that row is the **only** `priority_band='high'` item among **67** gmail rows received in the 24h window before delivery; the other 66 are `normal` | **correct selection, not just a correct quote** |

That last line is the one worth keeping: the brief did not merely quote a real
email, it picked the *only* high-band item out of 67 and left the other 66 —
newsletters, job alerts, retail — out. The selection is right, not just the
citation.

**The named partial.** The AC says "real combined **calendar and** inbox
content". The calendar contributed **nothing**, and the reason is that there was
nothing to contribute: `wombat_external_items` holds exactly 3 `gcal` rows, all
in the past (`2026-07-12`, `2026-07-13`, `2026-08-02`), none on 2026-08-05. The
brief correctly said nothing about the calendar rather than inventing an event or
falsely claiming the source was down (see AC3 for why that distinction is load-
bearing). So the product behaved correctly, but **a genuinely combined two-source
brief was not exercised today and is not proven by this sweep.** What would prove
it: any wombat-day carrying at least one calendar event. Not routed as a finding
— nothing was observed to contradict expectation — but deliberately not counted
as covered either.

## AC2 — no second brief, across re-fire and full restart — **PASS**

The strongest result in this sweep, and it needed no construction at all.

**Ten runtime boots on 2026-08-05. Exactly one brief.**

```
runtime-20260805-091501.log   <- the fire: brief delivered 09:15:21
runtime-20260805-092703.log
runtime-20260805-092840.log
runtime-20260805-100815.log
runtime-20260805-101425.log
runtime-20260805-110610.log
runtime-20260805-111032.log
runtime-20260805-134028.log   <- this sweep's boot
runtime-20260805-134601.log   <- this sweep's second boot, after a full stop
(plus the 09:15 boot above)
```

`grep -c 'run=wombat-brief-2026-08-05' brief.md` → **1**
`daily_ledger brief:run 2026-08-05` → **1**, before and after
`sha256sum brief.md` → `0bf68e7e16af111d998b5baf516f80ccd7f8b06c6d41272cb82a030ddb0e65e3`,
**byte-identical** across both of this sweep's boots (captured before the first,
re-captured after the second).

Two boots in this sweep were driven deliberately: one plain restart, and one
after a full `scripts/stop-wombat.ps1` teardown confirmed down to zero `-m
wombat` processes. Neither produced a second brief.

**Why this is positive proof and not just absence.** A fence that never runs
would look identical to a fence that runs and skips. It is distinguishable here
because *the same timer fired the brief at 09:15 on this very day* — so the
stage demonstrably runs, and the nine subsequent boots demonstrably declined to
fire again. The mechanism is exercised in both directions on one wombat-day.

The wider file corroborates it: **16 brief entries across 16 distinct dates**
(2026-07-09 → 2026-08-05), **zero duplicate dates**, spanning many restarts and
at least one power loss.

**One observability limitation, routed as ISS-59 (minor, not a FAIL).** The skip
decision itself is logged at `DEBUG` (`brief_timer_stage.py`: "brief already ran
this wombat-day"), and `__main__.py` hardcodes `root.setLevel(logging.INFO)` with
no override, so a sweep cannot *directly* observe the fence choosing to skip
without editing source — which protocol.md (d) forbids. The AC is satisfied by
state and by the 09:15 fire, but the decision is invisible in the log where an
operator would look for it. Same family as ISS-56.

## AC3 — honest empty state — **BLOCKED**

The AC's condition is a day with **no calendar events *and* no recent mail**.
That condition cannot be created on this host: Jim's inbox receives mail
continuously (67 messages in the last 24h alone), and the brief gathers from the
**live Google fetch seams** — `CalendarPoller.fetch_window` /
`GmailPoller.fetch_recent`, read directly by `BriefGatherStage`, **not** from
`wombat_external_items` — so pointing the runtime at an empty database does not
produce an empty *brief*. That was tested, not assumed (see below).

**What would unblock it:** wombat wired to a Google account with an empty inbox
and empty calendar. That needs Jim (a credential only he can authorize), which
makes the full check class B in practice despite being class A in principle.

Two partial observations recorded so a later sweep starts ahead:

**(a) Empty calendar, source wired and working — honest. PASS in isolation.**
Today's 09:15 brief ran against a **live, working** calendar that returned **zero
events**: `logs/runtime-20260805-091501.log:2` shows
`gcal: stored access token expired — refreshing non-interactively` (wired and
refreshed), and the boot contains **no** `brief_gather: calendar source
unavailable` warning — so `fetch_window` *succeeded* and returned empty. The
brief then simply omitted the calendar. It did not invent an event, and it did
not claim the source was down. That is the honest empty behavior, on the real
product, for one of the two halves.

**(b) Both sources unavailable — honest, and a different case than AC3.**
A throwaway runtime was booted against a fresh empty Postgres (`:5540`, torn
down after) with the Google client credentials blanked **in the process
environment only** — never `.env`, never the production database. It delivered:

> `[run=wombat-brief-2026-08-05] delivered_at=2026-08-05T13:44:11.367917-04:00`
> Calendar's down. Gmail's down. That's all.

Honest, short, no invention, no crash, and the ledger/one-brief mechanic was
watched firing live on a fresh ledger. **But this is the source-*unavailable*
degrade, not AC3's source-*empty* case**, and it is recorded as such rather than
allowed to stand in for it — the whole point of AC3 is the difference between
"nothing happened today" and "I couldn't look."

*(Method note for whoever runs this next: blanking a credential in PowerShell
must use `" "` — a single space. `$env:FOO=""` **deletes** the variable, and the
override silently does not happen. Cost a boot on a previous sweep; applied
correctly here, verified `length 1` before launch.)*

## AC4 — spoken, and terse — **BLOCKED (class B), class C substitute recorded**

Nothing in this repo can hear. Per DEC-85 this needs Jim, and it is recorded
BLOCKED rather than assumed.

**Class C instrumented substitute, recorded alongside:**

- `logs/runtime-20260805-091501.log:12` —
  `POST https://api.fish.audio/v1/tts "HTTP/1.1 200 OK"` at **09:15:22,337**,
  i.e. **0.9 s after** the brief's own `delivered_at=09:15:21.432`. A real
  synthesis call for the brief was made and succeeded.
- Voice is genuinely on for this host: `WOMBAT_VOICE_ENABLED=true`,
  `WOMBAT_TTS_PROVIDER=fish`.
- Buffered, not streamed, this boot: `voice: fish TTS streaming playback is
  unavailable — install the 'voice-cloud' extra's sounddevice dependency`
  (`:3`). Recorded because it bears on any latency judgement Jim makes.
- **Terseness, measured:** the delivered brief is **230 characters, one
  paragraph, 3 sentences.** The spoken channel is that same text — this is not a
  full read-aloud of a longer document, because no longer document exists.
- A second TTS call at `09:15:45` (23 s later) is **not** a second brief —
  `brief.md` holds one entry for the day. Noted so a later reader does not
  mistake it for one.

**What Jim is owed, precisely:** confirm he *heard* the morning brief spoken, and
that it read as terse rather than droning. If it was never audible, that is a
new ISS and the class C evidence above says the failure is downstream of
synthesis (the API returned 200), which narrows it to playback.

---

## Findings routed

- **ISS-59 (new, minor)** — the once-daily brief skip decision is `DEBUG`-only
  and the log level is hardcoded `INFO` with no override, so the fence's decision
  cannot be observed from the log an operator would read. Not a FAIL: AC2 passed
  on state and on the same-day fire. Same family as ISS-56.

No other findings. **No FAILs in this sweep.**

## State left behind

- Real runtime **UP**, boot `logs/runtime-20260805-134601.log`, zero
  `Traceback`/`CRITICAL`/`terminating` lines.
- Throwaway Postgres `:5540` **stopped and removed** (`--rm`); zero `tk367`
  containers remain. Live `wombat-runtime-db` untouched — `brief:run` for
  2026-08-05 still **1** after teardown.
- Zero `-m wombat` processes were left orphaned between phases; each transition
  was verified to 0 before the next launch.
- Production `brief.md` **byte-identical** to its pre-sweep hash. Nothing this
  sweep did wrote to it.
- No destructive operation — that is TK-376's alone.

## What this sweep does NOT claim

Per DEC-85(c): the sweep ran and its findings were routed. It does **not** assert
the morning brief has PASSED. Two of four ACs are BLOCKED and one carries a named
partial. TK-377 alone adjudicates.
