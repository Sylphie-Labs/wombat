# Project memory

<!-- BEGIN planning-worx -->
@planning/planning-worx.rules.md
<!-- END planning-worx -->

# The project at a glance

wombat ("The Steward") is a quiet personal-assistant agent built on the
**cog-worx** cognitive-architecture framework (resolved editable from
`../cog_worx/cog-worx`). It is a queue-driven **deterministic** loop, not an
agent loop: sources enqueue into a durable Postgres queue, a model-free Gate
(`wombat.gate.pipeline`) decides what surfaces, and **the LLM is a mouth** —
it phrases pre-decided output, it never decides whether or when to act.

- `src/` — the `wombat` package (Python 3.13+, mypy strict over src *and* tests).
- `tests/` — pytest (asyncio auto mode).
- `app/` — Electron companion app (settings UI + chat pane); own npm toolchain,
  backed by `python -m wombat.settings_app` (loopback-only FastAPI).
- `planning/` — the contract, vision, and verification docs.
- `memory/` — the wombat-memory recall pipeline (Node + Postgres + Ollama),
  wired in via hooks; not part of the product.
- `cog-worx-bugs/` — ticket intake for the cog-worx maintainer agent.

Commands (README.md has the full detail — trust it over memory):

```bash
uv sync                      # install; optional extras: voice, voice-cloud, settings-app, browser
python -m wombat             # boot the ONE standing process (wombat.runtime.serve())
uv run pytest                # tests
uv run ruff check .          # lint
uv run mypy                  # strict, covers src + tests
cd app && npm install && npm start   # Electron app (spawns settings_app itself)
```

Boot facts that bite: `DEEPSEEK_API_KEY`/`DEEPSEEK_BASE_URL` are required env;
`WOMBAT_PG_DSN` is required by `serve()` but deliberately not by tests/demo;
Google, brief, and voice pathways all degrade loudly-but-safely when unset.
Optional extras are lazy-imported behind seams — a base checkout must always
boot clean.

# How we work — execution model

These rules govern how work is actually executed in this repo. They complement
the planning-worx rules above: the contract still defines *what* to build and
*when it's done*; this section defines *how it gets built*.

## Orchestrator, not implementer

- The main session (Fable) **orchestrates and decides**. It does not implement
  inline. All build work runs through the **Workflow tool** or spawned agents.
- Never end a turn asking "shall I continue?" — keep pulling the next ready
  work until the batch is done or genuinely blocked on Jim.

## Parallel by default

- Do **not** work one ticket at a time. Batch every *ready, independent* ticket
  into a single workflow and run them **in parallel** (pipeline/fan-out).
  Serialize only where tickets genuinely depend on each other.
- Each ticket in a batch still gets its own lean briefing (`briefing-builder`)
  and still passes its runnable acceptance checks. Parallelism never relaxes
  the done-bar.
- Agents mutating files in parallel use worktree isolation to avoid conflicts.

## Done means it ran live

- **A feature is not done because its code exists and its tests pass. It is
  done when the real code path has executed in the real running app.** Three
  consecutive verification sweeps found "done" features that had never once
  run in production: the reflection that was never composed (ISS-64), the
  browser capability that was never wired (ISS-66), ratings computed against a
  test double instead of the live user model (ISS-63). The board counts code;
  it must count liveness.
- Verification of a "done" ticket therefore asks first: *has this path ever
  actually executed?* Prove it from live state — running processes, real
  endpoints, the DB, logs — not from the source alone. Live-drive the app
  (computer-control MCP) when that's what proof requires.
- Wiring is part of the ticket. A component built but never registered,
  composed, or reachable from the running loop does not satisfy any
  acceptance criterion.

## Agents and model tiers

- Use the agent definitions in `.claude/agents/` where one fits — they carry
  their own model assignments. When a recurring role has no definition, create
  one rather than re-prompting the same role ad hoc.
- When choosing a model for an agent or workflow stage, follow the tiered
  approach:
  - **Haiku** — reading, comparing, scanning, mechanical sweeps.
  - **Sonnet** — reading + implementing (the build tier).
  - **Opus** — reasoning: verification, judging, review, architecture.

## The endorsed build cadence

- The proven loop lives in the session memory dir
  (`~/.claude/projects/C--Users-Jim-OneDrive-desktop-Code-wombat/memory/`):
  **`wombat-build-batch.workflow.js`**, invoked with args
  `{today, state_summary, steer}`. Variants alongside it:
  `wombat-build-batch-parallel.workflow.js` and
  `wombat-batch-resume.workflow.js`.
- Its shape: Sequence → Build (per ticket: Sonnet builds, architect records +
  commits) → Batch verify (one Opus reviewer over the whole batch diff, ≤2
  Sonnet repair rounds with architect fix records).
- Run a review pass after each arc, and complete the whole arc before
  reporting back. Don't reinvent this loop — extend the script if the cadence
  needs to change.

## Ambiguity routing

- Agents **never self-resolve ambiguity**. Anything unclear is flagged back to
  the orchestrator, which routes it:
  - Code / design / architecture / scope → the **architect** agent (it decides
    within the locked frame and records the outcome).
  - Feature intent → `planning/vision.md`.
  - Contract questions → answer from `planning/contract.yaml` and the docs.
  - Only a **true conflict** the docs cannot settle goes to Jim.
- Every resolved ambiguity gets a home in `governance` (decision, deferral, or
  non-goal) — the conversation is never the record.

## Removal discipline

- No dangerous or bulk removals by any agent. Deletions land as small, named,
  reviewable increments (one-line edits; file deletions via explicit `git rm`
  with a stated reason).

# Working with Jim

- **His bug reports are pre-vetted.** By the time Jim reports a bug he has
  already done the obvious fixes. Diagnose from live state (processes,
  endpoints, the DB, logs) — never hand him a how-to checklist.
- **The designer never implements.** The ux-designer agent produces mocks
  only, and stops for Jim's approval. All code — including UI — lands through
  the build workflow after the architect mints tickets.
- **Editing `planning/contract.yaml`** without tripping the append-guard hook:
  every edit must also append a changelog entry, and superseding a decision
  requires the new entry to reference the old DEC id. `decisions` and
  `changelog` are append-only; never touch past entries.
