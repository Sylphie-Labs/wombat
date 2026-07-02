# cog-worx bug intake — wombat ⇄ cog-worx

A two-way channel between **wombat** (the consumer, built on cog-worx) and the
**cog-worx** maintainer agent (running in a separate terminal, rooted at
`../cog_worx/cog-worx`).

wombat is cog-worx's real-world testing ground: when wombat hits a bug, missing
capability, rough edge, or surprising behavior **in cog-worx** (not in wombat's own
code), file it here. The cog-worx agent watches this folder, fixes the issue in the
cog-worx repo, and writes the resolution back into the same file.

## How to file (wombat side)

1. Copy `TEMPLATE.md` to a new file named `YYYY-MM-DD-short-slug.md`
   (e.g. `2026-07-02-model-timeout-swallowed.md`).
2. Fill in **everything above the `--- cog-worx fills below ---` line**. The more
   concrete the repro + the verbatim traceback, the faster the fix.
3. Set `Status: OPEN`. Leave the lower half blank — that's the cog-worx agent's.
4. (Optional) drop a one-line pointer in `INDEX.md` so both sides see the queue at a glance.

## How it gets fixed (cog-worx side)

1. Pick up any file with `Status: OPEN`, set it to `IN-PROGRESS`.
2. Diagnose + fix in the cog-worx repo (own gates: ruff + mypy --strict + pytest + CANON).
3. Fill the lower half: diagnosis, fix (commit SHA + files), verification, and any
   **API/behavior change wombat must adopt** (this is the important back-channel — if a
   fix changes a signature or contract, wombat needs to know).
4. Set `Status: FIXED` (or `WONTFIX` / `NEEDS-INFO` with a reason).

## Status lifecycle

`OPEN` → `IN-PROGRESS` → `FIXED` · or `NEEDS-INFO` (bounced back to wombat) · or `WONTFIX`
(with rationale — e.g. "works as designed, here's the intended usage").

## Notes

- One bug per file. Keep them; a `FIXED` file is the record of what changed and why.
- This folder lives in the **wombat** repo, so both agents can read/write it from their
  own working trees. cog-worx never edits wombat's `src/`; it only replies here.
- Severity `blocker` on an OPEN file = wombat is hard-stopped; those jump the queue.
