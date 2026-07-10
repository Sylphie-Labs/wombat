# wombat

A personal assistant agent built on the [**cog-worx**](../cog_worx/cog-worx) cognitive-architecture
framework — the loop, memory, verification, and perception, out of the box.

wombat is a queue-driven **deterministic** loop, not an agent loop: sources enqueue candidate
items into a durable Postgres queue, a deterministic Gate (pure code, never a model call) decides
what's worth surfacing, and only cleared items reach the LLM. **The LLM is a mouth** — called
only to phrase pre-decided output into terse natural language, never to decide whether or when to
act (`wombat.gate.pipeline` is model-free by construction).

## Run

```bash
uv sync                 # install (Python 3.13+); cog-worx resolves from ../cog_worx/cog-worx (editable)
python -m wombat         # boot the ONE standing process (wombat.runtime.serve())
```

`serve()` assembles the composition (`wombat.bootstrap.assemble_runtime`), fires one initial
drain drive, then runs forever off the shipped cog-worx `Sweeper` — there is no other loop.
Shutdown is a cooperative `CancelledError`/`KeyboardInterrupt`; the queue/daily-ledger/pending-
journal connections close best-effort in a `finally`.

**Required environment** (`wombat.config.REQUIRED_ENV` — absent any of these, `load_config()`
raises `ConfigurationError` naming the first missing one):

- `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL` — the mouth's model egress.

**`WOMBAT_PG_DSN`** backs the queue/daily-ledger/pending-journal (the queue is Postgres-only).
It is deliberately **not** part of `REQUIRED_ENV` — tests, the demo script, and a Google-less
boot must stay bootable without it — but `wombat.runtime.serve()` itself requires it and raises
`ConfigurationError` naming it before starting.

**Optional environment:**

- `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` — the gcal/gmail sources. Absent (or no
  stored OAuth token yet), each source is skipped with a loud log — the drain spine still boots
  Google-less. One-time interactive consent: `python -m wombat.integrations.<gcal|gmail>.auth`.
- `WOMBAT_BRIEF_PATH` — the morning brief's append-only text-sink file path. Set -> both the
  `wombat.brief` and `wombat.brief_schedule` pathways register at boot. Blank/absent -> both are
  skipped together (a loud warning, no crash) and the drain spine boots without them.
- `WOMBAT_VOICE_ENABLED` — `true`/`false` (default `false`); gates voice delivery of the brief
  alongside text once `WOMBAT_BRIEF_PATH` is set.

## Pathways

`assemble_runtime` registers up to three cog-worx pathways on one Engine/one Postgres (ASMP-2 —
exactly one draining `WombatQueue` process-wide):

- **`wombat.drain`** — always registered. `DrainQueueStage -> GateStage -> ReviewOrSpeakStage ->
  ComposeDispatchRouter -> ComposeStage`: drains the queue, gates deterministically, and the
  mouth phrases whatever clears the gate.
- **`wombat.brief`** / **`wombat.brief_schedule`** — the once-daily morning brief, registered as a
  pair only when `WOMBAT_BRIEF_PATH` is non-blank. `wombat.brief_schedule` is the durable
  once-per-day timer (survives a crash/sleep and fires a missed brief once on the next boot);
  `wombat.brief` gathers, force-flushes, composes, and delivers it.

## Companion app (Electron)

`app/` is an Electron shell over a settings UI + a runtime chat pane (EP-32). It has its own npm
toolchain, separate from the Python side above.

**Prerequisites:**

- Node (a recent LTS) + npm.
- `uv sync --extra settings-app` — the settings UI's backend (`python -m wombat.settings_app`) is
  a loopback-only FastAPI process; the app can't start without it.
- Optional: `uv sync --extra voice-cloud` to enable cloud STT/TTS provider selection (fish,
  elevenlabs, deepgram) instead of local-only.

**Launch:**

```bash
cd app
npm install
npm start        # builds the renderer, then opens Electron
```

`npm start` spawns `python -m wombat.settings_app` itself — no separate process to start by hand.
It resolves the python interpreter from `WOMBAT_PYTHON` (default `python`); point it at a venv
interpreter (e.g. `.venv/Scripts/python.exe`) if `python` on your `PATH` isn't the one with the
`settings-app`/`voice-cloud` extras installed.

**Chat bring-up** (optional, one line): the app's chat pane talks to a *running* `wombat` runtime,
not the settings API. Set `WOMBAT_CHAT_HANDSHAKE_FILE` in the repo-root `.env`, start the runtime
(`python -m wombat`), then start the app — the chat pane connects once the runtime writes its
handshake file. This is the operator half of the runtime chat surface; the app side needs nothing
else.

**e2e smoke:** `npm run e2e` runs the one Playwright-for-Electron happy-path spec (`app/e2e`) —
drives the real UI, then proves the round trip landed where the runtime reads it (a throwaway cwd
+ keyring service, never the real vault).

## Develop

```bash
uv run ruff check src tests   # lint
uv run mypy                   # type-check (strict, src AND tests)
uv run pytest                 # tests
```

cog-worx is wired as a **local editable directory source** (`[tool.uv.sources]` in `pyproject.toml`),
so edits in the sibling `cog_worx/cog-worx` checkout are picked up live. Swap that for a version or
git pin once cog-worx is published.

## Tests

Most tests are pure/in-memory (no network, no real Postgres). Two gating conventions skip the rest
rather than fail a fresh clone:

- **pg-gated** — tests needing a real Postgres are skipped LOUDLY at collection unless
  `WOMBAT_TEST_PG_DSN` is set. Spin up a throwaway instance:
  `docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16`, then
  `WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres`.
- **live-gated** — tests exercising a real Google Calendar/Gmail network call are skipped unless
  `WOMBAT_TEST_GCAL_LIVE=1` / `WOMBAT_TEST_GMAIL_LIVE=1` is set, which requires the one-time
  interactive OAuth consent step above.

## Tooling

- **codebase-pkg** — queryable Neo4j knowledge graph of this repo, exposed to Claude Code over MCP.
  Bring up the graph with `docker compose -f docker-compose.codebase-pkg.yml up -d` then `codebase-pkg seed`.
- **memory/** — a local, model-light long-term memory for the agent *building* wombat (captures
  what's already been derived and surfaces it again before re-deriving it); see `memory/README.md`.
