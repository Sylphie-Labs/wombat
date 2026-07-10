# Code review — 2026-07-10b (pass 5: persona arc)

> **What this is:** the fifth cross-cutting severity-ranked findings register, covering the
> persona arc (`a8b2dba..HEAD` — EP-33 complete: matrix TK-206, builder TK-207, config
> surface TK-208, LivePersona hot-apply TK-209, gate personality_band TK-215, degrade-path
> deltas TK-216, expression seam TK-219, policy-as-data TK-220, clause calibration TK-221,
> output-EFFECT harness TK-210; plus the app-editable settings source TK-196 and the CR4
> discharge fixes TK-217/TK-218). Written so each finding can be routed into
> `planning/contract.yaml` by the architect-of-record. **This document does not modify the
> contract** — per operating rules, routing (governance entries and/or tickets) happens in
> the next governance step. Findings are numbered `CR5-n` to avoid colliding with the
> earlier `CR-n` (2026-07-06), `CR2-n` (2026-07-09), `CR3-n` (2026-07-09b), and `CR4-n`
> (2026-07-10) registers.
>
> **Method:** targeted sweeps followed by an independent adversarial verification pass;
> every finding below is **CONFIRMED** (executable repro or direct source + governance
> verification — each carries its own verdict evidence). A closing section lists
> plausible-but-unconfirmed items (no CR5 ids) so route-and-fix can consciously skip them.

---

## Critical

None in this pass.

---

## Major

### CR5-1 · settings.json read with locale encoding (cp1252) but written UTF-8 — non-ASCII app-editable values mojibake, and undefined-cp1252 bytes brick boot

- **Where:** `src/wombat/config.py:210` (the `_AppEditableJsonSettingsSource` construction
  with no `json_file_encoding`); UTF-8 writer/reader counterparts at
  `src/wombat/persona/live.py:147` (`_persist`) and `live.py:110` (`poll_settings_file`).
- **Description:** `_AppEditableJsonSettingsSource` is constructed with no
  `json_file_encoding`, and `WombatConfig.model_config` sets only `env_file_encoding`, so
  pydantic-settings' `JsonConfigSettingsSource._read_file` opens `wombat.settings.json`
  with `encoding=None` = the process locale default (cp1252 on a standard Windows host —
  Python 3.13, `locale.getpreferredencoding()` = `'cp1252'` on this machine). But every
  WRITER of the same file uses UTF-8: `LivePersona._persist` writes `encoding="utf-8"`
  (live.py:147) and `LivePersona.poll_settings_file` reads `encoding="utf-8"`
  (live.py:110). So the two code paths that touch this one file disagree on encoding.
  `wombat_assistant_name` and `wombat_tts_voice_id` are free-form APP_EDITABLE fields that
  a settings UI or a hand-edit can legitimately set to non-ASCII values (most editors save
  UTF-8). Two failure modes result: (1) SILENT MOJIBAKE for ordinary accented names — the
  corrupted name is then rendered into every mouth's system instruction and spoken by TTS;
  (2) an UNCAUGHT `UnicodeDecodeError` → BOOT CRASH whenever a value's UTF-8 contains a
  byte undefined in cp1252 (0x81/0x8D/0x8F/0x90/0x9D — e.g. Cyrillic/CJK/many emoji). The
  malformed-file guard at `config.py:78` only catches `json.JSONDecodeError`;
  `UnicodeDecodeError` is a `ValueError`, not a `JSONDecodeError`, so it propagates out of
  `load_config` (which catches only `ValidationError`) and bricks the daemon. This directly
  violates the ticket's explicit guarantee that the app-editable file "never fails boot" —
  and here the file is not even malformed, it is perfectly valid UTF-8 JSON.
- **Failure scenario:** operator (or the future settings UI) sets `wombat_assistant_name`
  in `wombat.settings.json`. Reproduced on this box: file = UTF-8 bytes for
  `{"wombat_assistant_name": "café"}`; `load_config()` returns codepoints
  `[0x63,0x61,0x66,0xc3,0xa9]` (`'cafÃ©'`) instead of `'café'` — silent corruption of the
  steward name in all prompts/TTS. With value `'Ёncins'` (UTF-8 `D0 81`, 0x81 undefined in
  cp1252), `load_config()` raises an UNCAUGHT `UnicodeDecodeError` ("charmap codec can't
  decode byte 0x81") and the daemon fails to boot on a valid JSON file.
- **Verification verdict — CONFIRMED (severity major):** reproduced both failure modes
  against the real `load_config()` on this Windows box (Python 3.13.11,
  `locale.getpreferredencoding` = cp1252). Mechanism verified in actual source:
  pydantic_settings `JsonConfigSettingsSource._read_file` opens with
  `encoding=self.json_file_encoding` (json.py:41); `config.py:210` constructs the source
  with no `json_file_encoding` and `model_config` sets only `env_file_encoding`, so it
  resolves to None → `open(encoding=None)` → cp1252. Meanwhile every `persona/live.py`
  path (110/141/147) uses `encoding='utf-8'`. Repro 1: UTF-8
  `{"wombat_assistant_name":"café"}` → `load_config` returns codepoints
  `[0x63,0x61,0x66,0xc3,0xa9]` (silent mojibake `'cafÃ©'`). Repro 2: UTF-8
  `{"wombat_assistant_name":"Ёncins"}` (byte 0x81 undefined in cp1252) → uncaught
  `UnicodeDecodeError`; confirmed it is NOT a `json.JSONDecodeError` (guard at
  `config.py:78` misses it) and NOT a `ValidationError` (`load_config` handler misses it),
  so it propagates and bricks boot on a valid UTF-8 JSON file — violating the module's
  explicit CON-3 "app file never fails boot" guarantee. Downstream impact confirmed:
  `bootstrap.py:627` threads `config.wombat_assistant_name` into `LivePersona` →
  `instruction_for` → all four mouths' system instructions + TTS, so the
  corrupted/crashing value is on the real product path. Undefined cp1252 bytes are common
  for Cyrillic names (`А` → `D0 90` crash, `я` → `D1 8F` crash), so the boot brick is
  realistic, not exotic. `wombat_assistant_name`/`wombat_tts_voice_id` are the free-form
  APP_EDITABLE fields a settings UI/hand-edit is meant to populate, and editors default to
  UTF-8. wombat's own `_persist` uses `json.dumps` with `ensure_ascii=True` so it never
  emits non-ASCII, but that cannot heal the file because `load_config` crashes at boot
  before any `set()` runs. Windows/locale-specific but the live target is Windows.
  Severity major (not critical: needs operator-supplied non-ASCII input; English-only
  operators unaffected). (Dimension: config-encoding.)
- **Proposed fix direction:** pass `json_file_encoding="utf-8"` to the source (or set it
  in `model_config`) to match the UTF-8 writer, and widen the `_read_file` guard to also
  catch `UnicodeDecodeError`/`OSError` under the same loud-then-treated-as-absent posture.

### CR5-2 · Invalid persona value in wombat.settings.json bricks the whole boot, contradicting TK-196's boot-safety guarantee and the hot-apply path's own tolerance

- **Where:** `src/wombat/config.py:96` (the `__call__` allowlist filter that admits
  APP_EDITABLE values unvalidated; guard scope at `_read_file`, `config.py:75-87`);
  tolerant counterpart at `src/wombat/persona/live.py:120`.
- **Description:** TK-196's `_AppEditableJsonSettingsSource` only shields boot from
  JSON-malformed and non-object files (`_read_file` catches `JSONDecodeError` / non-dict
  and treats them as absent). A syntactically-valid file carrying a value OUTSIDE an
  axis's closed `Literal` vocabulary passes the `APP_EDITABLE_FIELDS` filter
  (`config.py:98`) unvalidated, then fails `WombatConfig`'s `Literal` validation, which
  `load_config` re-raises as `ConfigurationError`. `serve()` calls `load_config()` before
  anything else, so an out-of-vocab persona value in the machine-local, app-written
  settings.json aborts the ENTIRE standing process (drain + brief + dream), not just
  persona. This is the exact asymmetry the persona arc introduced:
  `LivePersona.poll_settings_file` (live.py:120) tolerates the identical bad value
  gracefully (logs one WARNING, keeps the current matrix, never crashes the drain loop per
  CON-3), yet the next restart on the same file will not boot. TK-208 widened this
  boot-fatal surface by adding five more `Literal`-typed fields to `APP_EDITABLE_FIELDS`.
  The module docstring's "so the app file never fails boot" is therefore not upheld for
  the value-invalid case. Verified empirically: settings.json =
  `{"wombat_persona_humor": "playful"}` → `load_config()` raises `ConfigurationError`
  "invalid environment variable WOMBAT_PERSONA_HUMOR; wombat will not start".
- **Failure scenario:** the settings app writes (or an operator hand-edits, or a newer
  binary once wrote a now-unknown axis level and was rolled back) `wombat.settings.json`
  containing e.g. `{"wombat_persona_humor":"playful"}`. The running wombat keeps operating
  (poll tolerates it). On the next restart, `load_config()` raises `ConfigurationError`
  and `serve()` aborts before starting the drain/brief/dream pathways — the whole product
  is down until someone hand-fixes or deletes the machine-local file, even though nothing
  about the drain spine depends on that value.
- **Verification verdict — CONFIRMED (severity major):** empirically reproduced:
  `wombat.settings.json={"wombat_persona_humor":"playful"}` with required env set makes
  `load_config()` raise `ConfigurationError` "invalid environment variable
  WOMBAT_PERSONA_HUMOR; wombat will not start". Verified the full chain in source:
  `_read_file` (config.py:75-87) only guards `JSONDecodeError` + non-dict; `__call__`
  (config.py:97-99) admits any `APP_EDITABLE_FIELDS` key WITHOUT value validation;
  `WombatConfig` fails the `Literal` check (config.py:191); `load_config` re-raises as
  `ConfigurationError` since no `REQUIRED_ENV` matches (config.py:226-238). `serve()`
  calls `load_config()` FIRST (runtime.py:167), before check_config/PG-DSN/assembly, so
  the entire standing process (drain+brief+dream) aborts, not just persona. The asymmetry
  is real: `LivePersona.poll_settings_file` (live.py:100-127) sends the same file through
  `from_strings`, which raises `ValueError` on unknown values (matrix.py:111-114), but the
  bare except catches it, logs one WARNING, and keeps the current matrix — so a running
  wombat tolerates the identical value that will brick the next boot. The module
  docstring's own stated invariant "a malformed file can never fail boot (CON-3)"
  (config.py:63) is not upheld for the value-invalid case; TK-208 widened this surface
  with five more `Literal` fields. Could not refute: no sanitization layer exists between
  the JSON source and `WombatConfig` validation, and `load_config` is unambiguously the
  first boot step. Severity major (not critical): the app's own write paths
  (`LivePersona._persist` via `to_strings`; TK-197 PUT validation) never emit out-of-vocab
  values, so the realistic triggers are external — operator hand-edit or a
  newer-binary-then-rollback version skew. Likelihood is edge, but the blast radius (whole
  product down on any restart until manual file repair) plus the violation of the code's
  own boot-safety intent for this machine-local file justify major over minor.
  (Dimension: config-boot-safety.)
- **Proposed fix direction:** validate app-file values at the source layer — drop any
  APP_EDITABLE value that fails its field's validation with one loud WARNING naming the
  key (the same loud-then-treated-as-absent posture as the malformed-file guard), so a
  bad settings.json value degrades to the field default instead of failing boot; env/.env
  values keep their fail-loud semantics.

---

## Minor

### CR5-3 · poll_settings_file advances the mtime cursor before the read/parse/apply succeeds, so a mid-write partial read can permanently drop that edit generation

- **Where:** `src/wombat/persona/live.py:107` (cursor advance before the read/parse/apply
  at lines 110-120); correct counterpart in `set()` at `live.py:98`.
- **Description:** `poll_settings_file` sets `self._last_mtime = mtime` at line 107
  BEFORE the `read_text`/`json.loads`/`from_strings` at lines 110-120. If any of those
  steps fails (a transient read failure on a OneDrive cloud-placeholder file, or a
  mid-write partial read of a non-atomically-written settings.json that yields invalid
  JSON), the except at 121 keeps the current matrix but the cursor has already advanced to
  the failed generation's mtime. The next poll compares against that advanced cursor and
  early-returns (line 105), so it never retries — the edit only hot-applies if the file's
  mtime later changes AGAIN to a strictly different value. Contrast `set()` (live.py:98),
  which correctly defers `self._last_mtime = self._current_mtime()` until AFTER a
  successful persist. The loss window is the app's completing write landing on the same
  stat-observed mtime as the consumed mid-write read (filesystem granularity collision) —
  narrow on NTFS, more plausible on a OneDrive-synced/network path like this repo's own
  location. Fail-safe (no crash, no wrong value applied), but a real hot-apply reliability
  gap.
- **Failure scenario:** the Electron settings app writes `wombat.settings.json`
  non-atomically (or the file is a OneDrive cloud placeholder). wombat's 5s Sweeper-beat
  poll stats the file mid-write, gets the new mtime, reads partial/undownloaded bytes →
  `JSONDecodeError` → caught, warning logged, `_last_mtime` already advanced. The app's
  write completes but its final mtime equals the mid-write stat value
  (coarse-granularity / same-tick), so subsequent polls see no change and the user's
  persona edit never hot-applies until they make a further, distinct edit.
- **Verification verdict — CONFIRMED (severity minor):** code-fact confirmed at
  `src/wombat/persona/live.py`: line 107 sets `self._last_mtime = mtime` BEFORE the
  `read_text`/`json.loads`/`from_strings` at lines 110-120. On any failure there, the
  except (121) keeps the matrix but the cursor is already advanced; the next poll's
  early-return (line 105, `mtime == self._last_mtime`) never retries that generation, so
  the edit only ever hot-applies on a LATER strictly-different mtime. This is the exact
  inverse of `set()` (line 98), which correctly defers the cursor advance into the else
  branch after a successful persist — confirming the asymmetry is a bug, not intentional.
  A deterministic repro (FAKE stat + one transient read failure on a settled file with
  stable mtime) reproduces permanent loss: poll#1 advances cursor 100→200 with matrix
  unchanged; poll#2 sees mtime==cursor and early-returns without re-reading; the user's
  edit content sitting on disk never applies. The finding's own scenario (mid-write
  partial read + granularity collision) is one trigger, but a stronger/simpler one exists:
  a single transient read/parse failure on an already-settled file (OneDrive
  cloud-placeholder pre-hydration, or a momentary sharing-violation while the Electron app
  holds the handle) guarantees the collision because the mtime is stable and won't change
  again until a further distinct edit. Fail-safe (never raises, never applies a wrong
  value), edge-triggered, and the repo does live on a OneDrive path — so minor is the
  honest severity: a real hot-apply reliability gap, not a correctness/safety break of the
  running product. (Dimension: hot-apply-poll.)
- **Proposed fix direction:** advance `self._last_mtime` only AFTER the
  read/parse/apply succeeds (mirror `set()`'s defer-into-else shape), so a failed
  generation is retried on the next Sweeper beat.

---

## Plausible-but-unconfirmed (no CR5 ids — route-and-fix may consciously skip)

None in this pass.

---

## Suggested routing (for the architect — next governance step)

| Finding | Proposed home |
|---|---|
| CR5-1 | P2 fix ticket (pair with CR5-2 — one settings-file robustness ticket on `config.py`) — pin `json_file_encoding="utf-8"` on the app-editable source + widen the `_read_file` guard to `UnicodeDecodeError`/`OSError` |
| CR5-2 | Same P2 ticket as CR5-1 — per-value validate-or-drop-with-WARNING at the source layer so a bad app-file value degrades to the default instead of failing boot |
| CR5-3 | Small P3 fix ticket — defer the mtime-cursor advance in `poll_settings_file` until after a successful apply |
