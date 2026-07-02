# Proxy-label rubric for the RISK-1 scoring fixture (TK-22 / DEC-24)

This rubric pre-labels each fixture event `surface` or `hold`. It is **derived from the
vision gate** (`planning/vision.md`): an item is surfaced only when it clears
**relevance ∧ importance ∧ user-state ∧ confidence**, and **silence is the default** —
speaking is the earned exception ("quiet-by-default ⇒ default hold").

These are **proxy** labels. RISK-1 is validated only when **Jim does a one-time confirmatory
pass** over them; agreement of `urgency()`/`cognitive_load()` vs. these proxy labels is an
*intermediate* signal, not the final human-judgment validation (hence `preliminary`).

## How each gate axis is operationalized for the fixture

- **Relevance** — is this addressed to / actionable by the user? Automated/marketing mail and
  CI/notification noise are not relevant on their own → fail.
- **Importance** — would the user want to act before the next natural break? VIP and
  known-human correspondence can clear this; automated mail cannot. A self calendar block is
  important only at its boundary (imminent), since the user already scheduled it.
- **User-state** — *out of scoring by design* (Q-12 / DEC-13). Presence is a separate
  gate-level hold applied AFTER scoring (TK-6). The rubric therefore does **not** use presence;
  it labels on relevance + importance + confidence only, exactly as the scoring functions see
  the world.
- **Confidence** — the deterministic signal must be unambiguous. Sparse/unknown sender → treat
  as automated (the quiet default), so ambiguity resolves toward hold.

## The decision rule (what `_proxy_label` encodes)

1. **Default = hold.** Quiet-by-default.
2. **Automated** sender class → **hold**, always (fails relevance ∧ importance).
3. **Self** calendar block → **surface** iff imminent (`urgency ≥ 0.78`), else hold — the
   steward nudges only at the boundary of a block the user already owns.
4. **Human** (vip / known_human / transactional) → **surface** iff `urgency ≥ 0.60`, else
   hold. High urgency for a human item means time-pressure and/or high sender priority have
   jointly cleared the bar.

## Why this is honest about the thesis

The fixture's real inbound mail in the sampled window was **entirely automated/transactional**
(deploy notifications, CI, package registry, newsletters, one payment statement). To exercise
the **surface** arm at all, a small number of **synthetic** VIP / known-human items were added
and are flagged `synthetic: true` in the fixture. The agreement number therefore measures
whether the scoring functions reproduce the rubric's surface/hold split across **both** the
real quiet-day mail and the injected human items — it does **not** claim the real day itself
contained surfacing-worthy human mail. That is precisely the judgment Jim's confirmatory pass
must settle.
