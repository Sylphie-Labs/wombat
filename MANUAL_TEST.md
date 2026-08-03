# Wombat — manual test guide

A start-to-finish walkthrough for running wombat by hand and checking that
everything works. Written 2026-07-18, when the live symptoms were: emails stuck
since last Friday, chat showing "offline". Both trace to one cause — the stored
Google OAuth token was revoked — so Part C covers the reconnect flow in detail.

---

## Part A — Start everything

### A1. Start the database

The core runtime refuses to boot without Postgres.

1. Start Docker Desktop if it isn't running.
2. Start the container: `docker start wombat-runtime-db`
3. **Expected:** `docker ps` lists `wombat-runtime-db` with port `5436` mapped.

### A2. Start the core runtime

1. From the repo root: `python -m wombat` (use the `.venv` python, or `uv run python -m wombat`).
2. **Expected:** it stays running (it's a long-lived process — leave the terminal open). A new log file appears in `logs/` named `runtime-YYYYMMDD-HHMMSS.log`.
3. Open that newest log and check:
   - **PASS:** no line saying `gmail source not wired` or `gcal source not wired`, and no `invalid_grant` / `RefreshError`.
   - **FAIL (current known state):** you see `gmail source not wired: stored credential failed to refresh` — the Google token is revoked. Wombat keeps running but pulls **zero** email/calendar. Go do Part C, then restart this process.
4. **Expected:** the chat handshake file `C:\Users\Jim\wombat-data\chat-handshake.json` exists and was just rewritten (check its modified time). It should contain a `port` and a `token`.

### A3. Start the desktop app

1. `cd app`, then `npm start` (first time: `npm install` first, and make sure `uv sync --extra settings-app` has been run).
2. **Expected:** the Electron window opens. There is no browser URL — the UI is the desktop window only.
3. **Expected:** the header shows **Running** (not "Offline").

---

## Part B — Chat

Chat is served by the `python -m wombat` process, not the app. The app just
reads the handshake file to find it.

1. With the runtime from A2 running, open the app's chat pane and send a message like "hello".
2. **Expected:** you get a reply, or a "held" response (held is fine — that's the gate holding the message, still counts as online).
3. **If it says "Wombat is not running — start it to chat" / header says "Offline":**
   - Is the `python -m wombat` terminal still alive? If it exited, chat is genuinely offline — restart it (A2) and the app will pick it up on its own (no app restart needed; it re-checks every time).
   - Check `.env` has `WOMBAT_CHAT_HANDSHAKE_FILE` set (line ~32). If it's blank, the runtime skips chat entirely.
   - Check the handshake file's modified time. If it's older than the last runtime boot, it's stale — restart the runtime.

---

## Part C — Google connections (Gmail + Calendar)

This is the fix for "emails stuck since Friday". A revoked token can't be
refreshed — it must be cleared and re-consented. The app now does this for you.

1. In the app, open the **API Keys** view. Find the **Google connections** panel — two rows: Google Calendar and Gmail.
2. Read the status on each row:
   - **Connected** — nothing to do.
   - **Expired** — the stored token is revoked (the current state). Continue below.
   - **Not connected** — no token stored at all; same steps below.
3. Click **Reconnect** (or **Connect**) on the Gmail row.
4. **Expected:** the row shows "Waiting for you to approve in the browser...", and your system browser opens a Google consent screen.
5. Approve in the browser with the jctisdale1988 account.
6. **Expected:** the row flips to **Connected**, and a notice appears: *"Restart Wombat so it picks up the new connection."*
7. Repeat steps 3–6 for the Google Calendar row.
8. **Restart the runtime** — stop the `python -m wombat` terminal (Ctrl+C) and start it again. Sources only wire at boot; skipping this step means email stays stuck even though the rows say Connected.
9. **Fallback if the in-app button fails:** the CLI does the same thing:
   `python -m wombat.integrations.gmail.auth` then `python -m wombat.integrations.gcal.auth`, then restart the runtime.

---

## Part D — Confirm email sync actually resumed

1. Open the newest `logs/runtime-*.log` from the post-reconnect boot.
2. **PASS:** no `not wired` lines; no `invalid_grant`.
3. Wombat polls Gmail on an interval over a rolling 24-hour inbox window, so anything that arrived in the last day gets picked up shortly after boot. Send yourself a test email and watch for it to be processed (or check the `wombat_external_items` table in Postgres for a fresh `first_seen_at`).
4. The morning brief at `C:\Users\Jim\wombat-data\brief.md` should show real calendar/inbox content on the next run, not empty slices.

---

## Known gotcha — the 7-day token death

If the Google connection dies again roughly a week after reconnecting, that is
the classic signature of the OAuth consent screen sitting in **Testing** mode
in Google Cloud Console (Testing-mode refresh tokens expire after 7 days).
Check the consent screen's publishing status is **In production** — that makes
the token long-lived and this whole problem stops recurring.
