# Code review — 2026-07-09b (pass 3: reflection arc + voice foundation surface)

> **What this is:** the third cross-cutting severity-ranked findings register, covering the
> reflection-arc nightly/render surface (EP-21..EP-24), the voice-foundation config/vault
> surface (EP-31 head), and the live production bring-up of 2026-07-09 evening. Written so
> each finding can be routed into `planning/contract.yaml` by the architect-of-record.
> **This document does not modify the contract** — per operating rules, routing (governance
> entries and/or tickets) happens in the next governance step. Findings are numbered
> `CR3-n` to avoid colliding with the earlier `CR-n` (2026-07-06) and `CR2-n`
> (2026-07-09) registers.
>
> **Method:** targeted sweeps followed by an independent adversarial verification pass;
> every finding below is **CONFIRMED** (live repro, executable repro, or direct source
> verification — each carries its own verdict evidence). One finding (CR3-1) is a live
> production incident from tonight's first standing-process boot. A closing section lists
> plausible-but-unconfirmed items (no CR3 ids) so route-and-fix can consciously skip them.

---

## Critical

### CR3-1 · Fresh-database first boot crashes: no composition path ever runs `ensure_schema`

- **Where:** `src/wombat/bootstrap.py:610`.
- **Description:** `assemble_runtime` eagerly replays the pending journal
  (`PendingSet.rebuild_from_journal`) but neither `serve()` nor `assemble_runtime` runs
  the packaged `ensure_schema` migrations, so wombat's FIRST boot against a brand-new
  Postgres dies with `psycopg.errors.UndefinedTable: relation 'pending_journal' does not
  exist`. Every pg-touching module (`wombat_queue`, `daily_ledger`, `pending_journal`,
  `action_trail_projection`, `wombat_behavior_events`) assumes the schema already exists.
- **Failure scenario:** LIVE REPRO 2026-07-09 ~19:05 — the first-ever production boot
  against a fresh docker volume `wombat-runtime-pg-data` crashed at `bootstrap.py:610`.
  Workaround applied by an operator-side script (manual `ensure_schema` x5) before
  restart.
- **Verification verdict — CONFIRMED:** reproduced live in production bring-up tonight;
  traceback on record. (Dimension: cross-cutting, orchestrator-seeded.)
- **Proposed fix direction:** run every adapter's packaged `ensure_schema` idempotently
  as the first step of `assemble_runtime` (or a `serve()` pre-flight), before any
  journal replay or adapter read touches the database.

---

## Major

### CR3-2 · `dream_pattern` enqueue catches only `QueueFullError`, so any other pg error crashes the whole nightly dream run (never-block-the-terminal parity break)

- **Where:** `src/wombat/behavior/stages/pattern_detector.py:184` (enqueue guard,
  lines 184-193); raw pg body at `src/wombat/queue.py:112-137`; retry default at
  `src/cogworx/loop/retry.py:67-72`.
- **Description:** `PatternDetectorStage.run()` wraps its read/parse in a broad
  `except Exception` (metrics=None, still transitions), but the enqueue write is guarded
  by `except QueueFullError` ONLY. `self._enqueue` is the injected `WombatQueue.enqueue`
  bind (wired at `bootstrap.py:892`), whose body does raw psycopg
  `cur.execute`/`conn.commit`/`conn.rollback` on a single long-held connection and can
  raise psycopg errors other than `QueueFullError` — e.g. `psycopg.OperationalError`
  when the shared queue connection, idle since the last daytime drain, has been reaped
  by the server, or `psycopg.errors.InFailedSqlTransaction` if the connection is left in
  an aborted-transaction state. Such an exception propagates out of `run()`. The stage
  carries no `retry_policy`, so the engine applies `DEFAULT_RETRY_POLICY` with
  `retryable=()`; the engine's `_drive` treats a non-retryable raise as a fail-loud BUG
  that propagates uncaught → RUN_CRASHED, and the `dream_run` terminal is never reached.
  This is exactly the never-block-the-terminal invariant every sibling dream stage
  upholds against ANY exception: `DreamTuneStage` (`dream_pathway.py:470`),
  `WriteWindowSummariesStage` (`write_window_summaries.py:101`), and
  `DreamBehaviorLogStage` (`dream_pathway.py:581`) all use broad `except Exception`
  around their pg/collaborator work and always return a Transition. PatternDetectorStage
  is the single hole. The test suite hides it:
  `tests/behavior/stages/test_pattern_detector.py` exercises only `QueueFullError`
  (test at line 332) as an enqueue failure, so pytest is green while a generic pg
  failure on enqueue is unhandled.
- **Failure scenario:** nightly dream run; a KB pattern matched so `pattern_id` is set
  and enqueue is called. The shared `WombatQueue` psycopg connection was closed by
  Postgres during the idle stretch since the last daytime gate drain.
  `self._enqueue(item)` raises `psycopg.OperationalError` (not `QueueFullError`) →
  propagates out of `PatternDetectorStage.run()` → engine classifies it non-retryable →
  the entire `wombat.dream` run crashes (RUN_CRASHED), the `dream_run` terminal never
  reached. The identical connection blip inside `dream_window`/`dream_behavior_log`/
  `dream_tune` would have been absorbed and the run would have completed.
- **Verification verdict — CONFIRMED (severity major):** confirmed by reproduction and
  source verification. (1) `bootstrap.py:892` injects `enqueue=queue.enqueue`, the raw
  bind. (2) `queue.py:101-137` raises `QueueFullError` only on the capacity path; a
  reaped/dead connection makes `cur.execute` raise `psycopg.OperationalError`, unrelated
  to `QueueFullError`. (3) `pattern_detector.py:184-193` guards the write path narrowly
  while the read path (line 154) is broad. (4) No `retry_policy` → `DEFAULT_RETRY_POLICY`
  (`retryable=()`); `engine.py` `_drive` only catches `retryable=(TimeoutError,)` at
  line 787, any other raise → RUN_CRASHED (`engine.py:363`). (5) Parity confirmed
  against `WriteWindowSummariesStage.run` (`write_window_summaries.py:86-107`). A repro
  using the test module's own fixtures proved the divergence: a generic/
  OperationalError-style enqueue exception makes `run()` raise out, whereas
  `QueueFullError` is absorbed into `Transition(to='dream_run', errors=1)`. Major, not
  critical: the crash requires the conjunction of a KB pattern match AND a connection
  fault exactly at enqueue, and the crashed dream run is re-drivable (the enqueue is
  date-keyed idempotent) so damage is bounded/recoverable — but it is a real
  never-block-the-terminal parity break a night can hit, since `dream_pattern` is the
  last stage before the terminal.
- **Proposed fix direction:** widen the enqueue guard to broad `except Exception`
  (log loud, count, still transition) to match every sibling dream stage, and add a
  non-`QueueFullError` enqueue-failure test.

---

## Minor

### CR3-3 · `load_psychology_kb` raises bare `ValueError`/`TypeError` on non-numeric threshold or version, escaping the boot KB-degrade path

- **Where:** `src/wombat/kb/loader.py:131` (`float(condition_raw["threshold"])`) and
  `:136` (`int(file_version)`); boot catch sites `src/wombat/bootstrap.py:731` and
  `:879`.
- **Description:** the loader's module docstring promises "Any violation raises
  ValidationError" and both bootstrap call sites catch ONLY
  `(FileNotFoundError, KBValidationError)` so that a KB load failure never fails the
  whole boot. But the two final coercions are unguarded: a `gate_condition.threshold`
  or top-level `version` that is present-but-non-numeric (e.g. a YAML typo
  `threshold: 0.6.`, `threshold: null`, `version: v1`, `version: [1,2]`) raises a bare
  `ValueError`/`TypeError`, NOT `ValidationError`. That exception type is outside both
  boot except clauses, so instead of degrading to an empty KB (the intended no-nudge /
  safe-default-prompt behavior for both `PatternDetectorStage` and
  `ReflectionComposeStage`) it propagates out of `assemble_runtime` and crashes the
  entire wombat process boot — a KB-content problem takes down the standing product,
  exactly what the try/except was written to prevent. Every other malformed-KB case
  (missing field, bad vocabulary, unparseable YAML, non-mapping top level, empty
  entries) is correctly wrapped in `ValidationError`; only the numeric fields have this
  gap.
- **Failure scenario:** a maintainer edits `src/wombat/kb/psychology_kb.yaml` and
  fat-fingers a threshold as `threshold: 0.6.` (trailing dot) or a null. At next boot,
  `assemble_runtime` calls `load_psychology_kb()`, reaches `float("0.6.")` at
  `loader.py:131` and raises `ValueError`. Neither boot except block catches it, so the
  whole wombat process fails to start — instead of the documented degrade to an empty
  KB (reflections disabled, drain/brief unaffected).
- **Verification verdict — CONFIRMED (severity minor):** confirmed by live repro.
  `schema.ValidationError` subclasses `ValueError`; the escaping exceptions are the
  PARENT of the caught subclass, hence uncaught. Repro output: `threshold='0.6.'` →
  `ValueError` ESCAPES; `threshold=null` → `TypeError` ESCAPES; `version='v1'` →
  `ValueError` ESCAPES; `version=[1,2]` → `TypeError` ESCAPES. Severity minor is
  honest: the packaged seed YAML is valid and
  `test_ac1_loads_the_real_packaged_kb_as_typed_entries` would fail on such an edit
  (CI backstop), so it's only reachable via a human hand-edit numeric typo that slips
  past CI — but that is precisely the KB's stated maintenance model ("Change a value →
  BUMP version"), making the loader/contract mismatch a genuine edge defect.
- **Proposed fix direction:** wrap the `float(...)`/`int(...)` coercions in try/except
  and re-raise as `ValidationError` naming the entry/field, matching the loader's
  stated contract.

### CR3-4 · Config unit test's `monkeypatch.delenv` is ineffective against the `.env` file source, leaving the suite red on any machine with a populated `.env`

- **Where:** `tests/unit/test_bootstrap.py:93`
  (`test_wombat_config_boots_without_brief_path_or_voice_env`); root cause interaction
  with `src/wombat/config.py:29` (`model_config` `env_file=".env"`).
- **Description:** the test clears `WOMBAT_BRIEF_PATH` via `monkeypatch.delenv` and then
  asserts `config.wombat_brief_path is None`. But `WombatConfig` has
  `env_file='.env'`, so pydantic-settings reads the repo-root `.env` file directly (not
  through `os.environ`). The delenv cannot suppress a value that lives in the `.env`
  file, so on any machine whose `.env` sets `WOMBAT_BRIEF_PATH` (the live wombat host
  does: `WOMBAT_BRIEF_PATH=C:\Users\Jim\wombat-data\brief.md`) the field is populated
  and the assertion fails. The TK-186 batch demonstrably knew this hazard — it added
  `monkeypatch.chdir(tmp_path)` to the two sibling AC2 tests
  (`test_ac2_missing_api_key`/`base_url`, with an explicit comment at lines 53-55 about
  pydantic-settings resolving `env_file` relative to CWD) and to every new
  `tests/unit/test_config.py` case — but left this pre-existing TK-101 test unguarded
  (and it now also pulls the real `WOMBAT_FISH_API_KEY` secret into the config under
  test). The test is therefore red under the stated 'pytest green' quality bar on the
  production machine, and its delenv-based guard gives false confidence: the "boots
  without brief path" precondition is never actually exercised where a `.env` exists
  (it only passes by accident in CI where no `.env` is present). Product code
  (`config.py`) is correct; this is purely a test-isolation defect.
- **Failure scenario:** on a host with a populated `.env` (the live wombat machine):
  run `.venv/Scripts/python -m pytest tests/unit/test_bootstrap.py` from the repo root
  → `test_wombat_config_boots_without_brief_path_or_voice_env` FAILS with
  `assert 'C:\\Users\\Jim\\wombat-data\\brief.md' is None`. Verified directly: after
  `os.environ.pop('WOMBAT_BRIEF_PATH')`, `WombatConfig().wombat_brief_path` still
  returns the real `.env` value.
- **Verification verdict — CONFIRMED (severity minor):** reproduced directly on the
  live machine (`.env` contains `WOMBAT_BRIEF_PATH`, `WOMBAT_VOICE_ENABLED=true`,
  `WOMBAT_FISH_API_KEY`); the single-test pytest run fails as described. No impact on
  the running product, only the test suite's green state. Overlaps the Q-103/TK-202
  hermeticity theme (env-leak into non-hermetic env-clearing tests).
- **Proposed fix direction:** mirror the sibling tests — add
  `monkeypatch.chdir(tmp_path)` (taking `tmp_path`) so no `.env` is on the resolution
  path; natural home is TK-202's hermeticity hardening.

---

## Plausible-but-unconfirmed (no CR3 ids — route-and-fix may consciously skip)

None in this pass.

---

## Suggested routing (for the architect — next governance step)

| Finding | Proposed home |
|---|---|
| CR3-1 | New P1 ticket — idempotent `ensure_schema` pre-flight in `assemble_runtime`/`serve()`; live incident already worked around operator-side, structural fix owed |
| CR3-2 | New P2 fix ticket — broaden `dream_pattern` enqueue guard to sibling-stage parity + non-QueueFullError enqueue-failure test |
| CR3-3 | P3 fix ticket (or batch into KB hygiene) — wrap numeric coercions in `ValidationError` per the loader's stated contract |
| CR3-4 | Fold into TK-202 (suite hermeticity) — `monkeypatch.chdir(tmp_path)` on the TK-101 boot test |
