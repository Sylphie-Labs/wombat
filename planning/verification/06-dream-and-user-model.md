# 06 — The nightly dream and the compounding user model (TK-368, SWEEP 06)

Run date: 2026-08-05, per `protocol.md`, one sweep at a time per DEC-86.

**Headline: the compounding claim cannot be demonstrated on this build, and the
reason is structural rather than a bug in the dream.** The rating half of the
user model is wired to an in-memory test double in production, so every
parameter the nightly tuner writes is destroyed when the process exits. Detail
in AC2/AC3 and routed as **ISS-63**.

| AC | Check | Class | Result |
|---|---|---|---|
| AC1 | dream completes off-path, facts listed with provenance, **no motive stored** | A | **PASS** on the CON-6/NG-1 half |
| AC2 | RatingTuner changes shown before/after, each inside its band | A | **BLOCKED** by ISS-63 |
| AC3 | re-rated item moved the direction outcomes argued — the compounding claim | A | **BLOCKED by construction** — ISS-63 |
| AC4 | no model call anywhere on the gate/decision path | A | **PASS** |
| AC5 | retention prunes outside the window, keeps inside | A | **BLOCKED** — nothing is old enough to prune |

---

## AC1 — no motive, cause or "why" is ever stored — **PASS**

The constitution's sharpest rule (CON-6/NG-1), checked the way the AC demands:
by reading **what was actually stored**, all of it, not by sampling.

**Artifact:** all 21 rows of the live `wombat_user_facts`, read verbatim —
9 `behavior`, 7 `dream`, 5 `derived`.

Every row is observational. Representative:

- `The user typically spends mornings working in a terminal.`
- `The user often watches anime on Crunchyroll via Chrome.`
- `The user called someone Mao Mao.`

**Not one row contains a motive, a cause, a preference-because, or a "why"
claim.** No row says the user *wants*, *prefers*, *is trying to*, or does
anything *because*. The wall holds where it matters most.

**But the content is wrong in ways that matter**, and three findings were routed
from this same read (full detail in the contract, summarised here):

- **ISS-60 (MAJOR)** — the model accumulates and never reconciles. `The user is
  called John.` and `The user's name is Jim.` are **both live**. The PK is a
  sha256 of the fact *text*, so any rewording is a new row by construction and
  semantic duplicates can never collide.
- **ISS-61 (MAJOR)** — all five `derived` facts count no-reply bulk marketing
  senders as correspondents (`no_reply@email.apple.com`, LinkedIn
  `newsletters-noreply`, `notification.capitalone.com`, …).
- **ISS-62 (MINOR)** — markdown list markers leak into stored fact text.

CON-6 is about *not inferring motive*. These findings are about *facts being
wrong*, which is a different failure and is not covered by that rule.

## AC4 — the model never touches a decision path — **PASS**

The DEC-23 boundary, and the exact line NG-4 draws.

**Artifact (structural):** `.complete(`, `ctx.model`, `ModelSpec`,
`chat_completion` and `deepseek` across `src/wombat/gate/` **and**
`src/wombat/stages/gate_stage.py` → **zero hits.** The gate cannot reach a model
because it holds no reference to one.

**Artifact (live):** `logs/runtime-20260805-134028.log:11-18` — eight consecutive
gate decisions (`gate: load flush denied: already flushed today`, then seven
`gate decision: … action='hold'`) with **no** model HTTP call anywhere among
them. The only `httpx` line in that boot is the faster-whisper model-card fetch
at startup.

The gate is deterministic and model-free, live.

## AC2 / AC3 — the tuner and the compounding claim — **BLOCKED (ISS-63)**

Both fail for one shared reason, found while looking for where tuned parameters
persist so a before/after could be captured.

**`src/wombat/bootstrap.py:967`:**

```python
entity_kg = InMemoryEntityKG()
```

That is `cogworx.testing.doubles.InMemoryEntityKG` — the **test double** —
constructed unconditionally in the production composition root and threaded into
both `UserModel` (the read seam the gate scores through) and `RatingTuner` (the
nightly write seam). `bootstrap.py:789` states the consequence plainly:

> `V1 honesty (Q-36/TK-14): in-memory, resets per process.`

**What that means concretely.** `RatingParams` are stored as claims in
`entity_kg`. The nightly `RatingTuner` writes there. `UserModel.ratings_for`
reads there, and on a miss falls back silently to `default_params_for` (the "no
node" case logs nothing). So on **every** process start the rating model is
back to defaults, and last night's tuning is gone.

**This host restarted the runtime 10 times on 2026-08-05 alone** (per TK-367's
boot census). Tuning cannot survive a day here, let alone compound over weeks.

- **AC2 is BLOCKED**: a before/after across a night is not meaningful when the
  store is reconstructed empty at each boot. The bands themselves *are* pinned
  and verifiable — `RatingTunerBounds` (`params.py:55-71`), LOCKED at
  `clamp_floor 0.35`, `clamp_ceiling 0.65`, `delta_bound 0.05`, `gain 0.20`,
  `surfacing_ceiling_per_day 12.0` — and the tuner clamps against them. What
  cannot be shown is a *real* tuned delta on real accumulated outcomes.
- **AC3 is BLOCKED by construction**: "understanding compounds instead of
  resetting" is precisely what this wiring prevents for the rating half. It is
  not that the check failed; it is that the check cannot be run.

**The citation is backwards, which is why this hid.** The comment credits Q-36.
Q-36's actual resolution text lists the **"user-model real adapter"** among the
stores that *"DO require Postgres/Neo4j for their durability semantics —
correct + intended."* Q-36 says the real adapter is expected; the code cites
Q-36 while wiring the double. A real `neo4j_entity_kg.py` adapter **exists in
cog-worx** and is simply not wired, and **no DEF-\* covers the swap** — so the
gap has no governance home.

**Important scope limit, so this is not overstated.** The user model has two
halves with *different* durability, and only one is affected:

| half | store | durable? |
|---|---|---|
| **facts** (`wombat_user_facts`) | Postgres | **yes** — 21 rows accumulating since 2026-07-31 |
| **ratings** (`RatingParams` via `entity_kg`) | `InMemoryEntityKG` | **no** — resets per process |

So wombat *does* remember things about Jim across restarts. What it does not
remember is anything it learned about **how to rate and when to interrupt** —
which is the half the gate actually consults.

Routed as **ISS-63**.

## AC5 — retention prune — **BLOCKED (nothing is prunable yet)**

Retention is `_OBSERVATION_RETENTION_DAYS = 21`, a pinned module constant
(`observations.py:58`), pruned once at boot by `runtime.serve()`.

**Artifact:** `SELECT min(started_at)::date, max(started_at)::date, count(*)
FROM wombat_observations` → oldest **2026-08-02**, newest **2026-08-05**, **540
rows**, oldest age **3 days**.

Nothing in the table is outside a 21-day window, so the "rows outside the window
are gone" half has nothing to act on. Only "rows inside survive" is observable,
and it did — 540 rows present after the 13:46 boot's prune ran.

**What unblocks it:** the observation store reaching 21+ days of depth, i.e. on
or after **2026-08-23** (collection began 2026-08-02). Same structural shape as
TK-367's AC3 — a check that needs the world to be in a state this host has not
reached yet, not a check anyone can force.

---

## Findings routed

- **ISS-63 (MAJOR/CRITICAL for the product thesis, new)** — the rating half of
  the user model runs on an in-memory test double in production; tuned
  parameters do not survive a process exit, so the compounding claim is
  structurally unachievable. Real adapter exists, is unwired, and no deferral
  covers it. The `Q-36` citation in `bootstrap.py:789` misreads that question.
- **ISS-60, ISS-61, ISS-62** — routed earlier in this sweep from AC1's read.

## State left behind

Read-only sweep. No runtime was started or stopped for AC2–AC5, no database
written, no source or test file edited, no dream fired. The runtime left up by
TK-367 (`logs/runtime-20260805-134601.log`) was untouched.

## What this sweep does NOT claim

Per DEC-85(c) the sweep ran and its findings were routed. It does **not** assert
FEAT-3/FEAT-4 have PASSED — two ACs are blocked by ISS-63, one by data depth,
and the two that passed are narrow. TK-377 alone adjudicates.
