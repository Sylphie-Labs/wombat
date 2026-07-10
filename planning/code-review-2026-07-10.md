# Code review — 2026-07-10 (pass 4: EP-31 voice provider arc)

> **What this is:** the fourth cross-cutting severity-ranked findings register, covering the
> EP-31 voice provider arc (`a82c8ac..HEAD` — cloud STT/TTS providers TK-189/TK-190/TK-191/
> TK-192, provider selection + fallback TK-193, assistant-name threading TK-194, and the
> TK-195 egress lesion proof). Written so each finding can be routed into
> `planning/contract.yaml` by the architect-of-record. **This document does not modify the
> contract** — per operating rules, routing (governance entries and/or tickets) happens in
> the next governance step. Findings are numbered `CR4-n` to avoid colliding with the
> earlier `CR-n` (2026-07-06), `CR2-n` (2026-07-09), and `CR3-n` (2026-07-09b) registers.
>
> **Method:** targeted sweeps followed by an independent adversarial verification pass;
> every finding below is **CONFIRMED** (executable repro or direct source + governance
> verification — each carries its own verdict evidence). A closing section lists
> plausible-but-unconfirmed items (no CR4 ids) so route-and-fix can consciously skip them.

---

## Critical

None in this pass.

---

## Major

None in this pass.

---

## Minor

### CR4-1 · Healthy cloud TTS boot emits a false "voice output disabled for this boot" warning

- **Where:** `src/wombat/voice/select.py:137` (the `_build_local_tts` warning, lines
  130–143); eager call site `select.py:249` in `build_tts_adapter`.
- **Description:** `build_tts_adapter` always constructs the local fallback eagerly on the
  healthy-cloud path: `return FallbackTTSAdapter(primary, fallback=_build_local_tts(config))`.
  When the operator has the `voice-cloud` extra (httpx) but NOT the local `voice` extra
  (pyttsx3), and selects a working cloud TTS provider with a resolving key + voice_id, the
  cloud primary constructs fine, but `_build_local_tts` then fails constructing
  `Pyttsx3Adapter` and logs WARNING: "voice: TTS adapter failed to construct ... voice
  output disabled for this boot". That statement is factually wrong here — cloud TTS is the
  working primary and voice output is NOT disabled. The message is correct only when the
  local adapter is the sole/primary adapter (the local-provider or fallback-to-local path),
  not when it is merely the failed fallback slot behind a live cloud primary. An operator
  reading boot logs is misled into thinking voice is off (or that cloud is broken).
- **Failure scenario:** config `wombat_voice_enabled=True`, `wombat_tts_provider="fish"`,
  valid `WOMBAT_FISH_API_KEY` + `WOMBAT_TTS_VOICE_ID`; host has the `voice-cloud` extra but
  not the `voice` extra (no pyttsx3). `build_tts_adapter` constructs the Fish cloud primary
  successfully, then `_build_local_tts` raises and logs "voice output disabled for this
  boot" even though cloud voice is fully functional — a false operator signal at every boot.
- **Verification verdict — CONFIRMED (severity minor):** reproduced with a fake key store,
  fake cloud TTS, and a failing `Pyttsx3Adapter` (voice extra absent). `build_tts_adapter`
  (`select.py:249`) eagerly calls `_build_local_tts(config)` on the healthy-cloud path;
  when `Pyttsx3Adapter()` raises (no pyttsx3), `_build_local_tts` catches Exception, logs
  the WARNING, and returns None. The function still returns a working `FallbackTTSAdapter`
  with the live cloud primary and `fallback=None`, so voice output is NOT disabled — the
  log is factually wrong on this path. Repro output confirmed: RETURNED
  `FallbackTTSAdapter`, primary works, fallback None, and the "voice output disabled for
  this boot" warning was emitted. The scenario is reachable: `voice` and `voice-cloud` are
  separate pyproject extras, so a cloud-only install is legitimate; and there is no
  positive "cloud TTS selected" log to counterbalance the misleading message. Genuine,
  reproducible defect but purely a misleading operator/log signal — cloud voice output
  actually works — so severity is minor. Not a style nit: it is a false degrade-path
  signal an operator would reasonably read as "voice is off". (Dimension: select-fallback.)
- **Proposed fix direction:** on the healthy-cloud path, contextualize the failed-fallback
  log (e.g. "local TTS fallback unavailable — cloud primary active") and reserve the
  "voice output disabled for this boot" message for the path where the local adapter is
  the sole/primary adapter.

### CR4-2 · `tts_adapter.py` docstring still asserts "the ONE concrete adapter ... no cloud TTS" — a stale absolute DEC-28 explicitly required the provider tickets to amend

- **Where:** `src/wombat/sinks/tts_adapter.py:7`.
- **Description:** DEC-28 (accepted) states verbatim that it "supersedes-in-part Q-96
  (pyttsx3 as THE one concrete adapter; no cloud TTS)" and that "the asr.py/tts_adapter.py
  module docstrings are amended by the provider tickets accordingly." TK-191 (955febc) and
  TK-192 (4accf1d) landed three cloud `TTSAdapter` implementations in
  `src/wombat/voice/tts.py` (`FishAudioTTSAdapter`, `ElevenLabsTTSAdapter`,
  `DeepgramAuraTTSAdapter`), all behind the same `TTSAdapter` protocol. But git log
  confirms `tts_adapter.py` was never touched after TK-164 (6d2014e), so its docstring
  still reads: "Pyttsx3Adapter is the ONE concrete adapter (Q-96 ruling): pyttsx3 (offline,
  Windows SAPI5 backend — CST-2/TECH-11 local-only speech, no cloud TTS)." That absolute
  is now factually false at product scope and directly contradicts the DEC-28 rescoping
  obligation.
- **Failure scenario:** EP-31 has shipped cloud TTS providers; DEC-28 is accepted and
  instructs the provider tickets to amend this docstring. A maintainer or the TK-200
  settings-app author reads `tts_adapter.py`, sees "the ONE concrete adapter ... no cloud
  TTS," and concludes wombat exposes no cloud TTS seam — a conclusion the accepted
  governance decision required this exact file to no longer support. The provider tickets
  closed with the amendment undone.
  (`git log --oneline a82c8ac..HEAD -- src/wombat/sinks/tts_adapter.py` → empty.)
- **Verification verdict — CONFIRMED (severity minor):** verified against source and
  governance. `tts_adapter.py:7-8` still asserts the absolute; git log `a82c8ac..HEAD` for
  that file (and `sinks/asr.py`) is empty — neither was touched. DEC-28
  (`contract.yaml:5357`, accepted) explicitly supersedes-in-part Q-96's "no cloud TTS" and
  names this exact file for amendment by the provider tickets. TK-191/TK-192 did land the
  three cloud `TTSAdapter` implementations under the same protocol, so the docstring's
  absolute is now false at product scope and the named amendment obligation went
  undischarged. Attempted refutation — that the docstring is module-scoped and
  `Pyttsx3Adapter` truly is the only concrete adapter in `sinks/` — fails, because DEC-28
  names this file and the exact "no cloud TTS" phrasing as the thing to rescope.
  Correctly minor: documentation/governance-only, zero runtime, correctness, degrade, or
  secret-handling impact; the harm is plausible comprehension harm, not a night-breaking
  one. (Dimension: cross-cutting.)
- **Proposed fix direction:** discharge DEC-28's amendment clause — rescope the docstring
  to "the local/default adapter" and qualify the no-cloud claim to the default
  configuration, pointing to `voice/tts.py` + `voice/select.py` for the opt-in cloud seam.

### CR4-3 · `asr.py` docstring still asserts "no audio or transcript ever leaves the machine (CST-2/ASMP-1 posture)" — the DEC-28-mandated rescope was not applied

- **Where:** `src/wombat/sources/asr.py:38`.
- **Description:** DEC-28 rescopes Q-97's "no audio/transcript ever leaves the machine"
  from an invariant of wombat to an invariant of the DEFAULT configuration, and names
  `asr.py` among the module docstrings the provider tickets must amend. TK-189 (6bd8e67) /
  TK-190 (e263e84) shipped cloud STT (`DeepgramTranscriber`, `ElevenLabsScribeTranscriber`,
  `FishAudioTranscriber`) and TK-193 (96f2127) rerouted
  `sources.bootstrap._maybe_register_asr` to `voice.select.build_transcriber`, which can
  select a cloud `Transcriber` that `ASRSource` then drives — so audio can now leave the
  machine when a user opts in. git log confirms `asr.py` was untouched since TK-162
  (9e22fc3); its module docstring still states the pre-DEC-28 absolute "no audio or
  transcript ever leaves the machine (CST-2/ASMP-1 posture)" without the DEC-28
  default-config qualification.
- **Failure scenario:** a user opts into a cloud STT provider (the sanctioned DEC-28
  path); `ASRSource` now ships audio bytes to that provider. A reader of `asr.py`'s
  docstring sees the unqualified "no audio or transcript ever leaves the machine" and
  mis-models the current egress surface — the precise stale absolute DEC-28 required these
  provider tickets to correct, left uncorrected at arc close.
  (`git log --oneline a82c8ac..HEAD -- src/wombat/sources/asr.py` → empty.)
- **Verification verdict — CONFIRMED (severity minor):** verified against source and
  governance. DEC-28 (`contract.yaml:5357`) explicitly names `asr.py`; `asr.py:37-38`
  still carries the unqualified absolute. git log `a82c8ac..HEAD` for the file is empty
  (last touch TK-162/9e22fc3, pre-DEC-28); the provider tickets TK-189/190 (cloud STT) and
  TK-193 (bootstrap reroute to `voice.select.build_transcriber`, which can inject a cloud
  `Transcriber` into `ASRSource` on user opt-in) never amended it. `sinks/tts_adapter.py`
  was likewise left unamended (CR4-2), confirming a systematic miss of the DEC-28
  docstring clause. Genuine, in-scope governance non-compliance (an accepted decision's
  explicit itemized instruction unfulfilled at arc close), not a style nit. Correctly
  minor: the structural opt-in is enforced by `select.py`, so there is no runtime
  misbehavior or actual leak; the only harm is a reader mis-modeling the egress surface.
  (Dimension: cross-cutting.)
- **Proposed fix direction:** apply the DEC-28 qualification to the docstring — the
  no-egress claim holds for the DEFAULT configuration; cloud STT is a per-user opt-in via
  `voice/select.py` — same fix ticket as CR4-2.

---

## Plausible-but-unconfirmed (no CR4 ids — route-and-fix may consciously skip)

None in this pass.

---

## Suggested routing (for the architect — next governance step)

| Finding | Proposed home |
|---|---|
| CR4-1 | Small P3 fix ticket — contextualize the failed-local-fallback log on the healthy-cloud path in `voice/select.py`; reserve "voice output disabled" for the sole/primary-adapter path |
| CR4-2 | Docs-fix ticket (pair with CR4-3) — discharge DEC-28's named docstring-amendment clause on `sinks/tts_adapter.py` |
| CR4-3 | Same docs-fix ticket as CR4-2 — apply the DEC-28 default-config qualification to `sources/asr.py` |
